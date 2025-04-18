import logging
import sys
import uuid
import re
from datetime import datetime, date
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ─── Middleware para limpeza de JSON mal‑formatado ──────────────────────────────
@app.middleware("http")
async def preprocess_request(request: Request, call_next):
    if request.headers.get("content-type", "").startswith("application/json"):
        raw = await request.body()
        text = raw.decode("utf-8", errors="ignore")
        logger.debug(f"Raw request body:\n{text}")

        # 1) corrige '";,' → '",'
        text = re.sub(r'";\s*,', '",', text)
        # 2) corrige '";}' → '"}'
        text = re.sub(r'";\s*}', '"}', text)
        text = re.sub(r'";\s*]', '"]', text)
        # 3) remove semicolons antes de vírgula/fechamento
        text = re.sub(r';+(?=\s*[,}\]])', '', text)
        # 4) remove vírgulas finais antes de fechamento
        text = re.sub(r',+(?=\s*[}\]])', '', text)

        logger.debug(f"Cleaned request body:\n{text}")

        # injeta JSON limpo
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
            return datetime.strptime(v, "%m/%d/%Y").date()
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

@app.post("/create_campaign")
async def create_campaign_endpoint(payload: CampaignPayload, background_tasks: BackgroundTasks):
    # valida budget mínimo $1.00
    if payload.budget < 1.0:
        raise HTTPException(422, "Budget muito baixo. Mínimo $1.00")

    # configurações fictícias do Google Ads
    creds = {
        "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
        "client_id": "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com",
        "client_secret": "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",
        "refresh_token": payload.refresh_token,
        "use_proto_plus": True,
    }
    try:
        client = GoogleAdsClient.load_from_dict(creds, version="v16")
        login_cid = client.get_service("CustomerService").list_accessible_customers().resource_names[0].split("/")[-1]
        client.login_customer_id = login_cid
    except Exception:
        raise HTTPException(400, "Falha de autenticação no Google Ads")

    # agendar criação em background
    background_tasks.add_task(create_campaign, client, payload)
    return {"message": "Criação de campanha agendada em background."}

def create_campaign(client: GoogleAdsClient, p: CampaignPayload):
    cid = client.login_customer_id
    # 1) cria budget
    budget_svc = client.get_service("CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    b = op.create
    b.name = f"{p.campaign_name} Budget {uuid.uuid4()}"
    b.amount_micros = int(p.budget * 1_000_000)
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    try:
        res = budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[op])
        budget_res = res.results[0].resource_name
    except GoogleAdsException as e:
        for err in e.failure.errors:
            if "Too low" in err.message:
                print("Budget muito baixo para criar.")
                return
        print("Erro ao criar budget:", e)
        return

    # 2) cria campanha
    camp_svc = client.get_service("CampaignService")
    op2 = client.get_type("CampaignOperation")
    c = op2.create
    c.name = p.campaign_name
    c.campaign_budget = budget_res
    c.status = client.enums.CampaignStatusEnum.PAUSED
    c.start_date = p.start_date.strftime("%Y-%m-%d")
    c.end_date = p.end_date.strftime("%Y-%m-%d")

    # define bidding based on objective
    obj = p.objective.lower()
    if obj in ["vendas", "leads", "promover site/app"]:
        ts = client.get_type("TargetSpend")()
        c.target_spend.CopyFrom(ts)
    elif obj == "alcance de marca":
        tis = client.get_type("TargetImpressionShare")()
        tis.location = client.enums.TargetImpressionShareLocationEnum.ANYWHERE_ON_PAGE
        tis.location_fraction_micros = 1_000_000
        c.target_impression_share.CopyFrom(tis)
    else:
        print("Objetivo inválido:", p.objective)
        return

    try:
        resp = camp_svc.mutate_campaigns(customer_id=cid, operations=[op2])
        print("Campanha criada:", resp.results[0].resource_name)
    except GoogleAdsException as e:
        print("Erro ao criar campanha:", e)
