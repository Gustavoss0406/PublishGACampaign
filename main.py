import logging
import sys
import uuid
import os
import re
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import requests
from PIL import Image
from io import BytesIO

# Configuração de logs detalhados
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Startup: Aplicação iniciada.")
    yield
    logger.info("Shutdown: Aplicação encerrada.")

app = FastAPI(lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data helpers
def format_date(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%m/%d/%Y")
    return dt.strftime("%Y%m%d")

def days_between(start_date: str, end_date: str) -> int:
    dt_start = datetime.strptime(start_date, "%m/%d/%Y")
    dt_end = datetime.strptime(end_date, "%m/%d/%Y")
    return (dt_end - dt_start).days + 1

# Imagens
def process_cover_photo(image_data: bytes) -> bytes:
    img = Image.open(BytesIO(image_data))
    w, h = img.size
    target_ratio = 1.91
    ratio = w / h
    if ratio > target_ratio:
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
    left = (w - m) // 2
    top  = (h - m) // 2
    img = img.crop((left, top, left + m, top + m)).resize((1200, 1200))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def upload_image_asset(client: GoogleAdsClient, customer_id: str, image_url: str, process: bool = False) -> str:
    logger.info(f"Download da imagem: {image_url}")
    resp = requests.get(image_url)
    if resp.status_code != 200:
        raise Exception(f"Falha no download da imagem: {resp.status_code}")
    data = resp.content
    if process:
        data = process_cover_photo(data)
    svc = client.get_service("AssetService")
    op  = client.get_type("AssetOperation")
    asset = op.create
    asset.name = f"Image_asset_{uuid.uuid4()}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = data
    res = svc.mutate_assets(customer_id=customer_id, operations=[op])
    return res.results[0].resource_name

def upload_square_image_asset(client: GoogleAdsClient, customer_id: str, image_url: str) -> str:
    logger.info(f"Download imagem quadrada: {image_url}")
    resp = requests.get(image_url)
    if resp.status_code != 200:
        raise Exception(f"Falha no download da imagem: {resp.status_code}")
    data = process_square_image(resp.content)
    svc = client.get_service("AssetService")
    op  = client.get_type("AssetOperation")
    asset = op.create
    asset.name = f"Square_Image_asset_{uuid.uuid4()}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = data
    res = svc.mutate_assets(customer_id=customer_id, operations=[op])
    return res.results[0].resource_name

def get_customer_id(client: GoogleAdsClient) -> str:
    svc = client.get_service("CustomerService")
    res = svc.list_accessible_customers()
    if not res.resource_names:
        raise Exception("Nenhum customer acessível")
    return res.resource_names[0].split("/")[-1]

# Log middleware
@app.middleware("http")
async def preprocess_request(request: Request, call_next):
    logger.info(f"{request.method} {request.url}")
    body = await request.body()
    text = body.decode("utf-8", errors="ignore")
    logger.info(f"Request body raw: {text}")
    # limpa cover_photo terminator
    cleaned = re.sub(r'("cover_photo":\s*".+?)[\";]+\s*,', r'\1",', text, flags=re.DOTALL)
    async def receive():
        return {"type":"http.request","body": cleaned.encode("utf-8")}
    request._receive = receive
    return await call_next(request)

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
            v = v.replace("$","").strip()
            return int(float(v))
        return v

    @field_validator("audience_min_age","audience_max_age", mode="before")
    def convert_age(cls, v):
        return int(v)

    @field_validator("cover_photo", mode="before")
    def clean_cover(cls, v):
        v = v.strip().rstrip(" ;")
        if v and not urlparse(v).scheme:
            v = "http://" + v
        return v

# Criação de budget
def create_campaign_budget(client: GoogleAdsClient, customer_id: str, total: int, start: str, end: str) -> str:
    days = days_between(start, end)
    if days <= 0:
        raise Exception("Datas inválidas")
    daily = total / days
    unit = 10_000
    micros = round(daily * 1_000_000 / unit) * unit
    svc = client.get_service("CampaignBudgetService")
    op  = client.get_type("CampaignBudgetOperation")
    budget = op.create
    budget.name = f"Budget_{uuid.uuid4()}"
    budget.amount_micros = int(micros)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    res = svc.mutate_campaign_budgets(customer_id=customer_id, operations=[op])
    return res.results[0].resource_name

def create_campaign_resource(client: GoogleAdsClient, customer_id: str, budget_res: str, data: CampaignRequest) -> str:
    svc = client.get_service("CampaignService")
    op  = client.get_type("CampaignOperation")
    camp = op.create
    camp.name = f"{data.campaign_name}_{uuid.uuid4().hex[:6]}"
    camp.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.DISPLAY
        if data.campaign_type.upper()=="DISPLAY"
        else client.enums.AdvertisingChannelTypeEnum.SEARCH
    )
    camp.status = client.enums.CampaignStatusEnum.ENABLED
    camp.campaign_budget = budget_res
    camp.start_date = format_date(data.start_date)
    camp.end_date   = format_date(data.end_date)
    res = svc.mutate_campaigns(customer_id=customer_id, operations=[op])
    return res.results[0].resource_name

def create_ad_group(client: GoogleAdsClient, customer_id: str, camp_res: str, data: CampaignRequest) -> str:
    svc = client.get_service("AdGroupService")
    op  = client.get_type("AdGroupOperation")
    ag  = op.create
    ag.name = f"{data.campaign_name}_AdGroup_{uuid.uuid4().hex[:6]}"
    ag.campaign = camp_res
    ag.status   = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_    = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ag.cpc_bid_micros = 1_000_000
    res = svc.mutate_ad_groups(customer_id=customer_id, operations=[op])
    return res.results[0].resource_name

def create_ad_group_keywords(client: GoogleAdsClient, customer_id: str, adg_res: str, data: CampaignRequest):
    svc = client.get_service("AdGroupCriterionService")
    ops = []
    def mk_kw(kw):
        op = client.get_type("AdGroupCriterionOperation")
        c = op.create
        c.ad_group = adg_res
        c.status   = client.enums.AdGroupCriterionStatusEnum.ENABLED
        c.keyword.text = kw
        c.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
        return op
    for kw in [data.keyword1, data.keyword2, data.keyword3]:
        if kw:
            ops.append(mk_kw(kw))
    if ops:
        svc.mutate_ad_group_criteria(customer_id=customer_id, operations=ops)

# Corrigida: instanciação correta dos assets e text assets
def create_responsive_display_ad(client: GoogleAdsClient,
                                 customer_id: str,
                                 ad_group_resource_name: str,
                                 data: CampaignRequest) -> str:
    logger.info("Criando Responsive Display Ad.")
    svc = client.get_service("AdGroupAdService")
    op  = client.get_type("AdGroupAdOperation")
    aga = op.create
    aga.ad_group = ad_group_resource_name
    aga.status   = client.enums.AdGroupAdStatusEnum.ENABLED

    ad = aga.ad
    ad.final_urls.append(data.final_url)

    # Headlines
    for txt in [data.keyword1 or data.campaign_name, data.keyword2, data.keyword3]:
        asset = client.get_type("AdTextAsset")()
        asset.text = txt
        ad.responsive_display_ad.headlines.append(asset)

    # Descriptions
    for desc in [data.campaign_description, data.objective]:
        asset = client.get_type("AdTextAsset")()
        asset.text = desc
        ad.responsive_display_ad.descriptions.append(asset)

    ad.responsive_display_ad.business_name = data.campaign_name
    long_hl = client.get_type("AdTextAsset")()
    long_hl.text = f"{data.campaign_name} – {data.objective}"
    ad.responsive_display_ad.long_headline.CopyFrom(long_hl)

    # Images
    if not data.cover_photo:
        raise Exception("cover_photo vazio")

    mkt_res  = upload_image_asset(client, customer_id, data.cover_photo, process=True)
    sqr_res  = upload_square_image_asset(client, customer_id, data.cover_photo)

    img1 = client.get_type("AdImageAsset")()
    img1.asset = mkt_res
    ad.responsive_display_ad.marketing_images.append(img1)

    img2 = client.get_type("AdImageAsset")()
    img2.asset = sqr_res
    ad.responsive_display_ad.square_marketing_images.append(img2)

    res = svc.mutate_ad_group_ads(customer_id=customer_id, operations=[op])
    return res.results[0].resource_name

def apply_targeting_criteria(client: GoogleAdsClient, customer_id: str, camp_res: str, data: CampaignRequest):
    svc = client.get_service("CampaignCriterionService")
    ops = []
    if data.audience_gender.upper() in ["MALE","FEMALE"]:
        exclude = ["FEMALE","UNDETERMINED"] if data.audience_gender.upper()=="MALE" else ["MALE","UNDETERMINED"]
        for g in exclude:
            op = client.get_type("CampaignCriterionOperation")
            c = op.create
            c.campaign = camp_res
            c.gender.type_ = client.enums.GenderTypeEnum[g]
            c.negative = True
            ops.append(op)
    if ops:
        svc.mutate_campaign_criteria(customer_id=customer_id, operations=ops)

@app.get("/")
async def health_check():
    return {"status": "ok"}

def process_campaign_task(client: GoogleAdsClient, data: CampaignRequest):
    try:
        cid       = get_customer_id(client)
        budget    = create_campaign_budget(client, cid, data.budget, data.start_date, data.end_date)
        camp_res  = create_campaign_resource(client, cid, budget, data)
        adg_res   = create_ad_group(client, cid, camp_res, data)
        create_ad_group_keywords(client, cid, adg_res, data)
        create_responsive_display_ad(client, cid, adg_res, data)
        apply_targeting_criteria(client, cid, camp_res, data)
        logger.info("Campanha Google Ads criada com sucesso.")
    except Exception:
        logger.exception("Erro no processamento da campanha.")

@app.post("/create_campaign")
async def create_campaign(request_data: CampaignRequest, background_tasks: BackgroundTasks):
    try:
        cfg = {
            "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
            "client_id":       os.getenv("GOOGLE_ADS_CLIENT_ID"),
            "client_secret":   os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
            "refresh_token":   request_data.refresh_token,
            "use_proto_plus":  True
        }
        client = GoogleAdsClient.load_from_dict(cfg)
    except Exception as e:
        logger.error("Erro ao inicializar Google Ads Client", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
    background_tasks.add_task(process_campaign_task, client, request_data)
    return {"status": "processing"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
