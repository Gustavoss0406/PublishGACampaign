import os
import logging
import json
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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

# -------------------- MODELOS --------------------
class CampaignCreationRequest(BaseModel):
    refresh_token: str
    objective: str                     # Ex: "Leads", "Vendas", "Alcance de marca", "Trafego em site"
    cover_photo: str                   # URL ou caminho da foto de capa
    campaign_name: str                 # Nome da campanha
    traffic_destination: str           # URL ou número de celular para tráfego
    campaign_description: str          # Descrição da campanha
    keywords: List[str]                # Lista de palavras-chave
    budget_micros: int                 # Budget em micros (ex: 5000000 para 5 USD)
    start_date: str                    # Data de início no formato "YYYYMMDD"
    end_date: str                      # Data de fim no formato "YYYYMMDD"
    price_model: str                   # "CPA" ou "CPC"
    campaign_type: str = "SEARCH"      # "SEARCH" ou "DISPLAY"
    # Campos de segmentação demográfica (usados se campaign_type for DISPLAY)
    audience_gender: Optional[str] = None       # Ex: "FEMALE", "MALE", etc.
    audience_age_range: Optional[str] = None      # Ex: "AGE_RANGE_25_34", "AGE_RANGE_18_24", etc.
    audience_income_range: Optional[str] = None   # Ex: "INCOME_RANGE_50_100", "INCOME_RANGE_HIGH", etc.
    devices: List[str] = []                       # Ex: ["DESKTOP", "MOBILE", "TABLET"]

class CampaignCreationResponse(BaseModel):
    customer_id: str
    campaign_budget_resource: str
    campaign_resource: str
    ad_group_resource: str
    keywords_resources: List[str]
    criteria_resources: List[str]
    objective: str
    cover_photo: str
    traffic_destination: str
    campaign_description: str

# -------------------- CONFIGURAÇÕES FIXAS --------------------
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
        client = GoogleAdsClient.load_from_dict(config, version="v19")
        logging.info("GoogleAdsClient inicializado com sucesso.")
        return client
    except Exception as e:
        logging.error(f"Erro na inicialização do GoogleAdsClient: {e}")
        raise HTTPException(status_code=500, detail=f"Erro na inicialização do GoogleAdsClient: {e}")

def get_accessible_customers(client: GoogleAdsClient) -> List[str]:
    logging.debug("Obtendo contas acessíveis...")
    try:
        customer_service = client.get_service("CustomerService")
        accessible_customers = customer_service.list_accessible_customers()
        customers = [resource_name.split("/")[-1] for resource_name in accessible_customers.resource_names]
        logging.info(f"Contas acessíveis obtidas: {customers}")
        return customers
    except Exception as e:
        logging.error(f"Erro ao obter contas acessíveis: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao obter contas acessíveis: {e}")

def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_micros: int) -> str:
    campaign_budget_service = client.get_service("CampaignBudgetService")
    campaign_budget_operation = client.get_type("CampaignBudgetOperation")
    campaign_budget = campaign_budget_operation.create

    campaign_budget.name = f"Orcamento_{str(uuid.uuid4())[:8]}"
    campaign_budget.amount_micros = budget_micros
    campaign_budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    try:
        response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[campaign_budget_operation]
        )
        return response.results[0].resource_name
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar o orçamento da campanha: {ex.failure}")
        raise HTTPException(status_code=500, detail=f"Erro ao criar o orçamento da campanha: {ex.failure}")

def create_campaign(client: GoogleAdsClient, customer_id: str, campaign_budget_resource: str,
                    campaign_name: str, start_date: str, end_date: str, price_model: str,
                    campaign_type: str) -> str:
    campaign_service = client.get_service("CampaignService")
    campaign_operation = client.get_type("CampaignOperation")
    campaign = campaign_operation.create

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
        campaign.target_cpa.target_cpa_micros = 1_000_000  # exemplo de valor alvo
        campaign.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.TARGET_CPA
    else:
        campaign.manual_cpc.enhanced_cpc_enabled = False
        campaign.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.MANUAL_CPC

    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_operation]
        )
        return response.results[0].resource_name
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar a campanha: {ex.failure}")
        raise HTTPException(status_code=500, detail=f"Erro ao criar a campanha: {ex.failure}")

def create_ad_group(client: GoogleAdsClient, customer_id: str, campaign_resource: str) -> str:
    ad_group_service = client.get_service("AdGroupService")
    ad_group_operation = client.get_type("AdGroupOperation")
    ad_group = ad_group_operation.create

    ad_group.name = f"Grupo_{str(uuid.uuid4())[:8]}"
    ad_group.campaign = campaign_resource
    ad_group.cpc_bid_micros = 500_000
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD

    try:
        response = ad_group_service.mutate_ad_groups(
            customer_id=customer_id, operations=[ad_group_operation]
        )
        return response.results[0].resource_name
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar o grupo de anúncios: {ex.failure}")
        raise HTTPException(status_code=500, detail=f"Erro ao criar o grupo de anúncios: {ex.failure}")

def add_keywords_to_ad_group(client: GoogleAdsClient, customer_id: str, ad_group_resource: str,
                             keywords: List[str]) -> List[str]:
    ad_group_criterion_service = client.get_service("AdGroupCriterionService")
    operations = []
    for kw in keywords:
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
        return [result.resource_name for result in response.results]
    except GoogleAdsException as ex:
        logging.error(f"Erro ao adicionar palavras-chave: {ex.failure}")
        raise HTTPException(status_code=500, detail=f"Erro ao adicionar palavras-chave: {ex.failure}")

def create_campaign_criteria(client: GoogleAdsClient, customer_id: str, campaign_resource: str,
                             campaign_type: str, audience_gender: Optional[str],
                             audience_age_range: Optional[str],
                             audience_income_range: Optional[str],
                             devices: List[str]) -> List[str]:
    """
    Cria critérios de campanha.
    Para campanhas SEARCH, serão aplicados apenas critérios de dispositivos.
    Para DISPLAY, se informados, serão adicionados também os critérios demográficos.
    """
    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operations = []

    # Critérios para dispositivos (sempre aplicados)
    if devices:
        for device in devices:
            device_operation = client.get_type("CampaignCriterionOperation")
            device_criterion = device_operation.create
            device_criterion.campaign = campaign_resource
            device_criterion.device.type_ = getattr(client.enums.DeviceEnum, device.upper())
            operations.append(device_operation)

    if campaign_type.upper() == "DISPLAY":
        if audience_gender:
            gender_operation = client.get_type("CampaignCriterionOperation")
            gender_criterion = gender_operation.create
            gender_criterion.campaign = campaign_resource
            gender_criterion.gender.type_ = getattr(client.enums.GenderTypeEnum, audience_gender.upper())
            operations.append(gender_operation)
        if audience_age_range:
            age_operation = client.get_type("CampaignCriterionOperation")
            age_criterion = age_operation.create
            age_criterion.campaign = campaign_resource
            age_criterion.age_range.type_ = getattr(client.enums.AgeRangeTypeEnum, audience_age_range.upper())
            operations.append(age_operation)
        if audience_income_range:
            income_operation = client.get_type("CampaignCriterionOperation")
            income_criterion = income_operation.create
            income_criterion.campaign = campaign_resource
            income_criterion.income_range.type_ = getattr(client.enums.IncomeRangeTypeEnum, audience_income_range.upper())
            operations.append(income_operation)

    try:
        response = campaign_criterion_service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        return [result.resource_name for result in response.results]
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar critérios de campanha: {ex.failure}")
        raise HTTPException(status_code=500, detail=f"Erro ao criar critérios de campanha: {ex.failure}")

# -------------------- ENDPOINT DA API --------------------
@app.post("/create_campaign", response_model=CampaignCreationResponse)
def create_campaign_endpoint(request_data: CampaignCreationRequest):
    try:
        logging.debug("Recebida requisição para criação de campanha.")
        client = initialize_google_ads_client(request_data.refresh_token)
        customers = get_accessible_customers(client)
        if not customers:
            raise HTTPException(status_code=500, detail="Nenhuma conta acessível encontrada.")
        customer_id = customers[0]

        campaign_budget_resource = create_campaign_budget(client, customer_id, request_data.budget_micros)
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
        ad_group_resource = create_ad_group(client, customer_id, campaign_resource)
        keywords_resources = add_keywords_to_ad_group(client, customer_id, ad_group_resource, request_data.keywords)
        criteria_resources = create_campaign_criteria(
            client,
            customer_id,
            campaign_resource,
            request_data.campaign_type,
            request_data.audience_gender,
            request_data.audience_age_range,
            request_data.audience_income_range,
            request_data.devices
        )

        result = CampaignCreationResponse(
            customer_id=customer_id,
            campaign_budget_resource=campaign_budget_resource,
            campaign_resource=campaign_resource,
            ad_group_resource=ad_group_resource,
            keywords_resources=keywords_resources,
            criteria_resources=criteria_resources,
            objective=request_data.objective,
            cover_photo=request_data.cover_photo,
            traffic_destination=request_data.traffic_destination,
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
