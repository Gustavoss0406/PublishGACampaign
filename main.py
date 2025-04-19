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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
import requests
from PIL import Image, ImageOps
from io import BytesIO

# ——— Configuração de logs detalhados ———
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
# Suprime os DEBUG de importação de plugins do PIL
logging.getLogger("PIL").setLevel(logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Startup: Aplicação iniciada.")
    yield
    logging.info("Shutdown: Aplicação encerrada.")

app = FastAPI(lifespan=lifespan)

# CORS pra teste (em produção, restrinja)
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
        raise HTTPException(400, f"Data inválida: {date_str}")

def days_between(start_date: str, end_date: str) -> int:
    try:
        dt_start = datetime.strptime(start_date, "%m/%d/%Y")
        dt_end = datetime.strptime(end_date, "%m/%d/%Y")
        delta = (dt_end - dt_start).days + 1
        if delta <= 0:
            raise ValueError("end_date deve ser após start_date")
        return delta
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Erro ao calcular intervalo entre '{start_date}' e '{end_date}': {e}")
        raise HTTPException(400, "Intervalo de datas inválido")

# ——— Funções de imagem ———
def process_cover_photo(image_data: bytes) -> bytes:
    img = Image.open(BytesIO(image_data))
    w, h = img.size
    target = 1.91
    ratio = w / h
    if ratio > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target)
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
    left, top = (w - m)//2, (h - m)//2
    img = img.crop((left, top, left + m, top + m))
    img = img.resize((1200, 1200))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def extract_video_thumbnail(video_bytes: bytes, size: tuple[int,int] = (1200, 628)) -> bytes:
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
    os.remove(vid_path)
    if result.returncode != 0:
        logging.error(f"ffmpeg error: {result.stderr.decode(errors='ignore')}")
        raise HTTPException(500, "Falha ao extrair thumbnail de vídeo")
    with open(img_path, "rb") as f:
        data = f.read()
    os.remove(img_path)
    return data

# ——— Upload de assets ao Google Ads ———
def upload_image_asset(client: GoogleAdsClient, customer_id: str, url: str, process: bool = False) -> str:
    resp = requests.get(url)
    if resp.status_code != 200:
        raise HTTPException(400, f"Falha no download ({resp.status_code})")
    content = resp.content
    if url.lower().endswith((".mp4", ".mov")):
        img_data = extract_video_thumbnail(content, (1200, 628))
    else:
        img_data = process_cover_photo(content) if process else content
    svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.create
    asset.name = f"Image_asset_{uuid.uuid4().hex}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = img_data
    res = svc.mutate_assets(customer_id=customer_id, operations=[op])
    return res.results[0].resource_name

def upload_square_image_asset(client: GoogleAdsClient, customer_id: str, url: str) -> str:
    resp = requests.get(url)
    if resp.status_code != 200:
        raise HTTPException(400, f"Falha no download ({resp.status_code})")
    content = resp.content
    if url.lower().endswith((".mp4", ".mov")):
        img_data = extract_video_thumbnail(content, (1200, 1200))
    else:
        img_data = process_square_image(content)
    svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.create
    asset.name = f"Square_Image_asset_{uuid.uuid4().hex}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = img_data
    res = svc.mutate_assets(customer_id=customer_id, operations=[op])
    return res.results[0].resource_name

def get_customer_id(client: GoogleAdsClient) -> str:
    svc = client.get_service("CustomerService")
    custs = svc.list_accessible_customers()
    if not custs.resource_names:
        raise HTTPException(404, "Nenhum customer acessível")
    return custs.resource_names[0].split("/")[-1]

# ——— Middleware para interceptar e limpar o body raw ———
@app.middleware("http")
async def sanitize_body(request: Request, call_next):
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    body = await request.body()
    text = body.decode("utf-8", errors="ignore")
    text = re.sub(r'("cover_photo":\s*".+?)[\";]+\s*,', r'\1",', text, flags=re.DOTALL)
    modified = text.encode("utf-8")
    async def receive():
        return {"type": "http.request", "body": modified}
    request._receive = receive
    return await call_next(request)

# ——— Pydantic model ———
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
    def str_to_int(cls, v):
        if isinstance(v, str):
            return int(float(v.replace("$", "").strip()))
        return v

    @field_validator("audience_min_age", "audience_max_age", mode="before")
    def age_to_int(cls, v):
        return int(v) if isinstance(v, str) else v

    @field_validator("cover_photo", mode="before")
    def clean_url(cls, v):
        if isinstance(v, str):
            u = v.strip().rstrip(" ;")
            if u and not urlparse(u).scheme:
                u = "http://" + u
            return u
        return v

# ——— Funções de criação de campanha, ad group, keywords, ads, targeting ———
# (mantenha aqui exatamente seu código original para:
# create_campaign_budget,
# create_campaign_resource,
# create_ad_group,
# create_ad_group_keywords,
# create_responsive_display_ad,
# apply_targeting_criteria)

def process_campaign_task(client: GoogleAdsClient, data: CampaignRequest):
    try:
        cid = get_customer_id(client)
        budget_res = create_campaign_budget(client, cid, data.budget, data.start_date, data.end_date)
        camp_res = create_campaign_resource(client, cid, budget_res, data)
        ag_res   = create_ad_group(client, cid, camp_res, data)
        create_ad_group_keywords(client, cid, ag_res, data)
        create_responsive_display_ad(client, cid, ag_res, data)
        apply_targeting_criteria(client, cid, camp_res, data)
        logging.info("Campanha processada com sucesso.")
    except Exception:
        logging.exception("Erro no processamento de campanha")

# ——— Endpoint principal ———
@app.post("/create_campaign")
async def create_campaign(req: CampaignRequest, bg: BackgroundTasks):
    try:
        cfg = {
            "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
            "client_id":      "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com",
            "client_secret":  "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",
            "refresh_token":  req.refresh_token,
            "use_proto_plus": True
        }
        client = GoogleAdsClient.load_from_dict(cfg)
    except Exception as e:
        logging.error("Erro inicializando GoogleAdsClient", exc_info=True)
        raise HTTPException(400, str(e))

    bg.add_task(process_campaign_task, client, req)
    return JSONResponse({"status": "accepted"}, status_code=202)

# ——— Health check ———
@app.get("/")
async def health_check():
    return JSONResponse({"status": "ok"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
