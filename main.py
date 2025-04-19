import logging
import sys
import uuid
import os
import re
import subprocess
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
import requests
from PIL import Image, ImageOps
from io import BytesIO

# Configuração de logs detalhados
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Startup: Aplicação iniciada.")
    yield
    logging.info("Shutdown: Aplicação encerrada.")

app = FastAPI(lifespan=lifespan)

# CORS (em produção, restrinja as origens)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ——— Helpers de data ———
def format_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return dt.strftime("%Y%m%d")
    except Exception as e:
        logging.error(f"Erro ao formatar data '{date_str}': {e}")
        raise

def days_between(start_date: str, end_date: str) -> int:
    try:
        dt_start = datetime.strptime(start_date, "%m/%d/%Y")
        dt_end = datetime.strptime(end_date, "%m/%d/%Y")
        return (dt_end - dt_start).days + 1
    except Exception as e:
        logging.error(f"Erro ao calcular intervalo entre '{start_date}' e '{end_date}': {e}")
        raise

# ——— Funções de imagem ———
def process_cover_photo(image_data: bytes) -> bytes:
    img = Image.open(BytesIO(image_data))
    w, h = img.size
    target_ratio = 1.91
    current_ratio = w / h
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    img = img.resize((1200, 628))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def process_square_image(image_data: bytes) -> bytes:
    img = Image.open(BytesIO(image_data)).convert("RGB")
    w, h = img.size
    m = min(w, h)
    left, top = (w - m) // 2, (h - m) // 2
    img = img.crop((left, top, left + m, top + m))
    img = img.resize((1200, 1200))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def extract_video_thumbnail(video_bytes: bytes, size: tuple[int,int] = (1200, 628)) -> bytes:
    """
    Extrai o primeiro frame de vídeo (.mp4, .mov, etc.) usando ffmpeg CLI.
    Requer que o binário `ffmpeg` esteja instalado no sistema.
    """
    vid_path = f"/tmp/{uuid.uuid4().hex}.mp4"
    img_path = f"/tmp/{uuid.uuid4().hex}.png"
    with open(vid_path, "wb") as f:
        f.write(video_bytes)

    cmd = [
        "ffmpeg", "-y",
        "-i", vid_path,
        "-vf", f"select=eq(n\\,0),scale={size[0]}:{size[1]}",
        "-frames:v", "1",
        img_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        logging.error(f"ffmpeg error: {result.stderr.decode()}")
        os.remove(vid_path)
        raise Exception("Falha ao extrair thumbnail de vídeo com ffmpeg")

    with open(img_path, "rb") as f:
        data = f.read()

    os.remove(vid_path)
    os.remove(img_path)
    return data

# ——— Upload de assets para Google Ads ———
def upload_image_asset(client: GoogleAdsClient, customer_id: str, image_url: str, process: bool = False) -> str:
    logging.info(f"Download de mídia: {image_url}")
    resp = requests.get(image_url)
    if resp.status_code != 200:
        raise Exception(f"Falha no download ({resp.status_code})")
    raw = resp.content

    if image_url.lower().endswith((".mp4", ".mov")):
        image_data = extract_video_thumbnail(raw, size=(1200, 628))
    else:
        image_data = process_cover_photo(raw) if process else raw

    svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.create
    asset.name = f"Image_asset_{uuid.uuid4().hex}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = image_data

    result = svc.mutate_assets(customer_id=customer_id, operations=[op])
    return result.results[0].resource_name

def upload_square_image_asset(client: GoogleAdsClient, customer_id: str, image_url: str) -> str:
    logging.info(f"Download quadrado: {image_url}")
    resp = requests.get(image_url)
    if resp.status_code != 200:
        raise Exception(f"Falha no download ({resp.status_code})")
    raw = resp.content

    if image_url.lower().endswith((".mp4", ".mov")):
        processed = extract_video_thumbnail(raw, size=(1200, 1200))
    else:
        processed = process_square_image(raw)

    svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.create
    asset.name = f"Square_Image_asset_{uuid.uuid4().hex}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = processed

    result = svc.mutate_assets(customer_id=customer_id, operations=[op])
    return result.results[0].resource_name

# ——— Obtém customer ID ———
def get_customer_id(client: GoogleAdsClient) -> str:
    service = client.get_service("CustomerService")
    customers = service.list_accessible_customers()
    if not customers.resource_names:
        raise Exception("Nenhum customer acessível encontrado.")
    return customers.resource_names[0].split("/")[-1]

# ——— Middleware pra logar e limpar body ———
@app.middleware("http")
async def preprocess_request_body(request: Request, call_next):
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    logging.info(f"Recebendo {request.method} {request.url}")
    body_bytes = await request.body()
    text = body_bytes.decode("utf-8", errors="ignore")
    logging.info(f"Request body raw: {text}")
    text = re.sub(
        r'("cover_photo":\s*".+?)[\";]+\s*,',
        r'\1",',
        text,
        flags=re.DOTALL
    )
    modified = text.encode("utf-8")
    async def receive():
        return {"type": "http.request", "body": modified}
    request._receive = receive
    return await call_next(request)

# ——— Model de request ———
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
            return int(float(v.replace("$", "").strip()))
        return v

    @field_validator("audience_min_age", "audience_max_age", mode="before")
    def convert_age(cls, v):
        return int(v) if isinstance(v, str) else v

    @field_validator("cover_photo", mode="before")
    def clean_cover_photo(cls, v):
        if isinstance(v, str):
            u = v.strip().rstrip(" ;")
            if u and not urlparse(u).scheme:
                u = "http://" + u
            logging.debug(f"Cover photo limpa: {u}")
            return u
        return v

# ——— Criação de Campaign Budget ———
def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_total: int, start_date: str, end_date: str) -> str:
    days = days_between(start_date, end_date)
    if days <= 0:
        raise Exception("Intervalo de datas inválido.")
    daily = budget_total / days
    unit = 10_000
    micros = round(daily * 1_000_000 / unit) * unit
    svc = client.get_service("CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    budget = op.create
    budget.name = f"Budget_{uuid.uuid4().hex}"
    budget.amount_micros = micros
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    resp = svc.mutate_campaign_budgets(customer_id=customer_id, operations=[op])
    return resp.results[0].resource_name

# ——— Resto das funções de criação de campanha, ad groups, ads, targeting etc. ———
# (mantenha o mesmo conteúdo que você já tinha implementado para create_campaign_resource,
# create_ad_group, create_ad_group_keywords, create_responsive_display_ad,
# apply_targeting_criteria e o endpoint /create_campaign)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logging.info(f"Iniciando uvicorn em 0.0.0.0:{port}.")
    uvicorn.run(app, host="0.0.0.0", port=port)
