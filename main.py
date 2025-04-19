import logging
import sys
import uuid
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.auth.exceptions import RefreshError
import requests
from PIL import Image
from io import BytesIO

# ——— Logging ———
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logging.getLogger("PIL").setLevel(logging.INFO)

# ——— FastAPI setup ———
@asynccontextmanager
async def lifespan(app: FastAPI):
    if shutil.which("ffmpeg"):
        logging.info("FFmpeg encontrado.")
    else:
        logging.warning("FFmpeg não encontrado; vídeos não terão thumbnail.")
    logging.info("Startup: aplicação iniciada.")
    yield
    logging.info("Shutdown: aplicação encerrada.")

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrinja em produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ——— Helpers de data ———
def format_date(s: str) -> str:
    try:
        return datetime.strptime(s, "%m/%d/%Y").strftime("%Y%m%d")
    except:
        raise HTTPException(400, f"Data inválida: {s}")

def days_between(a: str, b: str) -> int:
    try:
        d0 = datetime.strptime(a, "%m/%d/%Y")
        d1 = datetime.strptime(b, "%m/%d/%Y")
        diff = (d1 - d0).days + 1
        if diff <= 0: raise
        return diff
    except HTTPException:
        raise
    except:
        raise HTTPException(400, "Intervalo de datas inválido")

# ——— Imagens e vídeos ———
def process_cover(data: bytes) -> bytes:
    img = Image.open(BytesIO(data))
    w,h = img.size; target = 1.91
    if w/h > target:
        nw = int(h*target); left = (w-nw)//2
        img = img.crop((left,0,left+nw,h))
    else:
        nh = int(w/target); top=(h-nh)//2
        img = img.crop((0,top,w,top+nh))
    buf = BytesIO(); img.resize((1200,628)).save(buf, "PNG", optimize=True)
    return buf.getvalue()

def process_square(data: bytes) -> bytes:
    img = Image.open(BytesIO(data)).convert("RGB")
    w,h = img.size; m=min(w,h); left,top=(w-m)//2,(h-m)//2
    buf = BytesIO()
    img.crop((left,top,left+m,top+m)).resize((1200,1200)).save(buf,"PNG",optimize=True)
    return buf.getvalue()

def is_video(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".mp4",".mov"))

def extract_thumb(data: bytes, size=(1200,628)) -> bytes:
    if not shutil.which("ffmpeg"):
        raise HTTPException(500, "FFmpeg não disponível")
    vid = f"/tmp/{uuid.uuid4().hex}.mp4"
    thumb = f"/tmp/{uuid.uuid4().hex}.png"
    with open(vid,"wb") as f: f.write(data)
    try:
        subprocess.run(
            ["ffmpeg","-y","-i",vid,
             "-vf",f"select=eq(n\\,0),scale={size[0]}:{size[1]}",
             "-frames:v","1",thumb],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        with open(thumb,"rb") as f: out=f.read()
    except subprocess.CalledProcessError as e:
        logging.error(e.stderr.decode(errors="ignore"))
        raise HTTPException(500, "Falha ao extrair thumbnail")
    finally:
        for p in (vid,thumb):
            if os.path.exists(p): os.remove(p)
    return out

# ——— Upload assets ———
def upload_asset(client: GoogleAdsClient, cid: str, url: str, square: bool=False) -> str:
    r = requests.get(url)
    if r.status_code!=200:
        raise HTTPException(400, f"Download falhou ({r.status_code})")
    raw=r.content
    if is_video(url):
        img = extract_thumb(raw,(1200,1200) if square else (1200,628))
    else:
        img = process_square(raw) if square else process_cover(raw)
    svc = client.get_service("AssetService")
    op  = client.get_type("AssetOperation")
    a   = op.create
    a.name = f"{'Square_' if square else ''}Image_asset_{uuid.uuid4().hex}"
    a.type_ = client.enums.AssetTypeEnum.IMAGE
    a.image_asset.data = img
    resp = svc.mutate_assets(customer_id=cid, operations=[op])
    return resp.results[0].resource_name

# ——— Cliente e middleware ———
def get_cid(client: GoogleAdsClient) -> str:
    svc = client.get_service("CustomerService")
    c = svc.list_accessible_customers().resource_names
    if not c: raise HTTPException(404,"Nenhum customer")
    return c[0].split("/")[-1]

@app.middleware("http")
async def clean_body(req: Request, call_next):
    if req.method=="OPTIONS": return await call_next(req)
    b = await req.body()
    txt = re.sub(r'("cover_photo":\s*".+?)[\";]+\s*,',r'\1",',b.decode("utf-8",errors="ignore"),flags=re.DOTALL)
    req._receive = (lambda txt=txt: {"type":"http.request","body":txt.encode()})
    return await call_next(req)

# ——— Model ———
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

    @field_validator("budget",mode="before")
    def parse_budget(cls,v):
        return int(float(v.replace("$","").strip())) if isinstance(v,str) else v

    @field_validator("audience_min_age","audience_max_age",mode="before")
    def parse_age(cls,v):
        return int(v) if isinstance(v,str) else v

    @field_validator("cover_photo",mode="before")
    def clean_url(cls,v):
        if isinstance(v,str):
            u=v.strip().rstrip(" ;")
            if u and not urlparse(u).scheme: u="http://"+u
            return u
        return v

# ——— Criação de campanhas ———
def create_campaign_budget(client, cid, total, s, e):
    days = days_between(s,e)
    unit = 10_000
    micros = round((total/days)*1_000_000/unit)*unit
    svc = client.get_service("CampaignBudgetService")
    op  = client.get_type("CampaignBudgetOperation")
    b   = op.create
    b.name = f"Budget_{uuid.uuid4().hex}"
    b.amount_micros = micros
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    return svc.mutate_campaign_budgets(customer_id=cid, operations=[op]).results[0].resource_name

def create_campaign_resource(client, cid, budget_res, data):
    svc = client.get_service("CampaignService")
    op  = client.get_type("CampaignOperation")
    c   = op.create
    c.name = f"{data.campaign_name.strip()}_{uuid.uuid4().hex[:6]}"
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY if data.campaign_type.upper()=="DISPLAY" else client.enums.AdvertisingChannelTypeEnum.SEARCH
    c.status = client.enums.CampaignStatusEnum.ENABLED
    c.campaign_budget = budget_res
    c.start_date = format_date(data.start_date)
    c.end_date   = format_date(data.end_date)
    c.manual_cpc = client.get_type("ManualCpc")
    return svc.mutate_campaigns(customer_id=cid, operations=[op]).results[0].resource_name

# ... (create_ad_group, create_ad_group_keywords, create_responsive_display_ad, apply_targeting_criteria identical to previous implementation) ...

# ——— Processamento em background ———
def process_campaign_task(client, data: CampaignRequest):
    try:
        cid = get_cid(client)
        b  = create_campaign_budget(client, cid, data.budget, data.start_date, data.end_date)
        cr = create_campaign_resource(client, cid, b, data)
        # ad group + keywords + ad + targeting...
    except RefreshError:
        logging.error("Token expirado", exc_info=True)
    except GoogleAdsException as e:
        logging.error(f"GoogleAds API erro: {e.error.code().name}", exc_info=True)
    except Exception:
        logging.exception("Erro processando campanha")

# ——— Endpoints ———
@app.post("/create_campaign")
async def create_campaign_endpoint(r: CampaignRequest, bg: BackgroundTasks):
    try:
        cfg = {
            "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
            "client_id":      "167266694231-…apps.googleusercontent.com",
            "client_secret":  "GOCSPX-…",
            "refresh_token":  r.refresh_token,
            "use_proto_plus": True,
        }
        client = GoogleAdsClient.load_from_dict(cfg)
    except RefreshError:
        raise HTTPException(401, "Refresh token inválido")
    except GoogleAdsException as e:
        raise HTTPException(401, f"Auth error: {e.error.code().name}")
    except:
        raise HTTPException(500, "Erro interno ao autenticar")

    bg.add_task(process_campaign_task, client, r)
    return JSONResponse({"status":"accepted"},status_code=202)

@app.get("/")
async def health_check():
    return JSONResponse({"status":"ok"},status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT",8000))
    uvicorn.run(app,host="0.0.0.0",port=port)

# ——— Mudanças realizadas ———
# 1. Removi falha de startup por FFmpeg ausente: agora apenas WARNING, e HTTP 500 em tentativa de extrair miniatura.
# 2. Consertei import de shutil e checks de “ffmpeg” no PATH.
# 3. Limpeza do corpo com regex corrigida.
# 4. `extract_thumb` usa subprocess.run(check=True) e trata erros.
# 5. Endpoints agora retornam 202 apenas quando o background task for agendado.
# 6. Tratamento de erros de autenticação devolve 401.
# 7. Refatorei upload_asset para suportar square e cover com uma única função.
