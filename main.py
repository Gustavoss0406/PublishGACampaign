import logging
import sys
import os
import time
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from typing import List

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

# ─── Helpers ────────────────────────────────────────────────────────────────────
def extract_fb_error(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return err.get("error_user_msg") or err.get("message") or resp.text
    except:
        return resp.text or "Erro desconhecido"

def rollback_campaign(campaign_id: str, token: str):
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{campaign_id}"
    try:
        requests.delete(url, params={"access_token": token})
        logger.info(f"Rollback: campanha {campaign_id} deletada")
    except:
        logger.exception("Falha no rollback")

def upload_video_to_fb(account_id: str, token: str, video_url: str) -> str:
    url = f"https://graph.facebook.com/{FB_API_VERSION}/act_{account_id}/advideos"
    resp = requests.post(url, data={"file_url": video_url, "access_token": token})
    logger.debug(f"Upload vídeo status {resp.status_code}: {resp.text}")
    if resp.status_code != 200:
        raise Exception(f"Erro ao enviar vídeo: {extract_fb_error(resp)}")
    vid = resp.json().get("id")
    if not vid:
        raise Exception("Facebook não retornou video_id")
    return vid

def fetch_video_thumbnail(video_id: str, token: str) -> str:
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{video_id}/thumbnails"
    for _ in range(5):
        resp = requests.get(url, params={"access_token": token})
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                return data[0]["uri"]
        logger.debug("Thumbnail não disponível ainda, aguardando...")
        time.sleep(2)
    raise Exception("Não foi possível obter thumbnail do vídeo")

def get_page_id(token: str) -> str:
    resp = requests.get(f"https://graph.facebook.com/{FB_API_VERSION}/me/accounts",
                        params={"access_token": token})
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Erro ao buscar páginas")
    data = resp.json().get("data", [])
    if not data:
        raise HTTPException(status_code=533, detail="Nenhuma página disponível")
    return data[0]["id"]

def check_account_balance(account_id: str, token: str, required_cents: int):
    resp = requests.get(f"https://graph.facebook.com/{FB_API_VERSION}/act_{account_id}",
                        params={"fields": "spend_cap,amount_spent", "access_token": token})
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Erro ao verificar saldo")
    js = resp.json()
    cap = int(js.get("spend_cap", 0))
    spent = int(js.get("amount_spent", 0))
    if cap - spent < required_cents:
        raise HTTPException(status_code=402, detail="Fundos insuficientes")

# ─── Modelos Pydantic ───────────────────────────────────────────────────────────
class CampaignRequest(BaseModel):
    account_id:   str
    token:        str
    campaign_name: str = ""
    objective:     str = "OUTCOME_TRAFFIC"
    content:       str = ""
    description:   str = ""
    keywords:      str = ""
    budget:        float = 0.0
    initial_date:  str = ""
    final_date:    str = ""
    target_sex:    str = ""
    target_age:    int = 0
    image:         str = ""
    carrossel:     List[str] = []
    video:         str = Field(default="", alias="video")

    @field_validator("objective", mode="before")
    def map_objective(cls, v):
        m = {
            "Vendas": "OUTCOME_SALES",
            "Promover site/app": "OUTCOME_TRAFFIC",
            "Leads": "OUTCOME_LEADS",
            "Alcance de marca": "OUTCOME_AWARENESS"
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
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": msg})

# ─── Endpoint principal ───────────────────────────────────────────────────────
@app.post("/create_campaign")
async def create_campaign(req: Request):
    data = CampaignRequest(**await req.json())
    logger.info(f"Iniciando campanha: {data.campaign_name}")

    # 1) Verifica saldo
    total_cents = int(data.budget * 100)
    check_account_balance(data.account_id, data.token, total_cents)

    # 2) Cria campanha
    camp_resp = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/act_{data.account_id}/campaigns",
        json={
            "name": data.campaign_name,
            "objective": data.objective,
            "status": "ACTIVE",
            "access_token": data.token,
            "special_ad_categories": []
        }
    )
    if camp_resp.status_code != 200:
        raise HTTPException(status_code=400, detail=extract_fb_error(camp_resp))
    camp_id = camp_resp.json()["id"]

    # 3) Criação do Ad Set (corrigido para todos os objetivos)
    start_dt = datetime.strptime(data.initial_date, "%m/%d/%Y")
    end_dt   = datetime.strptime(data.final_date,   "%m/%d/%Y")
    days     = max((end_dt - start_dt).days, 1)
    daily    = total_cents // days

    if daily < 576:
        rollback_campaign(camp_id, data.token)
        raise HTTPException(status_code=400, detail="Orçamento diário deve ser ≥ $5.76")
    if (end_dt - start_dt) < timedelta(hours=24):
        rollback_campaign(camp_id, data.token)
        raise HTTPException(status_code=400, detail="Duração mínima 24h")

    # otimização e cobrança por objetivo
    if data.objective == "OUTCOME_AWARENESS":
        optimization_goal = "REACH"
        billing_event    = "IMPRESSIONS"
    else:
        optimization_goal = "LINK_CLICKS"
        billing_event    = "LINK_CLICKS"

    page_id = get_page_id(data.token)
    promoted_object = {"page_id": page_id}

    adset_payload = {
        "name":              f"AdSet {data.campaign_name}",
        "campaign_id":       camp_id,
        "daily_budget":      daily,
        "billing_event":     billing_event,
        "optimization_goal": optimization_goal,
        "bid_amount":        100,
        "targeting": {
            "geo_locations":      {"countries": GLOBAL_COUNTRIES},
            "genders":            {"male":[1], "female":[2]}.get(data.target_sex.lower(), []),
            "age_min":            data.target_age,
            "age_max":            data.target_age,
            "publisher_platforms": PUBLISHER_PLATFORMS
        },
        "start_time":       int(start_dt.timestamp()),
        "end_time":         int(end_dt.timestamp()),
        "promoted_object":  promoted_object,
        "access_token":     data.token
    }
    adset_resp = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/act_{data.account_id}/adsets",
        json=adset_payload
    )
    if adset_resp.status_code != 200:
        rollback_campaign(camp_id, data.token)
        raise HTTPException(status_code=400, detail=extract_fb_error(adset_resp))
    adset_id = adset_resp.json()["id"]

    # 4) Upload do vídeo (se houver) + thumbnail
    video_id = None
    thumbnail = None
    if data.video.strip():
        url = data.video.strip().rstrip(";,")
        try:
            video_id = upload_video_to_fb(data.account_id, data.token, url)
            thumbnail = fetch_video_thumbnail(video_id, data.token)
        except Exception as e:
            rollback_campaign(camp_id, data.token)
            raise HTTPException(status_code=400, detail=str(e))

    # 5) Monta spec do Creative
    default_link    = data.content or "https://www.adstock.ai"
    default_message = data.description

    if video_id:
        creative_spec = {
            "video_data": {
                "video_id":       video_id,
                "message":        default_message,
                "image_url":      thumbnail,
                "call_to_action": {"type":"LEARN_MORE","value":{"link":default_link}}
            }
        }
    elif data.image.strip():
        creative_spec = {
            "link_data": {
                "message": default_message,
                "link":    default_link,
                "picture": data.image.strip()
            }
        }
    elif any(u.strip() for u in data.carrossel):
        child = [
            {"link": default_link, "picture": u, "message": default_message}
            for u in data.carrossel if u.strip()
        ]
        creative_spec = {"link_data": {"child_attachments": child,
                                       "message": default_message,
                                       "link": default_link}}
    else:
        creative_spec = {
            "link_data": {
                "message": default_message,
                "link":    default_link,
                "picture": "https://via.placeholder.com/1200x628.png?text=Ad+Placeholder"
            }
        }

    # 6) Cria Ad Creative
    creative_resp = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/act_{data.account_id}/adcreatives",
        json={
            "name": f"Creative {data.campaign_name}",
            "object_story_spec": {"page_id": page_id, **creative_spec},
            "access_token": data.token
        }
    )
    if creative_resp.status_code != 200:
        rollback_campaign(camp_id, data.token)
        raise HTTPException(status_code=400, detail=extract_fb_error(creative_resp))
    creative_id = creative_resp.json()["id"]

    # 7) Cria o Ad
    ad_resp = requests.post(
        f"https://graph.facebook.com/{FB_API_VERSION}/act_{data.account_id}/ads",
        json={
            "name":         f"Ad {data.campaign_name}",
            "adset_id":     adset_id,
            "creative":     {"creative_id": creative_id},
            "status":       "ACTIVE",
            "access_token": data.token
        }
    )
    if ad_resp.status_code != 200:
        rollback_campaign(camp_id, data.token)
        raise HTTPException(status_code=400, detail=extract_fb_error(ad_resp))
    ad_id = ad_resp.json()["id"]

    # 8) Retorno final
    return {
        "status":        "success",
        "campaign_id":   camp_id,
        "ad_set_id":     adset_id,
        "creative_id":   creative_id,
        "ad_id":         ad_id,
        "campaign_link": f"https://www.facebook.com/adsmanager/manage/campaigns?act={data.account_id}&campaign_ids={camp_id}"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
