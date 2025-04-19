import logging
import sys
import os
import time
import json
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ─── FastAPI setup ─────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Constantes ─────────────────────────────────────────────────────────────────
FB_API_VERSION      = "v16.0"
GLOBAL_COUNTRIES    = ["US","CA","GB","DE","FR","BR","IN","MX","IT","ES","NL","SE","NO","DK","FI","CH","JP","KR"]
PUBLISHER_PLATFORMS = ["facebook","instagram","audience_network","messenger"]

OBJECTIVE_TO_OPT_GOAL = {
    "OUTCOME_AWARENESS":   "IMPRESSIONS",
    "OUTCOME_TRAFFIC":     "LINK_CLICKS",
    "OUTCOME_LEADS":       "LEAD_GENERATION",
    "OUTCOME_SALES":       "OFFSITE_CONVERSIONS",
    "OUTCOME_ENGAGEMENT":  "PAGE_LIKES",       # para curtir página via engajamento&#8203;:contentReference[oaicite:0]{index=0}
}

OBJECTIVE_TO_BILLING_EVENT = {
    "OUTCOME_AWARENESS":   "IMPRESSIONS",
    "OUTCOME_TRAFFIC":     "LINK_CLICKS",
    "OUTCOME_LEADS":       "IMPRESSIONS",
    "OUTCOME_SALES":       "IMPRESSIONS",
    "OUTCOME_ENGAGEMENT":  "PAGE_LIKES",       # cobra por like de página
}

CTA_MAP = {
    "OUTCOME_AWARENESS":   {"type": "LEARN_MORE", "value": {"link": ""}},
    "OUTCOME_TRAFFIC":     {"type": "LEARN_MORE", "value": {"link": ""}},
    "OUTCOME_LEADS":       {"type": "SIGN_UP",    "value": {"link": ""}},
    "OUTCOME_SALES":       {"type": "SHOP_NOW",   "value": {"link": ""}},
    "OUTCOME_ENGAGEMENT":  {"type": "LIKE_PAGE",  "value": {"page": ""}},
}

# ─── Helpers ────────────────────────────────────────────────────────────────────
def extract_fb_error(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return err.get("error_user_msg") or err.get("message") or resp.text
    except:
        return resp.text or "Erro desconhecido"

def rollback_campaign(campaign_id: str, token: str):
    logger.debug(f"Rollback: deletando campanha {campaign_id}")
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{campaign_id}"
    resp = requests.delete(url, params={"access_token": token})
    logger.info(f"Rollback status {resp.status_code}: {resp.text}")

def upload_video_to_fb(account_id: str, token: str, video_url: str) -> str:
    url = f"https://graph.facebook.com/{FB_API_VERSION}/act_{account_id}/advideos"
    payload = {"file_url": video_url, "access_token": token}
    logger.debug(f"Upload vídeo payload: {json.dumps(payload)}")
    resp = requests.post(url, data=payload)
    logger.debug(f"Upload vídeo status {resp.status_code}: {resp.text}")
    if resp.status_code != 200:
        raise Exception(f"Erro ao enviar vídeo: {extract_fb_error(resp)}")
    vid = resp.json().get("id")
    if not vid:
        raise Exception("Facebook não retornou video_id")
    return vid

def fetch_video_thumbnail(video_id: str, token: str) -> str:
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{video_id}/thumbnails"
    for i in range(5):
        logger.debug(f"Thumbnail tentativa {i+1} para video_id {video_id}")
        resp = requests.get(url, params={"access_token": token})
        logger.debug(f"Thumbnail status {resp.status_code}: {resp.text}")
        data = resp.json().get("data", [])
        if resp.status_code == 200 and data:
            return data[0]["uri"]
        time.sleep(2)
    raise Exception("Não foi possível obter thumbnail")

def get_page_id(token: str) -> str:
    url = f"https://graph.facebook.com/{FB_API_VERSION}/me/accounts"
    logger.debug("Buscando page_id")
    resp = requests.get(url, params={"access_token": token})
    logger.debug(f"get_page_id status {resp.status_code}: {resp.text}")
    data = resp.json().get("data", [])
    if not data:
        raise HTTPException(status_code=533, detail="Nenhuma página disponível")
    return data[0]["id"]

def check_account_balance(account_id: str, token: str, required_cents: int):
    url = f"https://graph.facebook.com/{FB_API_VERSION}/act_{account_id}"
    params = {"fields": "spend_cap,amount_spent", "access_token": token}
    logger.debug(f"Balance check params: {json.dumps(params)}")
    resp = requests.get(url, params=params)
    logger.debug(f"Balance status {resp.status_code}: {resp.text}")
    js = resp.json()
    cap   = int(js.get("spend_cap", 0))
    spent = int(js.get("amount_spent", 0))
    logger.debug(f"cap={cap}, spent={spent}, required={required_cents}")
    if cap - spent < required_cents:
        raise HTTPException(status_code=402, detail="Fundos insuficientes")

# ─── Pydantic Model ────────────────────────────────────────────────────────────
class CampaignRequest(BaseModel):
    account_id: str
    token: str
    campaign_name: str = ""
    objective: str = "OUTCOME_TRAFFIC"
    content: str = ""
    description: str = ""
    keywords: str = ""
    budget: float = 0.0
    initial_date: str = ""   # "MM/DD/YYYY"
    final_date: str = ""     # "MM/DD/YYYY"
    target_sex: str = ""     # "male" / "female" / ""
    target_age: int = 0
    image: str = ""
    carrossel: List[str] = []
    video: str = Field(default="", alias="video")
    pixel_id: Optional[str] = None

    @field_validator("objective", mode="before")
    def map_objective(cls, v):
        m = {
            "Vendas":            "OUTCOME_SALES",
            "Promover site/app": "OUTCOME_TRAFFIC",
            "Leads":             "OUTCOME_LEADS",
            "Alcance de marca":  "OUTCOME_ENGAGEMENT",  # agora vira ENGAGEMENT
            "Seguidores":        "OUTCOME_ENGAGEMENT",
        }
        return m.get(v, v)

    @field_validator("budget", mode="before")
    def parse_budget(cls, v):
        if isinstance(v, str):
            return float(v.replace("$", "").replace(",", "."))
        return v

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    msg = exc.errors()[0].get("msg", "Erro de validação")
    logger.error(f"Validation error: {msg}")
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": msg})

# ─── Endpoint ──────────────────────────────────────────────────────────────────
@app.post("/create_campaign")
async def create_campaign(req: Request):
    data = CampaignRequest(**await req.json())
    logger.info(f"Iniciando campanha: {data.campaign_name}")

    # 1) Saldo
    total_cents = int(data.budget * 100)
    logger.debug(f"Total budget in cents: {total_cents}")
    check_account_balance(data.account_id, data.token, total_cents)

    # 2) Criar campanha
    camp_payload = {
        "name":                  data.campaign_name,
        "objective":             data.objective,
        "status":                "ACTIVE",
        "access_token":          data.token,
        "special_ad_categories": []
    }
    logger.debug(f"Campanha payload: {json.dumps(camp_payload)}")
    camp_resp = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/act_{data.account_id}/campaigns",
        json=camp_payload
    )
    logger.debug(f"Campanha response {camp_resp.status_code}: {camp_resp.text}")
    if camp_resp.status_code != 200:
        raise HTTPException(status_code=400, detail=extract_fb_error(camp_resp))
    campaign_id = camp_resp.json()["id"]
    logger.info(f"Campaign ID: {campaign_id}")

    # 3) Datas e daily budget
    start_dt  = datetime.strptime(data.initial_date, "%m/%d/%Y")
    end_dt    = datetime.strptime(data.final_date,   "%m/%d/%Y")
    days_diff = (end_dt - start_dt).days
    days      = max(days_diff, 1)
    daily     = total_cents // days
    logger.debug(f"Dias={days_diff}, daily budget={daily} cents")
    if daily < 576:
        rollback_campaign(campaign_id, data.token)
        raise HTTPException(status_code=400, detail="Orçamento diário deve ser ≥ $5.76")

    now_ts   = int(time.time())
    start_ts = max(int(start_dt.timestamp()), now_ts + 60)
    end_ts   = start_ts + days * 86400

    opt_goal      = OBJECTIVE_TO_OPT_GOAL[data.objective]
    billing_event = OBJECTIVE_TO_BILLING_EVENT[data.objective]
    genders       = {"male":[1], "female":[2]}.get(data.target_sex.lower(), [])
    page_id       = get_page_id(data.token)

    # 4) Criar Ad Set
    adset_payload = {
        "name":              f"AdSet {data.campaign_name}",
        "campaign_id":       campaign_id,
        "daily_budget":      daily,
        "billing_event":     billing_event,
        "optimization_goal": opt_goal,
        "bid_amount":        100,
        "targeting": {
            "geo_locations":       {"countries": GLOBAL_COUNTRIES},
            "genders":             genders,
            "age_min":             data.target_age,
            "age_max":             data.target_age,
            "publisher_platforms": PUBLISHER_PLATFORMS
        },
        "start_time":        start_ts,
        "end_time":          end_ts,
        "access_token":      data.token
    }
    logger.debug(f"AdSet payload: {json.dumps(adset_payload, indent=2)}")
    resp_adset = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/act_{data.account_id}/adsets",
        json=adset_payload
    )
    logger.debug(f"AdSet response {resp_adset.status_code}: {resp_adset.text}")
    if resp_adset.status_code != 200:
        rollback_campaign(campaign_id, data.token)
        raise HTTPException(status_code=400, detail=extract_fb_error(resp_adset))
    adset_id = resp_adset.json()["id"]
    logger.info(f"AdSet ID: {adset_id}")

    # 5) Vídeo + thumb
    video_id  = None
    thumbnail = None
    if data.video.strip():
        try:
            video_id  = upload_video_to_fb(data.account_id, data.token, data.video.strip())
            thumbnail = fetch_video_thumbnail(video_id, data.token)
        except Exception as e:
            rollback_campaign(campaign_id, data.token)
            raise HTTPException(status_code=400, detail=str(e))

    # 6) Creative Spec
    default_link    = data.content or "https://www.adstock.ai"
    default_message = data.description

    if data.objective == "OUTCOME_ENGAGEMENT":
        # engajamento → curtir página
        cta = CTA_MAP["OUTCOME_ENGAGEMENT"].copy()
        cta["value"]["page"] = page_id
        if video_id:
            creative_spec = {
                "video_data": {
                    "video_id":       video_id,
                    "message":        default_message,
                    "image_url":      thumbnail,
                    "call_to_action": cta
                }
            }
        else:
            creative_spec = {
                "link_data": {
                    "message":        default_message,
                    "link":           default_link,
                    "picture":        data.image.strip() or "https://via.placeholder.com/1200x628.png?text=Ad",
                    "call_to_action": cta
                }
            }
    # ... (mantém lógica anterior para outros objetivos) ...

    creative_payload = {
        "name":               f"Creative {data.campaign_name}",
        "object_story_spec":  {"page_id": page_id, **creative_spec},
        "access_token":       data.token
    }
    logger.debug(f"Creative payload: {json.dumps(creative_payload, indent=2)}")
    creative_resp = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/act_{data.account_id}/adcreatives",
        json=creative_payload
    )
    logger.debug(f"Creative response {creative_resp.status_code}: {creative_resp.text}")
    if creative_resp.status_code != 200:
        rollback_campaign(campaign_id, data.token)
        raise HTTPException(status_code=400, detail=extract_fb_error(creative_resp))
    creative_id = creative_resp.json()["id"]
    logger.info(f"Creative ID: {creative_id}")

    # 7) Cria Ad
    ad_payload = {
        "name":         f"Ad {data.campaign_name}",
        "adset_id":     adset_id,
        "creative":     {"creative_id": creative_id},
        "status":       "ACTIVE",
        "access_token": data.token
    }
    logger.debug(f"Ad payload: {json.dumps(ad_payload)}")
    ad_resp = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/act_{data.account_id}/ads",
        json=ad_payload
    )
    logger.debug(f"Ad response {ad_resp.status_code}: {ad_resp.text}")
    if ad_resp.status_code != 200:
        rollback_campaign(campaign_id, data.token)
        raise HTTPException(status_code=400, detail=extract_fb_error(ad_resp))
    ad_id = ad_resp.json()["id"]
    logger.info(f"Ad ID: {ad_id}")

    return {
        "status":        "success",
        "campaign_id":   campaign_id,
        "ad_set_id":     adset_id,
        "creative_id":   creative_id,
        "ad_id":         ad_id,
        "campaign_link": f"https://www.facebook.com/adsmanager/manage/campaigns?act={data.account_id}&campaign_ids={campaign_id}"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
