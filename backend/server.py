# server.py
from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import requests
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr
from typing import List
import uuid
from datetime import datetime

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Read env
MONGO_URL = os.environ.get('MONGO_URL', '')
DB_NAME = os.environ.get('DB_NAME', 'test_database')
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '*')

# Try to connect to MongoDB but keep service working even if DB fails.
client = None
db = None
if MONGO_URL:
    try:
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        logger.info("Connected to MongoDB")
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        client = None
        db = None
else:
    logger.warning("MONGO_URL not set — status & admin endpoints using DB will be limited.")

app = FastAPI()
api_router = APIRouter(prefix="/api")


# Pydantic models
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class StatusCheckCreate(BaseModel):
    client_name: str

class ContactForm(BaseModel):
    name: str
    email: EmailStr
    company: str = ""
    message: str

class ContactResponse(BaseModel):
    status: str
    message: str


@api_router.get("/")
async def root():
    return {"message": "Hello World - backend up"}


# STATUS routes use Mongo if available
@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    status_dict = input.dict()
    status_obj = StatusCheck(**status_dict)
    _ = await db.status_checks.insert_one(status_obj.dict())
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    status_checks = await db.status_checks.find().to_list(1000)
    return [StatusCheck(**status_check) for status_check in status_checks]


# Admin page to view contact submissions stored in Mongo (if available)
@api_router.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    if db is None:
        return HTMLResponse("""
            <html><body>
              <h2>Admin panel unavailable</h2>
              <p>MongoDB is not configured on this deployment. Set MONGO_URL and redeploy to enable admin panel.</p>
            </body></html>
        """, status_code=503)

    try:
        contacts = await db.contacts.find().sort("timestamp", -1).to_list(100)
        for contact in contacts:
            contact["_id"] = str(contact["_id"])

        contacts_html = ""
        for contact in contacts:
            timestamp = contact.get('timestamp', 'N/A')
            if isinstance(timestamp, datetime):
                timestamp = timestamp.strftime("%Y-%m-%d %H:%M:%S")
            contacts_html += f"""
            <div style="border:1px solid #ddd; padding:16px; margin:10px 0; border-radius:8px; background:#fff;">
              <h3 style="margin:0;color:#2563eb;">{contact.get('name','N/A')}</h3>
              <p><strong>Email:</strong> {contact.get('email','N/A')}</p>
              <p><strong>Company:</strong> {contact.get('company','')}</p>
              <p><strong>Date:</strong> {timestamp}</p>
              <div style="background:#f9f9f9;padding:10px;border-left:4px solid #2563eb;">
                {contact.get('message','')}
              </div>
            </div>
            """

        if not contacts_html:
            contacts_html = "<p style='text-align:center;color:#666;padding:40px;'>No submissions yet.</p>"

        html_content = f"""
        <!DOCTYPE html>
        <html><head><title>Contact Submissions</title>
        <meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1"/>
        <style>body{{font-family:Arial,Helvetica,sans-serif;background:#f5f7fb;padding:20px}}.container{{max-width:900px;margin:0 auto}}</style>
        </head><body><div class='container'>
        <h1>Contact Submissions</h1>
        <div><strong>Total:</strong> {len(contacts)}</div>
        <div style="margin-top:20px">{contacts_html}</div>
        </div></body></html>
        """
        return HTMLResponse(html_content)
    except Exception as e:
        logger.error(f"Error generating admin panel: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate admin panel")


@api_router.get("/contacts")
async def get_contacts():
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    try:
        contacts = await db.contacts.find().sort("timestamp", -1).to_list(100)
        for contact in contacts:
            contact["_id"] = str(contact["_id"])
        return {"status": "success", "count": len(contacts), "contacts": contacts}
    except Exception as e:
        logger.error(f"Error fetching contacts: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch contacts")


# Contact route - send to Google Apps Script (Sheets)
@api_router.post("/contact", response_model=ContactResponse)
async def contact_form(form_data: ContactForm):
    try:
        # IMPORTANT: update this URL if Emergent gave a different script URL
        google_sheets_url = os.environ.get("GOOGLE_SHEETS_URL",
            "https://script.google.com/macros/s/AKfycbyT65djHFUaZiVA1Jj86BwIuVYrWdttp96KxRlcyb_jMCJN4OL1wP3eCGfL6Lqz7VS6IA/exec"
        )

        sheets_data = {
            "name": form_data.name,
            "email": form_data.email,
            "company": form_data.company,
            "message": form_data.message
        }

        response = requests.post(
            google_sheets_url,
            json=sheets_data,
            headers={'Content-Type': 'application/json'},
            timeout=15
        )

        if response.status_code == 200:
            logger.info(f"Sent to Google Sheets: {form_data.email}")
            # Optionally save to Mongo if available
            if db is not None:
                try:
                    doc = form_data.dict()
                    doc['timestamp'] = datetime.utcnow()
                    await db.contacts.insert_one(doc)
                except Exception as e:
                    logger.error(f"Saving to Mongo failed: {e}")

            return ContactResponse(status="success", message="Thanks — we'll get back to you.")
        else:
            logger.error(f"Google Sheets error: {response.status_code} {response.text}")
            raise HTTPException(status_code=500, detail="Failed to send message")
    except requests.exceptions.Timeout:
        logger.error("Timeout contacting Google Sheets")
        raise HTTPException(status_code=500, detail="Timeout sending message")
    except Exception as e:
        logger.error(f"Contact form error: {e}")
        raise HTTPException(status_code=500, detail="Failed to send message")


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=CORS_ORIGINS.split(',') if CORS_ORIGINS else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    if client:
        client.close()
