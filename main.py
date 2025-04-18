import logging
import sys
import uuid
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# ─── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s",
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

# ─── Middleware de pré‑processamento ────────────────────────────────────────────
@app.middleware("http")
async def preprocess_request(request: Request, call_next):
    raw = await request.body()
    text = raw.decode("utf-8", errors="ignore")
    logger.debug(f"Raw request body (pre-clean):\n{text}")

    # 0) Remove o ';' que vier dentro das aspas antes de vírgula ou fechamento
    text = re.sub(r'";\s*(?=[,}\]])', '",', text)
    # 1) Remove qualquer ';' imediatamente antes de vírgula, chave ou colchete
    text = re.sub(r';+(?=\s*[,}\]])', '', text)
    # 2) Remove vírgulas finais antes de '}' ou ']'
    text = re.sub(r',+(?=\s*[}\]])', '', text)

    logger.debug(f"Cleaned request body (post-clean):\n{text}")

    async def receive():
        return {"type": "http.request", "body": text.encode("utf-8")}
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
        return int(float(v.replace("$", ""))) if isinstance(v, str) else v

    @field_validator("audience_min_age", "audience_max_age", mode="before")
    def convert_age(cls, v):
        return int(v)

    @field_validator("cover_photo", "final_url", mode="before")
    def clean_urls(cls, v):
        v = v.strip().rstrip(" ;")
        if not v or v.lower() == "null":
            return ""
        if not re.match(r"^https?://", v):
            v = "http://" + v
        return v

# ─── Helpers Google Ads ──────────────────────────────────────────────────────────
def format_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%m/%d/%Y").strftime("%Y%m%d")

def get_customer_id(client: GoogleAdsClient) -> str:
    svc = client.get_service("CustomerService")
    res = svc.list_accessible_customers()
    if not res.resource_names:
        raise Exception("Nenhum customer acessível")
    return res.resource_names[0].split("/")[-1]

# ─── Background task: cria só a campanha (budget já existe) ────────────────────
def create_campaign_bg(client: GoogleAdsClient, data: CampaignRequest, budget_res: str):
    try:
        logger.info(">> [BG] Criando campanha no Google Ads")
        cid = client.login_customer_id
        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        camp = op.create
        camp.name = data.campaign_name

        is_display = data.campaign_type.upper() == "DISPLAY"
        if is_display:
            camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
            max_conv = client.get_type("MaximizeConversions")
            camp.maximize_conversions.CopyFrom(max_conv)
        else:
            camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
            camp.manual_cpc.enhanced_cpc_enabled = True

        camp.status = client.enums.CampaignStatusEnum.PAUSED
        camp.campaign_budget = budget_res
        camp.start_date = format_date(data.start_date)
        camp.end_date   = format_date(data.end_date)

        resp = svc.mutate_campaigns(customer_id=cid, operations=[op])
        logger.info(f"[BG] Campanha criada: {resp.results[0].resource_name}")
    except Exception:
        logger.exception("‼️ [BG] Erro dentro de create_campaign_bg")

# ─── Endpoint principal ─────────────────────────────────────────────────────────
@app.post("/create_campaign")
async def create_campaign(request_data: CampaignRequest, background_tasks: BackgroundTasks):
    if not request_data.final_url:
        raise HTTPException(400, "Campo final_url é obrigatório")

    # Tokens fictícios (em produção, use env vars/BaseSettings)
    DEV_TOKEN = "D4yv61IQ8R0JaE5dxrd1Uw"
    CID       = "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com"
    CSECRET   = "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"

    cfg = {
        "developer_token": DEV_TOKEN,
        "client_id":       CID,
        "client_secret":   CSECRET,
        "refresh_token":   request_data.refresh_token,
        "use_proto_plus":  True,
    }

    try:
        client = GoogleAdsClient.load_from_dict(cfg)
        login_cid = get_customer_id(client)
        client.login_customer_id = login_cid
    except Exception:
        logger.exception("Falha na autenticação Google Ads")
        raise HTTPException(400, "Erro de autenticação no Google Ads")

    # Cria orçamento síncrono para capturar “Too low”
    budget_svc = client.get_service("CampaignBudgetService")
    budget_op = client.get_type("CampaignBudgetOperation")
    budget = budget_op.create
    budget.name = f"{request_data.campaign_name} Budget {uuid.uuid4()}"
    budget.amount_micros = request_data.budget * 1_000_000
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    try:
        resp_budget = budget_svc.mutate_campaign_budgets(
            customer_id=client.login_customer_id,
            operations=[budget_op],
        )
    except GoogleAdsException as ex:
        for err in ex.failure.errors:
            if "Too low" in err.message:
                raise HTTPException(400, "Orçamento diário muito baixo. Aumente o valor.")
        logger.exception("Erro inesperado ao criar budget")
        raise HTTPException(500, "Erro ao criar orçamento no Google Ads")

    budget_res = resp_budget.results[0].resource_name
    logger.info(f"Budget criado: {budget_res}")

    background_tasks.add_task(create_campaign_bg, client, request_data, budget_res)
    return {"status": "processing"}
