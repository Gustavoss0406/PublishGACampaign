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
from google.ads.googleads.errors import GoogleAdsException
import requests
from PIL import Image, ImageOps
from io import BytesIO

# ——— Logging ———
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
# Suprime logs de plugin de imagem muito verbosos
logging.getLogger("PIL").setLevel(logging.INFO)

# ——— FastAPI setup ———
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Startup: aplicação iniciada.")
    yield
    logging.info("Shutdown: aplicação encerrada.")

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
def format_date(s: str) -> str:
    try:
        dt = datetime.strptime(s, "%m/%d/%Y")
        return dt.strftime("%Y%m%d")
    except Exception:
        raise HTTPException(400, f"Data inválida: {s}")

def days_between(start: str, end: str) -> int:
    try:
        d0 = datetime.strptime(start, "%m/%d/%Y")
        d1 = datetime.strptime(end,   "%m/%d/%Y")
        diff = (d1 - d0).days + 1
        if diff <= 0:
            raise ValueError("end_date deve ser após start_date")
        return diff
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Intervalo de datas inválido")

# ——— Processamento de imagens ———
def process_cover_photo(data: bytes) -> bytes:
    img = Image.open(BytesIO(data))
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

def process_square_image(data: bytes) -> bytes:
    img = Image.open(BytesIO(data)).convert("RGB")
    w, h = img.size
    m = min(w, h)
    left, top = (w - m)//2, (h - m)//2
    img = img.crop((left, top, left + m, top + m))
    img = img.resize((1200, 1200))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def extract_video_thumbnail(data: bytes, size: tuple[int,int] = (1200, 628)) -> bytes:
    vid = f"/tmp/{uuid.uuid4().hex}.mp4"
    img = f"/tmp/{uuid.uuid4().hex}.png"
    with open(vid, "wb") as f:
        f.write(data)
    cmd = [
        "ffmpeg", "-y", "-i", vid,
        "-vf", f"select=eq(n\\,0),scale={size[0]}:{size[1]}",
        "-frames:v", "1", img
    ]
    res = subprocess.run(cmd, capture_output=True)
    os.remove(vid)
    if res.returncode != 0:
        logging.error(res.stderr.decode(errors="ignore"))
        raise HTTPException(500, "Falha ao extrair thumbnail de vídeo")
    with open(img, "rb") as f:
        thumb = f.read()
    os.remove(img)
    return thumb

# ——— Upload de assets ———
def upload_image_asset(client: GoogleAdsClient, customer_id: str, url: str, process: bool = False) -> str:
    r = requests.get(url)
    if r.status_code != 200:
        raise HTTPException(400, f"Download falhou ({r.status_code})")
    raw = r.content
    if url.lower().endswith((".mp4", ".mov")):
        img = extract_video_thumbnail(raw, (1200, 628))
    else:
        img = process_cover_photo(raw) if process else raw
    svc = client.get_service("AssetService")
    op  = client.get_type("AssetOperation")
    a   = op.create
    a.name = f"Image_asset_{uuid.uuid4().hex}"
    a.type_ = client.enums.AssetTypeEnum.IMAGE
    a.image_asset.data = img
    resp = svc.mutate_assets(customer_id=customer_id, operations=[op])
    return resp.results[0].resource_name

def upload_square_image_asset(client: GoogleAdsClient, customer_id: str, url: str) -> str:
    r = requests.get(url)
    if r.status_code != 200:
        raise HTTPException(400, f"Download falhou ({r.status_code})")
    raw = r.content
    if url.lower().endswith((".mp4", ".mov")):
        img = extract_video_thumbnail(raw, (1200, 1200))
    else:
        img = process_square_image(raw)
    svc = client.get_service("AssetService")
    op  = client.get_type("AssetOperation")
    a   = op.create
    a.name = f"Square_Image_asset_{uuid.uuid4().hex}"
    a.type_ = client.enums.AssetTypeEnum.IMAGE
    a.image_asset.data = img
    resp = svc.mutate_assets(customer_id=customer_id, operations=[op])
    return resp.results[0].resource_name

# ——— Cliente Google Ads ———
def get_customer_id(client: GoogleAdsClient) -> str:
    svc = client.get_service("CustomerService")
    custs = svc.list_accessible_customers()
    if not custs.resource_names:
        raise HTTPException(404, "Nenhum customer acessível")
    return custs.resource_names[0].split("/")[-1]

# ——— Middleware para limpar body raw ———
@app.middleware("http")
async def sanitize_body(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    body = await request.body()
    txt = body.decode("utf-8", errors="ignore")
    txt = re.sub(r'("cover_photo":\s*".+?)[\";]+\s*,', r'\1",', txt, flags=re.DOTALL)
    modified = txt.encode("utf-8")
    async def recv():
        return {"type": "http.request", "body": modified}
    request._receive = recv
    return await call_next(request)

# ——— Pydantic Model ———
class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    refresh_token:       str
    campaign_name:       str
    campaign_description:str
    objective:           str
    cover_photo:         str
    final_url:           str
    keyword1:            str
    keyword2:            str
    keyword3:            str
    budget:              int
    start_date:          str
    end_date:            str
    price_model:         str
    campaign_type:       str
    audience_gender:     str
    audience_min_age:    int
    audience_max_age:    int
    devices:             list[str]

    @field_validator("budget", mode="before")
    def to_int_budget(cls, v):
        if isinstance(v, str):
            return int(float(v.replace("$", "").strip()))
        return v

    @field_validator("audience_min_age", "audience_max_age", mode="before")
    def to_int_age(cls, v):
        return int(v) if isinstance(v, str) else v

    @field_validator("cover_photo", mode="before")
    def clean_url(cls, v):
        if isinstance(v, str):
            u = v.strip().rstrip(" ;")
            if u and not urlparse(u).scheme:
                u = "http://" + u
            return u
        return v

# ——— Criação de recursos de campanha ———
def create_campaign_budget(client, cust_id, total: int, start: str, end: str) -> str:
    days = days_between(start, end)
    daily = total / days
    unit  = 10_000
    micros= round(daily * 1_000_000 / unit) * unit
    svc = client.get_service("CampaignBudgetService")
    op  = client.get_type("CampaignBudgetOperation")
    b   = op.create
    b.name            = f"Budget_{uuid.uuid4().hex}"
    b.amount_micros   = micros
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    resp = svc.mutate_campaign_budgets(customer_id=cust_id, operations=[op])
    return resp.results[0].resource_name

def create_campaign_resource(client, cust_id, budget_res: str, data: CampaignRequest) -> str:
    svc = client.get_service("CampaignService")
    op  = client.get_type("CampaignOperation")
    c   = op.create
    c.name                         = f"{data.campaign_name.strip()}_{uuid.uuid4().hex[:6]}"
    c.advertising_channel_type     = (
        client.enums.AdvertisingChannelTypeEnum.DISPLAY
        if data.campaign_type.upper()=="DISPLAY"
        else client.enums.AdvertisingChannelTypeEnum.SEARCH
    )
    c.status                       = client.enums.CampaignStatusEnum.ENABLED
    c.campaign_budget              = budget_res
    c.start_date                   = format_date(data.start_date)
    c.end_date                     = format_date(data.end_date)
    c.manual_cpc                   = client.get_type("ManualCpc")
    resp = svc.mutate_campaigns(customer_id=cust_id, operations=[op])
    return resp.results[0].resource_name

def create_ad_group(client, cust_id, camp_res: str, data: CampaignRequest) -> str:
    svc = client.get_service("AdGroupService")
    op  = client.get_type("AdGroupOperation")
    ag  = op.create
    ag.name               = f"{data.campaign_name.strip()}_AdGroup_{uuid.uuid4().hex[:6]}"
    ag.campaign           = camp_res
    ag.status             = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_              = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ag.cpc_bid_micros     = 1_000_000
    resp = svc.mutate_ad_groups(customer_id=cust_id, operations=[op])
    return resp.results[0].resource_name

def create_ad_group_keywords(client, cust_id, ag_res: str, data: CampaignRequest):
    svc = client.get_service("AdGroupCriterionService")
    ops = []
    for kw in (data.keyword1, data.keyword2, data.keyword3):
        if kw:
            op  = client.get_type("AdGroupCriterionOperation")
            crt = op.create
            crt.ad_group        = ag_res
            crt.status          = client.enums.AdGroupCriterionStatusEnum.ENABLED
            crt.keyword.text    = kw
            crt.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
            ops.append(op)
    if ops:
        svc.mutate_ad_group_criteria(customer_id=cust_id, operations=ops)

def create_responsive_display_ad(client, cust_id, ag_res: str, data: CampaignRequest) -> str:
    svc = client.get_service("AdGroupAdService")
    op  = client.get_type("AdGroupAdOperation")
    ada = op.create
    ada.ad_group = ag_res
    ada.status   = client.enums.AdGroupAdStatusEnum.ENABLED
    ad = ada.ad
    ad.final_urls.append(data.final_url)

    for txt in (data.keyword1 or data.campaign_name.strip(), data.keyword2, data.keyword3):
        if txt:
            h = client.get_type("AdTextAsset")
            h.text = txt
            ad.responsive_display_ad.headlines.append(h)

    for txt in (data.campaign_description, data.objective):
        if txt:
            d = client.get_type("AdTextAsset")
            d.text = txt
            ad.responsive_display_ad.descriptions.append(d)

    ad.responsive_display_ad.business_name = data.campaign_name.strip()
    ad.responsive_display_ad.long_headline.text = f"{data.campaign_name.strip()} - {data.objective.strip()}"

    if not data.cover_photo:
        raise HTTPException(400, "cover_photo não fornecida")

    if data.cover_photo.startswith("http"):
        main_res   = upload_image_asset(client, cust_id, data.cover_photo, process=True)
        square_res = upload_square_image_asset(client, cust_id, data.cover_photo)
    else:
        main_res = square_res = data.cover_photo

    img1 = client.get_type("AdImageAsset"); img1.asset = main_res
    img2 = client.get_type("AdImageAsset"); img2.asset = square_res
    ad.responsive_display_ad.marketing_images.append(img1)
    ad.responsive_display_ad.square_marketing_images.append(img2)

    resp = svc.mutate_ad_group_ads(customer_id=cust_id, operations=[op])
    return resp.results[0].resource_name

def apply_targeting_criteria(client, cust_id, camp_res: str, data: CampaignRequest):
    svc = client.get_service("CampaignCriterionService")
    ops = []
    gdr = data.audience_gender.upper()
    if gdr in ("MALE", "FEMALE"):
        excludes = (["FEMALE","UNDETERMINED"] if gdr=="MALE" else ["MALE","UNDETERMINED"])
        for ex in excludes:
            op  = client.get_type("CampaignCriterionOperation")
            crt = op.create
            crt.campaign       = camp_res
            crt.gender.type_   = client.enums.GenderTypeEnum[ex]
            crt.negative       = True
            crt.status         = client.enums.CampaignCriterionStatusEnum.ENABLED
            ops.append(op)
    if ops:
        svc.mutate_campaign_criteria(customer_id=cust_id, operations=ops)

# ——— Background task ———
def process_campaign_task(client: GoogleAdsClient, data: CampaignRequest):
    try:
        cid       = get_customer_id(client)
        budget_id = create_campaign_budget(client, cid, data.budget, data.start_date, data.end_date)
        camp_id   = create_campaign_resource(client, cid, budget_id, data)
        ag_id     = create_ad_group(client, cid, camp_id, data)
        create_ad_group_keywords(client, cid, ag_id, data)
        create_responsive_display_ad(client, cid, ag_id, data)
        apply_targeting_criteria(client, cid, camp_id, data)
        logging.info("Campanha processada com sucesso.")
    except GoogleAdsException as e:
        logging.error(f"GoogleAdsException: {e.failure}", exc_info=True)
    except Exception:
        logging.exception("Erro no processamento de campanha")

# ——— Endpoints ———
@app.post("/create_campaign")
async def create_campaign_endpoint(req: CampaignRequest, bg: BackgroundTasks):
    try:
        cfg = {
            "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
            "client_id":      "167266694231-g7hvta57r99et...apps.googleusercontent.com",
            "client_secret":  "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",
            "refresh_token":  req.refresh_token,
            "use_proto_plus": True
        }
        client = GoogleAdsClient.load_from_dict(cfg)
    except Exception as e:
        logging.error("Falha ao inicializar GoogleAdsClient", exc_info=True)
        raise HTTPException(400, str(e))

    bg.add_task(process_campaign_task, client, req)
    return JSONResponse({"status": "accepted"}, status_code=202)

@app.get("/")
async def health_check():
    return JSONResponse({"status": "ok"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
