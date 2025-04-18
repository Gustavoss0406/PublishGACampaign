import logging
import sys
import uuid
import re
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, constr
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# ─── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ─── FastAPI setup & CORS ───────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ─── Middleware: limpa JSON mal‑formatado ───────────────────────────────────────
@app.middleware("http")
async def preprocess_request(request: Request, call_next):
    if request.headers.get("content-type", "").startswith("application/json"):
        raw = await request.body()
        text = raw.decode("utf-8", errors="ignore")
        logger.debug(f"Raw request body:\n{text}")

        # 1) corrige '";,' → '",'
        text = text.replace('";,', '",')
        # 2) corrige '";}' ou '";]' → '"}' ou '"]'
        text = re.sub(r'";\s*}', '"}', text)
        text = re.sub(r'";\s*]', '"]', text)
        # 3) remove qualquer ';' imediatamente antes de ',', '}' ou ']'
        text = re.sub(r';(?=\s*[,}\]])', '', text)
        # 4) remove vírgulas finais antes de '}' ou ']'
        text = re.sub(r',(?=\s*[}\]])', '', text)

        logger.debug(f"Cleaned request body:\n{text}")

        async def receive():
            return {"type": "http.request", "body": text.encode("utf-8")}
        request._receive = receive

    return await call_next(request)

# ─── Modelo Pydantic ─────────────────────────────────────────────────────────────
class CampaignPayload(BaseModel):
    refresh_token: str
    campaign_name: str
    campaign_description: str
    objective: str
    cover_photo: str
    final_url: str
    keyword1: str
    keyword2: str
    keyword3: str
    budget: constr(pattern=r'^\$\d+(\.\d{1,2})?$')  # ex: "$32.50"
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
        # remove '$' e converte para float
        return float(v.replace("$", "")) if isinstance(v, str) else v

    @field_validator("start_date", "end_date", mode="before")
    def validate_dates(cls, v):
        try:
            return datetime.strptime(v, "%m/%d/%Y").strftime("%Y%m%d")
        except Exception:
            raise ValueError("Date must be MM/DD/YYYY")

    @field_validator("cover_photo", "final_url", mode="before")
    def clean_urls(cls, v):
        v = v.strip().rstrip(" ;")
        if not v or v.lower() == "null":
            return ""
        if not re.match(r"^https?://", v):
            v = "http://" + v
        return v

# ─── Endpoint principal ─────────────────────────────────────────────────────────
@app.post("/create_campaign")
async def create_campaign(request_data: CampaignPayload, background_tasks: BackgroundTasks):
    # 1) validações iniciais
    if not request_data.final_url:
        raise HTTPException(400, "Campo final_url é obrigatório")
    if request_data.budget < 1.0:
        raise HTTPException(422, "Budget muito baixo. Mínimo $1.00")

    # 2) credenciais fictícias Google Ads (apenas testes)
    creds = {
        "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
        "client_id":       "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com",
        "client_secret":   "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",
        "refresh_token":   request_data.refresh_token,
        "use_proto_plus":  True,
    }

    # 3) inicializa GoogleAdsClient
    try:
        client = GoogleAdsClient.load_from_dict(creds, version="v16")
        svc = client.get_service("CustomerService")
        login_cid = svc.list_accessible_customers().resource_names[0].split("/")[-1]
        client.login_customer_id = login_cid
    except Exception:
        raise HTTPException(400, "Falha de autenticação no Google Ads")

    # 4) agendar criação em background
    background_tasks.add_task(_create_campaign_task, client, request_data)
    return {"status": "processing"}

# ─── Função que executa em background ───────────────────────────────────────────
def _create_campaign_task(client: GoogleAdsClient, data: CampaignPayload):
    cid = client.login_customer_id

    # ─── cria budget ─────────────────────────────────────────────────────────────
    budget_svc = client.get_service("CampaignBudgetService")
    op_budget = client.get_type("CampaignBudgetOperation")
    b = op_budget.create
    b.name = f"{data.campaign_name} Budget {uuid.uuid4()}"
    b.amount_micros = int(data.budget * 1_000_000)
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    try:
        res_budget = budget_svc.mutate_campaign_budgets(
            customer_id=cid, operations=[op_budget]
        )
        budget_res = res_budget.results[0].resource_name
    except GoogleAdsException as e:
        for err in e.failure.errors:
            if "too low" in err.message.lower():
                logger.error("💰 Budget muito baixo: %s", err.message)
                return
        logger.exception("Erro ao criar budget no Google Ads")
        return

    # ─── cria campanha ───────────────────────────────────────────────────────────
    camp_svc = client.get_service("CampaignService")
    op_camp = client.get_type("CampaignOperation")
    c = op_camp.create
    c.name = data.campaign_name
    c.campaign_budget = budget_res
    c.status = client.enums.CampaignStatusEnum.PAUSED
    c.start_date = data.start_date
    c.end_date   = data.end_date

    # define estratégia de lance por objective
    obj = data.objective.strip().lower()
    if obj in {"vendas", "leads", "promover site/app"}:
        ts = client.get_type("TargetSpend")()
        c.target_spend.CopyFrom(ts)
    elif obj == "alcance de marca":
        tis = client.get_type("TargetImpressionShare")()
        tis.location = client.enums.TargetImpressionShareLocationEnum.ANYWHERE_ON_PAGE
        tis.location_fraction_micros = 1_000_000
        c.target_impression_share.CopyFrom(tis)
    else:
        logger.error("Objetivo inválido recebido: %s", data.objective)
        return

    try:
        res_camp = camp_svc.mutate_campaigns(customer_id=cid, operations=[op_camp])
        logger.info("✅ Campanha criada: %s", res_camp.results[0].resource_name)
    except GoogleAdsException as e:
        logger.exception("Erro ao criar campanha no Google Ads")
