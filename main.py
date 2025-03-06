import os
import logging
import json
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
import uvicorn

# Google Ads
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# -------------------- CONFIGURAÇÃO DE LOGGING --------------------
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

# -------------------- INICIALIZAÇÃO DA APLICAÇÃO --------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ajuste conforme necessário
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware para logar o corpo da requisição (raw) e o status code da resposta
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
    objective: str                     # Ex: "Leads", "Vendas", "Alcance de marca", "Trafego em site"
    cover_photo: str                   # URL ou caminho da foto de capa
    campaign_name: str                 # Nome da campanha
    campaign_description: str          # Descrição da campanha
    # Keywords recebidas separadamente
    keyword1: Optional[str] = None
    keyword2: Optional[str] = None
    keyword3: Optional[str] = None
    budget: str                        # Recebido no formato "$100"
    start_date: str                    # Data de início no formato "YYYYMMDD"
    end_date: str                      # Data de fim no formato "YYYYMMDD"
    price_model: str                   # "CPA" ou "CPC"
    campaign_type: str = "SEARCH"      # "SEARCH" ou "DISPLAY"
    # Segmentação demográfica (usados se campaign_type for DISPLAY)
    audience_gender: Optional[str] = None       # Ex: "FEMALE", "MALE"
    audience_min_age: Optional[int] = None
    audience_max_age: Optional[int] = None
    devices: List[str] = []

    @validator("devices", pre=True, always=True)
    def filter_empty_devices(cls, v):
        # Remove entradas vazias ou espaços
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

# -------------------- CONFIGURAÇÕES FIXAS --------------------
DEVELOPER_TOKEN = "D4yv61IQ8R0JaE5dxrd1Uw"
CLIENT_ID = "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI = "https://app.adstock.ai/dashboard"

# -------------------- FUNÇÕES AUXILIARES (SÍNCRONAS) --------------------
def initialize_google_ads_client(refresh_token: str) -> GoogleAdsClient:
    logging.debug("Inicializando GoogleAdsClient (v12)...")
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
        raise HTTPException(status_code=500, detail=str(e))

def get_accessible_customers(client: GoogleAdsClient) -> List[str]:
    logging.debug("Obtendo contas acessíveis via refresh token...")
    try:
        customer_service = client.get_service("CustomerService")
        accessible_customers = customer_service.list_accessible_customers()
        customers = [res.split("/")[-1] for res in accessible_customers.resource_names]
        logging.info(f"Contas acessíveis obtidas: {customers}")
        return customers
    except Exception as e:
        logging.error(f"Erro ao obter contas acessíveis: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def convert_budget_to_micros(budget_str: str) -> int:
    """
    Remove o cifrão e converte o valor para micros.
    Ex: "$100" -> 100 * 1_000_000 = 100000000
    """
    try:
        numeric_str = budget_str.replace("$", "").strip()
        budget_value = float(numeric_str)
        return int(budget_value * 1_000_000)
    except Exception as e:
        logging.error(f"Erro ao converter o budget: {e}")
        raise HTTPException(status_code=400, detail=str(e))

def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_micros: int) -> str:
    logging.debug("Criando orçamento de campanha...")
    campaign_budget_service = client.get_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.create

    budget.name = f"Orcamento_{str(uuid.uuid4())[:8]}"
    budget.amount_micros = budget_micros
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    try:
        response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[operation]
        )
        resource_name = response.results[0].resource_name
        logging.info(f"Orçamento criado: {resource_name}")
        return resource_name
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar o orçamento: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

def create_campaign(client: GoogleAdsClient, customer_id: str, campaign_budget_resource: str,
                    campaign_name: str, start_date: str, end_date: str,
                    price_model: str, campaign_type: str) -> str:
    logging.debug("Criando campanha...")
    campaign_service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.create

    campaign.name = campaign_name
    if campaign_type.upper() == "DISPLAY":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    else:
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH

    campaign.status = client.enums.CampaignStatusEnum.ENABLED
    campaign.campaign_budget = campaign_budget_resource
    campaign.start_date = start_date
    campaign.end_date = end_date

    if price_model.upper() == "CPA":
        campaign.target_cpa.target_cpa_micros = 1_000_000
        campaign.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.TARGET_CPA
    else:
        campaign.manual_cpc.enhanced_cpc_enabled = False
        campaign.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.MANUAL_CPC

    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[operation]
        )
        resource_name = response.results[0].resource_name
        logging.info(f"Campanha criada: {resource_name}")
        return resource_name
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar campanha: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

def create_ad_group(client: GoogleAdsClient, customer_id: str, campaign_resource: str) -> str:
    logging.debug("Criando grupo de anúncios...")
    ad_group_service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.create

    ad_group.name = f"Grupo_{str(uuid.uuid4())[:8]}"
    ad_group.campaign = campaign_resource
    ad_group.cpc_bid_micros = 500_000
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD

    try:
        response = ad_group_service.mutate_ad_groups(
            customer_id=customer_id, operations=[operation]
        )
        resource_name = response.results[0].resource_name
        logging.info(f"Grupo de anúncios criado: {resource_name}")
        return resource_name
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar grupo de anúncios: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

def add_keywords_to_ad_group(client: GoogleAdsClient, customer_id: str,
                             ad_group_resource: str, keywords: List[str]) -> List[str]:
    logging.debug("Adicionando palavras-chave...")
    ad_group_criterion_service = client.get_service("AdGroupCriterionService")
    operations = []
    for kw in keywords:
        if kw:
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.create
            criterion.ad_group = ad_group_resource
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            criterion.keyword.text = kw
            criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
            operations.append(operation)

    try:
        response = ad_group_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
        resource_names = [res.resource_name for res in response.results]
        logging.info(f"Palavras-chave adicionadas: {resource_names}")
        return resource_names
    except GoogleAdsException as ex:
        logging.error(f"Erro ao adicionar palavras-chave: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

def create_campaign_criteria(client: GoogleAdsClient, customer_id: str, campaign_resource: str,
                             campaign_type: str, audience_gender: Optional[str],
                             audience_min_age: Optional[int], audience_max_age: Optional[int],
                             devices: List[str]) -> List[str]:
    """
    Cria critérios de campanha.
    Para SEARCH, aplica apenas critérios de dispositivos.
    Para DISPLAY, adiciona também critérios demográficos se informados.
    """
    logging.debug("Criando critérios de campanha...")
    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operations = []

    # Dispositivos (sempre aplicados)
    for device in devices:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_resource
        crit.device.type_ = getattr(client.enums.DeviceEnum, device.upper())
        operations.append(op)

    # Para DISPLAY, podemos adicionar demografia
    if campaign_type.upper() == "DISPLAY":
        if audience_gender:
            op = client.get_type("CampaignCriterionOperation")
            crit = op.create
            crit.campaign = campaign_resource
            crit.gender.type_ = getattr(client.enums.GenderTypeEnum, audience_gender.upper())
            operations.append(op)

        if audience_min_age is not None and audience_max_age is not None:
            age_op = client.get_type("CampaignCriterionOperation")
            age_crit = age_op.create
            age_crit.campaign = campaign_resource
            # Monta a string ex: "AGE_RANGE_25_34"
            age_range_str = f"AGE_RANGE_{audience_min_age}_{audience_max_age}"
            age_enum = getattr(client.enums.AgeRangeTypeEnum, age_range_str.upper(), None)
            if age_enum is None:
                logging.warning(f"Faixa etária '{age_range_str}' não reconhecida. Ignorando.")
            else:
                age_crit.age_range.type_ = age_enum
                operations.append(age_op)

    if not operations:
        logging.info("Nenhum critério adicional aplicado.")
        return []

    try:
        response = campaign_criterion_service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        resource_names = [res.resource_name for res in response.results]
        logging.info(f"Critérios de campanha criados: {resource_names}")
        return resource_names
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar critérios de campanha: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

# -------------------- ENDPOINT DA API (SÍNCRONO) --------------------
@app.post("/create_campaign", response_model=CampaignCreationResponse)
def create_campaign_endpoint(request_data: CampaignCreationRequest):
    """
    Fluxo de criação de campanha totalmente síncrono, similar ao código local rápido.
    """
    try:
        logging.debug(f"Body recebido: {json.dumps(request_data.dict(), indent=4)}")

        # 1) Inicializa o cliente
        client = initialize_google_ads_client(request_data.refresh_token)

        # 2) Obtém customer_id
        customers = get_accessible_customers(client)
        if not customers:
            raise HTTPException(status_code=500, detail="Nenhuma conta acessível encontrada.")
        customer_id = customers[0]

        # 3) Converte budget
        budget_micros = convert_budget_to_micros(request_data.budget)

        # 4) Cria o orçamento
        campaign_budget_resource = create_campaign_budget(client, customer_id, budget_micros)
        
        # 5) Cria a campanha
        campaign_resource = create_campaign(
            client,
            customer_id,
            campaign_budget_resource,
            request_data.campaign_name,
            request_data.start_date,
            request_data.end_date,
            request_data.price_model,
            request_data.campaign_type
        )

        # 6) Cria o grupo de anúncios
        ad_group_resource = create_ad_group(client, customer_id, campaign_resource)

        # 7) Adiciona palavras-chave (se existirem)
        keywords_list = [request_data.keyword1, request_data.keyword2, request_data.keyword3]
        keywords_resources = add_keywords_to_ad_group(
            client, customer_id, ad_group_resource, keywords_list
        )

        # 8) Cria critérios de campanha (dispositivos e, se DISPLAY, demografia)
        criteria_resources = create_campaign_criteria(
            client,
            customer_id,
            campaign_resource,
            request_data.campaign_type,
            request_data.audience_gender,
            request_data.audience_min_age,
            request_data.audience_max_age,
            request_data.devices
        )

        # 9) Monta a resposta
        result = CampaignCreationResponse(
            customer_id=customer_id,
            campaign_budget_resource=campaign_budget_resource,
            campaign_resource=campaign_resource,
            ad_group_resource=ad_group_resource,
            keywords_resources=keywords_resources,
            criteria_resources=criteria_resources,
            objective=request_data.objective,
            cover_photo=request_data.cover_photo,
            campaign_description=request_data.campaign_description
        )
        logging.info(f"Campanha criada com sucesso: {json.dumps(result.dict(), ensure_ascii=False)}")
        return result

    except Exception as e:
        logging.exception("Erro inesperado ao criar a campanha.")
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- EXECUÇÃO --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
