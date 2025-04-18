import logging
import sys
import uuid
import re
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from google.ads.googleads.client import GoogleAdsClient
from pydantic import BaseModel, field_validator, ConfigDict

# ─── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ─── FastAPI app & CORS ─────────────────────────────────────────────────────────
app = FastAPI()

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
        if not re.match(r"^https?://", v):
            v = "http://" + v
        return v

# ─── Google Ads helpers ──────────────────────────────────────────────────────────
def get_customer_id(client: GoogleAdsClient) -> str:
    svc = client.get_service("CustomerService")
    res = svc.list_accessible_customers()
    if not res.resource_names:
        raise Exception("Nenhum customer acessível")
    return res.resource_names[0].split("/")[-1]

# ─── Background task ───────────────────────────────────────────────────────────
def process_campaign_task(client: GoogleAdsClient, data: CampaignRequest):
    logger.info(">> Iniciando criação de campanha no Google Ads")
    customer_id = client.login_customer_id

    # 1) Criar budget
    budget_service = client.get_service("CampaignBudgetService")
    budget_op = client.get_type("CampaignBudgetOperation")
    budget = budget_op.create
    budget.name = f"{data.campaign_name} Budget {uuid.uuid4()}"
    budget.amount_micros = data.budget * 1_000_000
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    resp_budget = budget_service.mutate_campaign_budgets(
        customer_id=customer_id,
        operations=[budget_op],
    )
    budget_resource = resp_budget.results[0].resource_name
    logger.info(f"Budget criado: {budget_resource}")

    # 2) Criar campanha
    campaign_service = client.get_service("CampaignService")
    camp_op = client.get_type("CampaignOperation")
    campaign = camp_op.create
    campaign.name = data.campaign_name
    # channel type
    if data.campaign_type.upper() == "DISPLAY":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    else:
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.campaign_budget = budget_resource
    # Manual CPC
    campaign.manual_cpc.CopyFrom(client.get_type("ManualCpc")())
    # Datas
    campaign.start_date = format_date(data.start_date)
    campaign.end_date   = format_date(data.end_date)

    resp_camp = campaign_service.mutate_campaigns(
        customer_id=customer_id,
        operations=[camp_op],
    )
    campaign_resource = resp_camp.results[0].resource_name
    logger.info(f"Campanha criada: {campaign_resource}")
    logger.info(">> Fim da criação de campanha.")

# ─── Main endpoint ─────────────────────────────────────────────────────────────
@app.post("/create_campaign")
async def create_campaign(request_data: CampaignRequest, background_tasks: BackgroundTasks):
    logger.debug("Parsed request data:\n%s", json.dumps(request_data.model_dump(), indent=2))

    if not request_data.final_url:
        logger.error("Validation error: final_url is empty")
        raise HTTPException(status_code=400, detail="Campo final_url é obrigatório")

    # Tokens fictícios (substitua pelos seus em produção!)
    DEV_TOKEN = "D4yv61IQ8R0JaE5dxrd1Uw"
    CID       = "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com"
    CSECRET   = "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"

    # Monta config e inicializa client
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
    except Exception:
        logger.exception("Falha ao inicializar GoogleAdsClient")
        raise HTTPException(
            status_code=400,
            detail="Falha ao inicializar GoogleAdsClient: verifique client_id/secret e refresh_token"
        )

    try:
        login_cid = get_customer_id(client)
        client.login_customer_id = login_cid
        logger.info(f"Usando login_customer_id = {login_cid}")
    except Exception:
        logger.exception("Erro ao obter login_customer_id")
        raise HTTPException(status_code=400, detail="Erro ao obter login_customer_id do Google Ads")

    # Agenda execução em background
    background_tasks.add_task(process_campaign_task, client, request_data)
    return {"status": "processing"}
