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
        if diff <= 0:
            raise ValueError()
        return diff
    except HTTPException:
        raise
    except:
        raise HTTPException(400, "Intervalo de datas inválido")

# ——— Emoji and symbol removal ———
_emoji_pattern = re.compile(
    "[\U0001F300-\U0001F6FF\U0001F900-\U0001F9FF\U0001F1E0-\U0001F1FF]+",
    flags=re.UNICODE
)
def remove_emojis(text: str) -> str:
    return _emoji_pattern.sub("", text)

# ——— Truncation helper ———
def truncate(text: str, max_len: int) -> str:
    clean = text.strip()
    return clean if len(clean) <= max_len else clean[:max_len]

# ——— Processamento de imagens e vídeos ———
def process_cover(data: bytes) -> bytes:
    img = Image.open(BytesIO(data))
    w, h = img.size
    target = 1.91
    if w / h > target:
        new_w = int(h * target)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    buf = BytesIO()
    img.resize((1200, 628)).save(buf, "PNG", optimize=True)
    return buf.getvalue()

def process_square(data: bytes) -> bytes:
    img = Image.open(BytesIO(data)).convert("RGB")
    w, h = img.size
    m = min(w, h)
    left, top = (w - m) // 2, (h - m) // 2
    buf = BytesIO()
    img.crop((left, top, left + m, top + m)).resize((1200, 1200)).save(buf, "PNG", optimize=True)
    return buf.getvalue()

def is_video(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".mp4", ".mov"))

def extract_thumb(data: bytes, size=(1200, 628)) -> bytes:
    if not shutil.which("ffmpeg"):
        raise HTTPException(500, "FFmpeg não disponível")
    vid = f"/tmp/{uuid.uuid4().hex}.mp4"
    thumb = f"/tmp/{uuid.uuid4().hex}.png"
    with open(vid, "wb") as f:
        f.write(data)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", vid,
             "-vf", f"select=eq(n\\,0),scale={size[0]}:{size[1]}",
             "-frames:v", "1", thumb],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )
        with open(thumb, "rb") as f:
            out = f.read()
    except subprocess.CalledProcessError as e:
        logging.error(e.stderr.decode(errors="ignore"))
        raise HTTPException(500, "Falha ao extrair thumbnail")
    finally:
        for p in (vid, thumb):
            if os.path.exists(p):
                os.remove(p)
    return out

# ——— Upload de assets ———
def upload_asset(client: GoogleAdsClient, cid: str, url: str, square: bool = False) -> str:
    resp = requests.get(url)
    if resp.status_code != 200:
        raise HTTPException(400, f"Download falhou ({resp.status_code})")
    raw = resp.content
    if is_video(url):
        img = extract_thumb(raw, (1200, 1200) if square else (1200, 628))
    else:
        img = process_square(raw) if square else process_cover(raw)
    svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    a = op.create
    a.name = f"{'Square_' if square else ''}Image_asset_{uuid.uuid4().hex}"
    a.type_ = client.enums.AssetTypeEnum.IMAGE
    a.image_asset.data = img
    res = svc.mutate_assets(customer_id=cid, operations=[op])
    return res.results[0].resource_name

# ——— Helpers do Google Ads ———
def get_cid(client: GoogleAdsClient) -> str:
    svc = client.get_service("CustomerService")
    c = svc.list_accessible_customers().resource_names
    if not c:
        raise HTTPException(404, "Nenhum customer acessível")
    return c[0].split("/")[-1]

@app.middleware("http")
async def clean_body(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    body = await request.body()
    text = re.sub(
        r'("cover_photo":\s*".+?)[\";]+\s*,',
        r'\1",',
        body.decode("utf-8", errors="ignore"),
        flags=re.DOTALL
    )
    request._receive = lambda text=text: {"type": "http.request", "body": text.encode()}
    return await call_next(request)

# ——— Modelo de requisição ———
class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    refresh_token:        str
    campaign_name:        str
    campaign_description: str
    objective:            str
    cover_photo:          str
    final_url:            str
    keyword1:             str
    keyword2:             str
    keyword3:             str
    budget:               int
    start_date:           str
    end_date:             str
    price_model:          str
    campaign_type:        str
    audience_gender:      str
    audience_min_age:     int
    audience_max_age:     int
    devices:              list[str]

    @field_validator("budget", mode="before")
    def parse_budget(cls, v):
        return int(float(v.replace("$", "").strip())) if isinstance(v, str) else v

    @field_validator("audience_min_age", "audience_max_age", mode="before")
    def parse_age(cls, v):
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
def create_campaign_budget(client, cid, total, start, end) -> str:
    days = days_between(start, end)
    unit = 10_000
    micros = round((total / days) * 1_000_000 / unit) * unit
    svc = client.get_service("CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    b = op.create
    b.name = f"Budget_{uuid.uuid4().hex}"
    b.amount_micros = micros
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    return svc.mutate_campaign_budgets(customer_id=cid, operations=[op]).results[0].resource_name

def create_campaign_resource(client, cid, budget_res, data: CampaignRequest) -> str:
    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    c = op.create
    c.name = f"{data.campaign_name.strip()}_{uuid.uuid4().hex[:6]}"
    c.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.DISPLAY
        if data.campaign_type.upper() == "DISPLAY"
        else client.enums.AdvertisingChannelTypeEnum.SEARCH
    )
    c.status = client.enums.CampaignStatusEnum.ENABLED
    c.campaign_budget = budget_res
    c.start_date = format_date(data.start_date)
    c.end_date = format_date(data.end_date)
    c.manual_cpc = client.get_type("ManualCpc")
    return svc.mutate_campaigns(customer_id=cid, operations=[op]).results[0].resource_name

def create_ad_group(client, cid, camp_res, data: CampaignRequest) -> str:
    svc = client.get_service("AdGroupService")
    op = client.get_type("AdGroupOperation")
    ag = op.create
    ag.name = f"{data.campaign_name.strip()}_AdGroup_{uuid.uuid4().hex[:6]}"
    ag.campaign = camp_res
    ag.status = client.enums.AdGroupStatusEnum.ENABLED
    ag.type_ = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ag.cpc_bid_micros = 1_000_000
    return svc.mutate_ad_groups(customer_id=cid, operations=[op]).results[0].resource_name

def create_ad_group_keywords(client, cid, ag_res, data: CampaignRequest):
    svc = client.get_service("AdGroupCriterionService")
    ops = []
    for kw in (data.keyword1, data.keyword2, data.keyword3):
        if kw:
            op = client.get_type("AdGroupCriterionOperation")
            crt = op.create
            crt.ad_group = ag_res
            crt.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            crt.keyword.text = kw
            crt.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
            ops.append(op)
    if ops:
        svc.mutate_ad_group_criteria(customer_id=cid, operations=ops)

def create_responsive_display_ad(client, cid, ag_res, data: CampaignRequest) -> str:
    svc = client.get_service("AdGroupAdService")
    op = client.get_type("AdGroupAdOperation")
    ada = op.create
    ada.ad_group = ag_res
    ada.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad = ada.ad
    ad.final_urls.append(data.final_url)

    for txt in (data.keyword1 or data.campaign_name.strip(), data.keyword2, data.keyword3):
        if txt:
            clean = remove_emojis(txt)
            h = client.get_type("AdTextAsset")
            h.text = truncate(clean, 30)
            ad.responsive_display_ad.headlines.append(h)

    for txt in (data.campaign_description.replace("\n", " "), data.objective):
        if txt:
            clean = remove_emojis(txt)
            d = client.get_type("AdTextAsset")
            d.text = truncate(clean, 90)
            ad.responsive_display_ad.descriptions.append(d)

    bn = remove_emojis(data.campaign_name.strip())
    ad.responsive_display_ad.business_name = truncate(bn, 25)

    long_h = remove_emojis(f"{data.campaign_name.strip()} - {data.objective.strip()}")
    ad.responsive_display_ad.long_headline.text = truncate(long_h, 90)

    main_res = upload_asset(client, cid, data.cover_photo, square=False)
    square_res = upload_asset(client, cid, data.cover_photo, square=True)
    img1 = client.get_type("AdImageAsset"); img1.asset = main_res
    img2 = client.get_type("AdImageAsset"); img2.asset = square_res
    ad.responsive_display_ad.marketing_images.append(img1)
    ad.responsive_display_ad.square_marketing_images.append(img2)

    return svc.mutate_ad_group_ads(customer_id=cid, operations=[op]).results[0].resource_name

def apply_targeting_criteria(client, cid, camp_res, data: CampaignRequest):
    svc = client.get_service("CampaignCriterionService")
    ops = []
    g = data.audience_gender.upper()
    if g in ("MALE", "FEMALE"):
        excludes = ["FEMALE", "UNDETERMINED"] if g == "MALE" else ["MALE", "UNDETERMINED"]
        for ex in excludes:
            op = client.get_type("CampaignCriterionOperation")
            crt = op.create
            crt.campaign = camp_res
            crt.gender.type_ = client.enums.GenderTypeEnum[ex]
            crt.negative = True
            crt.status = client.enums.CampaignCriterionStatusEnum.ENABLED
            ops.append(op)
    if ops:
        svc.mutate_campaign_criteria(customer_id=cid, operations=ops)

# ——— Background task ———
def process_campaign_task(client: GoogleAdsClient, data: CampaignRequest):
    try:
        cid = get_cid(client)
        budget_res = create_campaign_budget(client, cid, data.budget, data.start_date, data.end_date)
        camp_res = create_campaign_resource(client, cid, budget_res, data)
        ag_res = create_ad_group(client, cid, camp_res, data)
        create_ad_group_keywords(client, cid, ag_res, data)
        create_responsive_display_ad(client, cid, ag_res, data)
        apply_targeting_criteria(client, cid, camp_res, data)
        logging.info("Campanha processada com sucesso.")
    except RefreshError:
        logging.error("Refresh token inválido ou expirado", exc_info=True)
    except GoogleAdsException as e:
        logging.error(f"Google Ads API error: {e.error.code().name}", exc_info=True)
    except Exception:
        logging.exception("Erro no processamento de campanha")

# ——— Endpoints ———
@app.post("/create_campaign")
async def create_campaign_endpoint(req: CampaignRequest, bg: BackgroundTasks):
    try:
        cfg = {
            "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
            "client_id":      "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com",
            "client_secret":  "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",
            "refresh_token":  req.refresh_token,
            "use_proto_plus": True,
        }
        client = GoogleAdsClient.load_from_dict(cfg)
    except RefreshError:
        raise HTTPException(401, "Refresh token inválido ou expirado")
    except GoogleAdsException as e:
        raise HTTPException(401, f"Google Ads auth error: {e.error.code().name}")
    except Exception:
        logging.exception("Erro inicializando GoogleAdsClient")
        raise HTTPException(500, "Erro interno ao autenticar com Google Ads")

    bg.add_task(process_campaign_task, client, req)
    return JSONResponse({"status": "accepted"}, status_code=200)

@app.get("/")
async def health_check():
    return JSONResponse({"status": "ok"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logging.info(f"Iniciando uvicorn em 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
