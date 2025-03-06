import os
import logging
import json
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
import uvicorn

# Google Ads
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_request_and_response(request: Request, call_next):
    body_bytes = await request.body()
    try:
        body_str = body_bytes.decode("utf-8")
    except Exception:
        body_str = str(body_bytes)
    logging.debug(f"Raw request body: {body_str}")
    
    response = await call_next(request)
    logging.debug(f"Response status code: {response.status_code}")
    return response

# -------------------- MODELOS --------------------
class CampaignCreationRequest(BaseModel):
    refresh_token: str
    objective: str
    cover_photo: str
    campaign_name: str
    campaign_description: str
    keyword1: Optional[str] = None
    keyword2: Optional[str] = None
    keyword3: Optional[str] = None
    budget: str
    start_date: str
    end_date: str
    price_model: str
    campaign_type: str = "SEARCH"
    audience_gender: Optional[str] = None
    audience_min_age: Optional[int] = None
    audience_max_age: Optional[int] = None
    devices: List[str] = []

    @validator("devices", pre=True, always=True)
    def filter_empty_devices(cls, v):
        if isinstance(v, list):
            return [item for item in v if item and item.strip()]
        return v

class CampaignCreationResponse(BaseModel):
    customer_id: str
    campaign_budget_resource: str
    campaign_resource: str
    ad_group_resource: str
    keywords_resources: List[str]
    criteria_resources: List[str]
    objective: str
    cover_photo: str
    campaign_description: str

# -------------------- CONFIG --------------------
DEVELOPER_TOKEN = "D4yv61IQ8R0JaE5dxrd1Uw"
CLIENT_ID = "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI = "https://app.adstock.ai/dashboard"

# -------------------- FUNÇÕES AUXILIARES --------------------
def initialize_google_ads_client(refresh_token: str) -> GoogleAdsClient:
    logging.debug("Inicializando GoogleAdsClient...")
    config = {
        "developer_token": DEVELOPER_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    try:
        client = GoogleAdsClient.load_from_dict(config, version="v12")
        logging.info("GoogleAdsClient inicializado com sucesso.")
        return client
    except Exception as e:
        logging.error(f"Erro na inicialização do GoogleAdsClient: {e}")
        raise HTTPException(status_code=500, detail=f"Erro na inicialização do GoogleAdsClient: {e}")

def get_accessible_customers(client: GoogleAdsClient) -> List[str]:
    logging.debug("Obtendo contas acessíveis via refresh token...")
    try:
        customer_service = client.get_service("CustomerService")
        accessible_customers = customer_service.list_accessible_customers()
        customers = [resource_name.split("/")[-1] for resource_name in accessible_customers.resource_names]
        logging.info(f"Contas acessíveis obtidas: {customers}")
        return customers
    except Exception as e:
        logging.error(f"Erro ao obter contas acessíveis: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao obter contas acessíveis: {e}")

def convert_budget_to_micros(budget_str: str) -> int:
    try:
        numeric_str = budget_str.replace("$", "").strip()
        budget_value = float(numeric_str)
        return int(budget_value * 1_000_000)
    except Exception as e:
        logging.error(f"Erro ao converter budget: {e}")
        raise HTTPException(status_code=400, detail=f"Erro ao converter o budget: {e}")

def combine_age_ranges(min_age: int, max_age: int) -> str:
    return f"AGE_RANGE_{min_age}_{max_age}"

# -------------------- FUNÇÃO PRINCIPAL DE MUTATE --------------------
def mutate_all_in_one(
    client: GoogleAdsClient,
    customer_id: str,
    request_data: CampaignCreationRequest,
    budget_micros: int
):
    """
    Cria Budget, Campaign, AdGroup, Keywords e Criteria em uma só chamada mutate.
    Usa IDs negativos para referenciar recursos recém-criados.
    """
    mutate_operations = []

    # 1) CampaignBudget (ID negativo -1)
    budget_op = client.get_type("MutateOperation")
    budget_op.campaign_budget_operation.create.name = f"Orcamento_{str(uuid.uuid4())[:8]}"
    budget_op.campaign_budget_operation.create.amount_micros = budget_micros
    budget_op.campaign_budget_operation.create.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget_op.campaign_budget_operation.create.resource_name = f"customers/{customer_id}/campaignBudgets/-1"
    mutate_operations.append(budget_op)

    # 2) Campaign (ID negativo -2), referenciando o budget -1
    campaign_op = client.get_type("MutateOperation")
    c = campaign_op.campaign_operation.create
    c.resource_name = f"customers/{customer_id}/campaigns/-2"
    c.name = request_data.campaign_name
    c.status = client.enums.CampaignStatusEnum.ENABLED
    if request_data.campaign_type.upper() == "DISPLAY":
        c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    else:
        c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    c.campaign_budget = f"customers/{customer_id}/campaignBudgets/-1"
    c.start_date = request_data.start_date
    c.end_date = request_data.end_date

    if request_data.price_model.upper() == "CPA":
        c.target_cpa.target_cpa_micros = 1_000_000
        c.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.TARGET_CPA
    else:
        c.manual_cpc.enhanced_cpc_enabled = False
        c.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.MANUAL_CPC
    mutate_operations.append(campaign_op)

    # 3) AdGroup (ID negativo -3), referenciando a campaign -2
    adgroup_op = client.get_type("MutateOperation")
    ag = adgroup_op.ad_group_operation.create
    ag.resource_name = f"customers/{customer_id}/adGroups/-3"
    ag.name = f"Grupo_{str(uuid.uuid4())[:8]}"
    ag.campaign = f"customers/{customer_id}/campaigns/-2"
    ag.cpc_bid_micros = 500_000
    ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    mutate_operations.append(adgroup_op)

    # 4) Keywords, referenciando ad group -3
    keywords = [request_data.keyword1, request_data.keyword2, request_data.keyword3]
    for kw in keywords:
        if kw:
            kw_op = client.get_type("MutateOperation")
            criterion = kw_op.ad_group_criterion_operation.create
            criterion.resource_name = f"customers/{customer_id}/adGroupCriteria/-3~{uuid.uuid4()}"
            criterion.ad_group = f"customers/{customer_id}/adGroups/-3"
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            criterion.keyword.text = kw
            criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
            mutate_operations.append(kw_op)

    # 5) Criteria (devices, demografia) referenciando a campaign -2
    if request_data.campaign_type.upper() == "DISPLAY":
        # Gênero
        if request_data.audience_gender:
            gender_op = client.get_type("MutateOperation")
            gc = gender_op.campaign_criterion_operation.create
            gc.resource_name = f"customers/{customer_id}/campaignCriteria/-2~{uuid.uuid4()}"
            gc.campaign = f"customers/{customer_id}/campaigns/-2"
            gc.gender.type_ = getattr(client.enums.GenderTypeEnum, request_data.audience_gender.upper(), None)
            mutate_operations.append(gender_op)
        # Idade
        if request_data.audience_min_age is not None and request_data.audience_max_age is not None:
            age_op = client.get_type("MutateOperation")
            ac = age_op.campaign_criterion_operation.create
            ac.resource_name = f"customers/{customer_id}/campaignCriteria/-2~{uuid.uuid4()}"
            ac.campaign = f"customers/{customer_id}/campaigns/-2"
            age_range_str = combine_age_ranges(request_data.audience_min_age, request_data.audience_max_age)
            age_enum = getattr(client.enums.AgeRangeTypeEnum, age_range_str.upper(), None)
            if age_enum is not None:
                ac.age_range.type_ = age_enum
            mutate_operations.append(age_op)

    # Dispositivos (sempre aplicados, mas só faz sentido se for SEARCH, pois DISPLAY não necessariamente usa device criterion)
    # Aqui, para simplificar, aplicamos device para qualquer campaign_type:
    for device in request_data.devices:
        dev_op = client.get_type("MutateOperation")
        cc = dev_op.campaign_criterion_operation.create
        cc.resource_name = f"customers/{customer_id}/campaignCriteria/-2~{uuid.uuid4()}"
        cc.campaign = f"customers/{customer_id}/campaigns/-2"
        cc.device.type_ = getattr(client.enums.DeviceEnum, device.upper(), None)
        mutate_operations.append(dev_op)

    # Executa a chamada mutate com todos os operations
    ga_service = client.get_service("GoogleAdsService")
    try:
        response = ga_service.mutate(customer_id=customer_id, mutate_operations=mutate_operations)
        return response
    except GoogleAdsException as ex:
        raise HTTPException(status_code=500, detail=f"Erro no mutate all-in-one: {ex}")

# -------------------- ENDPOINT --------------------
@app.post("/create_campaign", response_model=CampaignCreationResponse)
async def create_campaign_endpoint(request_data: CampaignCreationRequest):
    try:
        logging.debug(f"Body recebido: {json.dumps(request_data.dict(), indent=4)}")
        client = initialize_google_ads_client(request_data.refresh_token)
        customers = get_accessible_customers(client)
        if not customers:
            raise HTTPException(status_code=500, detail="Nenhuma conta acessível encontrada.")
        customer_id = customers[0]

        budget_micros = convert_budget_to_micros(request_data.budget)

        # Fazemos apenas UMA chamada mutate para tudo
        response = await asyncio.to_thread(mutate_all_in_one, client, customer_id, request_data, budget_micros)

        # Precisamos extrair os resource_names criados (budget -1, campaign -2, adgroup -3, etc.)
        # O response contém todos os MutateOperationResponse
        # Vamos procurar por cada tipo:
        budget_resource = ""
        campaign_resource = ""
        ad_group_resource = ""
        keywords_resources = []
        criteria_resources = []

        for result in response.mutate_operation_responses:
            if result.campaign_budget_result.resource_name.endswith("/-1"):
                budget_resource = result.campaign_budget_result.resource_name
            elif result.campaign_result.resource_name.endswith("/-2"):
                campaign_resource = result.campaign_result.resource_name
            elif result.ad_group_result.resource_name.endswith("/-3"):
                ad_group_resource = result.ad_group_result.resource_name
            elif result.ad_group_criterion_result.resource_name:
                # Pode ser keyword
                keywords_resources.append(result.ad_group_criterion_result.resource_name)
            elif result.campaign_criterion_result.resource_name:
                criteria_resources.append(result.campaign_criterion_result.resource_name)

        # Monta o resultado
        result = CampaignCreationResponse(
            customer_id=customer_id,
            campaign_budget_resource=budget_resource,
            campaign_resource=campaign_resource,
            ad_group_resource=ad_group_resource,
            keywords_resources=keywords_resources,
            criteria_resources=criteria_resources,
            objective=request_data.objective,
            cover_photo=request_data.cover_photo,
            campaign_description=request_data.campaign_description
        )
        logging.info(f"Campanha criada com sucesso (all-in-one): {json.dumps(result.dict(), ensure_ascii=False)}")
        return result

    except Exception as e:
        logging.exception("Erro inesperado ao criar a campanha all-in-one.")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
