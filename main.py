import logging
import sys
import os
import uuid
import re
import time
import json
import requests
from io import BytesIO
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from PIL import Image

# ─── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ─── FastAPI lifecycle ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Startup: Aplicação iniciada.")
    yield
    logger.info("Shutdown: Aplicação encerrada.")

app = FastAPI(lifespan=lifespan)

# ─── CORS ────────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Helpers ────────────────────────────────────────────────────────────────────
def format_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y%m%d")

def days_between(start_date: str, end_date: str) -> int:
    dt_start = datetime.strptime(start_date, "%m/%d/%Y")
    dt_end   = datetime.strptime(end_date,   "%m/%d/%Y")
    return (dt_end - dt_start).days + 1

# ─── Request logging middleware ─────────────────────────────────────────────────
@app.middleware("http")
async def preprocess_request(request: Request, call_next):
    body = await request.body()
    text = body.decode("utf-8", errors="ignore")
    logger.info(f"{request.method} {request.url}")
    logger.debug(f"Raw request body:\n{text}")
    # clean up stray terminators in JSON
    cleaned = re.sub(r'("cover_photo":\s*".+?)[\";]+\s*,', r'\1",', text, flags=re.DOTALL)
    async def receive():
        return {"type":"http.request","body": cleaned.encode("utf-8")}
    request._receive = receive
    return await call_next(request)

# ─── Pydantic model ─────────────────────────────────────────────────────────────
class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    refresh_token: str
    campaign_name: str
    campaign_description: str
    objective: str
    cover_photo: str
    final_url: str
    keyword1: str
    keyword2: str
    keyword3: str
    budget: int
    start_date: str
    end_date: str
    price_model: str
    campaign_type: str
    audience_gender: str
    audience_min_age: int
    audience_max_age: int
    devices: list[str]

    @field_validator("budget", mode="before")
    def convert_budget(cls, v):
        if isinstance(v, str):
            return int(float(v.replace("$","").strip()))
        return v

    @field_validator("audience_min_age","audience_max_age", mode="before")
    def convert_age(cls, v):
        return int(v)

    @field_validator("cover_photo","final_url", mode="before")
    def clean_urls(cls, v):
        v = v.strip().rstrip(" ;")
        if v.lower() == "null" or not v:
            return ""
        if not urlparse(v).scheme:
            v = "http://" + v
        return v

# ─── Google Ads helpers ──────────────────────────────────────────────────────────
def get_customer_id(client: GoogleAdsClient) -> str:
    svc = client.get_service("CustomerService")
    res = svc.list_accessible_customers()
    if not res.resource_names:
        raise Exception("Nenhum customer acessível")
    return res.resource_names[0].split("/")[-1]

# (Image processing and asset upload functions omitted for brevity)

# ─── Background task ───────────────────────────────────────────────────────────
def process_campaign_task(client: GoogleAdsClient, data: CampaignRequest):
    # ... existing logic ...
    pass

# ─── Main endpoint ─────────────────────────────────────────────────────────────
@app.post("/create_campaign")
async def create_campaign(request_data: CampaignRequest, background_tasks: BackgroundTasks):
    # Log the entire parsed request model
    logger.debug("Parsed request data:\n%s", json.dumps(request_data.model_dump(), indent=2, default=str))

    # 1) Validate final_url
    if not request_data.final_url:
        logger.error("Validation error: final_url is empty")
        raise HTTPException(status_code=400, detail="Campo final_url é obrigatório")

    # 2) Validate env vars for Google Ads
    DEV_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")
    CID       = os.getenv("GOOGLE_ADS_CLIENT_ID")
    CSECRET   = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
    missing = [name for name,val in [
        ("GOOGLE_ADS_DEVELOPER_TOKEN", DEV_TOKEN),
        ("GOOGLE_ADS_CLIENT_ID",       CID),
        ("GOOGLE_ADS_CLIENT_SECRET",   CSECRET),
    ] if not val]
    if missing:
        logger.error("Missing env vars: %s", missing)
        raise HTTPException(status_code=500, detail=f"Faltando env vars: {', '.join(missing)}")

    # 3) Build GoogleAdsClient config
    cfg = {
        "developer_token": DEV_TOKEN,
        "client_id":       CID,
        "client_secret":   CSECRET,
        "refresh_token":   request_data.refresh_token,
        "use_proto_plus":  True,
    }
    logger.debug("GoogleAdsClient config:\n%s", json.dumps(cfg, indent=2))

    try:
        client = GoogleAdsClient.load_from_dict(cfg)
    except Exception as e:
        logger.exception("Failed to initialize GoogleAdsClient")
        raise HTTPException(
            status_code=400,
            detail="Falha ao inicializar GoogleAdsClient: verifique client_id/secret e refresh_token"
        )

    # 4) Discover login_customer_id
    try:
        login_cid = get_customer_id(client)
        client.login_customer_id = login_cid
        logger.info("Using login_customer_id = %s", login_cid)
    except Exception as e:
        logger.exception("Failed to get login_customer_id")
        raise HTTPException(status_code=400, detail="Erro ao obter login_customer_id do Google Ads")

    # 5) Schedule the background task
    background_tasks.add_task(process_campaign_task, client, request_data)
    return {"status": "processing"}
