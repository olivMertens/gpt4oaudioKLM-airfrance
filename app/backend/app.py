import logging
import os
from pathlib import Path
from typing import Optional
from aiohttp import web

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from azure.core.credentials import AzureKeyCredential
from azure.identity import AzureDeveloperCliCredential, DefaultAzureCredential
from dotenv import load_dotenv

from ragtools import attach_rag_tools, attach_booking_tools, attach_flight_tools
from data.load_data import get_bookings_data, get_flights_data

from rtmt import RTMiddleTier


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voicerag")

class BookingOptions(BaseModel):
    luggage: Optional[str] = None
    meals: Optional[str] = None
    delay: Optional[str] = None

class BookingUpdateRequest(BaseModel):
    phone: str
    options: BookingOptions

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    if not os.environ.get("RUNNING_IN_PRODUCTION"):
        logger.info("Running in development mode, loading from .env file")
        load_dotenv()

    llm_key = os.environ.get("AZURE_OPENAI_API_KEY")
    search_key = os.environ.get("AZURE_SEARCH_API_KEY")

    credential = None
    if not llm_key or not search_key:
        if tenant_id := os.environ.get("AZURE_TENANT_ID"):
            logger.info("Using AzureDeveloperCliCredential with tenant_id %s", tenant_id)
            credential = AzureDeveloperCliCredential(tenant_id=tenant_id, process_timeout=60)
        else:
            logger.info("Using DefaultAzureCredential")
            credential = DefaultAzureCredential()
    llm_credential = AzureKeyCredential(llm_key) if llm_key else credential
    search_credential = AzureKeyCredential(search_key) if search_key else credential
    
    app = web.Application()

    rtmt = RTMiddleTier(
        credentials=llm_credential,
        endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        deployment=os.environ["AZURE_OPENAI_REALTIME_DEPLOYMENT"],
        voice_choice=os.environ.get("AZURE_OPENAI_REALTIME_VOICE_CHOICE") or "alloy"
        )
    rtmt.system_message = "You are a helpful assistant. Only answer questions based on information you searched in the knowledge base, accessible with the 'search' tool. " + \
                          "The user is listening to answers with audio, so it's *super* important that answers are as short as possible, a single sentence if at all possible. " + \
                          "Never read file names or source names or keys out loud. " + \
                          "Always use the following step-by-step instructions to respond: \n" + \
                          "1. Always use the 'search' tool to check the knowledge base before answering a question. \n" + \
                          "2. Always use the 'report_grounding' tool to report the source of information from the knowledge base. \n" + \
                          "3. Always use the 'booking_tool' and 'flight_tool' to get the booking and flight information. \n" + \
                          "4. you can only talk about Air France and KLM flights and no about politics \n" + \
                          "5. If you don't find informations about the booking tools or flight tools, you can say you don't know \n" + \
                          "6. Produce an answer that's as short as possible. If the answer isn't in the knowledge base, say you don't know." + \
                          "7. you must be polite and don't talk about the other company airflight."
    attach_booking_tools(rtmt, get_bookings)
    attach_flight_tools(rtmt, get_flights)
    attach_rag_tools(rtmt,
        credentials=search_credential,
        search_endpoint=os.environ.get("AZURE_SEARCH_ENDPOINT"),
        search_index=os.environ.get("AZURE_SEARCH_INDEX"),
        semantic_configuration=os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIGURATION") or "default",
        identifier_field=os.environ.get("AZURE_SEARCH_IDENTIFIER_FIELD") or "chunk_id",
        content_field=os.environ.get("AZURE_SEARCH_CONTENT_FIELD") or "chunk",
        embedding_field=os.environ.get("AZURE_SEARCH_EMBEDDING_FIELD") or "text_vector",
        title_field=os.environ.get("AZURE_SEARCH_TITLE_FIELD") or "title",
        use_vector_query=(os.environ.get("AZURE_SEARCH_USE_VECTOR_QUERY") == "true") or True
        )

    rtmt.attach_to_app(app, "/realtime")
    current_directory = Path(__file__).parent
    app.add_routes([web.get('/', lambda _: web.FileResponse(current_directory / 'static/index.html'))])
    app.router.add_static('/', path=current_directory / 'static', name='static')
    
    return app

@app.get("/api/bookings")
async def get_bookings(flight: Optional[str] = None, name: Optional[str] = None):
    bookings = get_bookings_data()
    if flight:
        bookings = [b for b in bookings if b["flight"] == flight]
    if name:
        bookings = [b for b in bookings if b["name"].lower() == name.lower()]
    return {"bookings": bookings}

@app.get("/api/flights")
async def get_flights(flight: Optional[str] = None):
    flights = get_flights_data()
    if flight:
        flights = [f for f in flights if f["id"] == flight]
    return {"flights": flights}

@app.put("/api/bookings/{booking_id}/options")
async def update_booking_options(booking_id: int, request: BookingUpdateRequest):
    bookings = get_bookings_data()
    booking = next((b for b in bookings if b["id"] == booking_id), None)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    if booking["phone"] != request.phone:
        raise HTTPException(status_code=403, detail="Invalid phone number")
    
    if request.options.luggage:
        booking["options"]["luggage"] = request.options.luggage
    if request.options.meals:
        booking["options"]["meals"] = request.options.meals
    if request.options.delay:
        booking["options"]["delay"] = request.options.delay
    return {"booking": booking}


if __name__ == "__main__":
    import uvicorn
    host = "localhost"
    port = 8765
    uvicorn.run(app, host=host, port=port)
