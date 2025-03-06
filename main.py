import sys
import json
import uuid
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# Google Ads
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# -------------------- CONFIGURAÇÕES FIXAS --------------------
DEVELOPER_TOKEN = "D4yv61IQ8R0JaE5dxrd1Uw"
CLIENT_ID = "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI = "https://app.adstock.ai/dashboard"

# -------------------- MODELOS --------------------
class CreateCampaignBody(BaseModel):
    refresh_token: str
    campaign_objective: str
    cover_photo_path: str
    campaign_name: str
    traffic_destination: str
    campaign_description: str
    keywords: List[str]               # ex: ["palavra chave 1", "palavra chave 2", ...]
    budget_micros: int                # ex: 5000000 para 5 USD
    start_date: str                   # ex: "20250401"
    end_date: str                     # ex: "20260401"
    price_model: str                  # "CPC" ou "CPA"

# -------------------- INICIALIZAÇÃO DA APLICAÇÃO --------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- FUNÇÕES AUXILIARES --------------------
def initialize_google_ads_client(
    refresh_token: str
) -> GoogleAdsClient:
    config = {
        "developer_token": DEVELOPER_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    try:
        # Ajuste a versão conforme seu ambiente (ex: "v13" se "v19" não estiver disponível).
        return GoogleAdsClient.load_from_dict(config, version="v19")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao inicializar GoogleAdsClient: {e}")

def get_first_customer_id(client: GoogleAdsClient) -> str:
    customer_service = client.get_service("CustomerService")
    accessible_customers = customer_service.list_accessible_customers()
    customer_ids = [res.split("/")[-1] for res in accessible_customers.resource_names]
    if not customer_ids:
        raise HTTPException(status_code=500, detail="Nenhuma conta acessível encontrada.")
    return customer_ids[0]

def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_micros: int) -> str:
    campaign_budget_service = client.get_service("CampaignBudgetService")
    campaign_budget_operation = client.get_type("CampaignBudgetOperation")
    campaign_budget = campaign_budget_operation.create

    campaign_budget.name = f"Orcamento_{uuid.uuid4().hex[:8]}"
    campaign_budget.amount_micros = budget_micros
    campaign_budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    try:
        response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[campaign_budget_operation]
        )
        return response.results[0].resource_name
    except GoogleAdsException as ex:
        raise HTTPException(status_code=500, detail=f"Erro ao criar orçamento: {ex.failure}")

def create_campaign(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_budget_resource: str,
    campaign_name: str,
    start_date: str,
    end_date: str,
    price_model: str
) -> str:
    campaign_service = client.get_service("CampaignService")
    campaign_operation = client.get_type("CampaignOperation")
    campaign = campaign_operation.create

    campaign.name = campaign_name
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
            customer_id=customer_id, operations=[campaign_operation]
        )
        return response.results[0].resource_name
    except GoogleAdsException as ex:
        raise HTTPException(status_code=500, detail=f"Erro ao criar campanha: {ex.failure}")

def create_ad_group(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_resource: str
) -> str:
    ad_group_service = client.get_service("AdGroupService")
    ad_group_operation = client.get_type("AdGroupOperation")
    ad_group = ad_group_operation.create

    ad_group.name = f"Grupo_{uuid.uuid4().hex[:8]}"
    ad_group.campaign = campaign_resource
    ad_group.cpc_bid_micros = 500_000
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD

    try:
        response = ad_group_service.mutate_ad_groups(
            customer_id=customer_id, operations=[ad_group_operation]
        )
        return response.results[0].resource_name
    except GoogleAdsException as ex:
        raise HTTPException(status_code=500, detail=f"Erro ao criar ad group: {ex.failure}")

def add_keywords_to_ad_group(
    client: GoogleAdsClient,
    customer_id: str,
    ad_group_resource: str,
    keywords: List[str]
) -> List[str]:
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
        return [res.resource_name for res in response.results]
    except GoogleAdsException as ex:
        raise HTTPException(status_code=500, detail=f"Erro ao adicionar keywords: {ex.failure}")

def create_campaign_criteria(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_resource: str
) -> List[str]:
    """
    Para SEARCH, adicionamos apenas critérios de dispositivos (DESKTOP, MOBILE, TABLET).
    """
    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operations = []

    devices = ["DESKTOP", "MOBILE", "TABLET"]
    for device in devices:
        device_op = client.get_type("CampaignCriterionOperation")
        dev_criterion = device_op.create
        dev_criterion.campaign = campaign_resource
        dev_criterion.device.type_ = getattr(client.enums.DeviceEnum, device)
        operations.append(device_op)

    try:
        response = campaign_criterion_service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        return [res.resource_name for res in response.results]
    except GoogleAdsException as ex:
        raise HTTPException(status_code=500, detail=f"Erro ao criar critérios (dispositivos): {ex.failure}")

# -------------------- ENDPOINT --------------------
@app.post("/create_campaign")
def create_campaign_api(body: CreateCampaignBody):
    """
    Endpoint que recebe o mesmo body e executa a lógica do script 'rápido'.
    """
    try:
        # 1. Inicializa o cliente
        client = initialize_google_ads_client(body.refresh_token)

        # 2. Obtém o primeiro customer ID
        customer_id = get_first_customer_id(client)

        # 3. Cria o orçamento (CampaignBudget)
        campaign_budget_resource = create_campaign_budget(client, customer_id, body.budget_micros)

        # 4. Cria a campanha
        campaign_resource = create_campaign(
            client,
            customer_id,
            campaign_budget_resource,
            body.campaign_name,
            body.start_date,
            body.end_date,
            body.price_model
        )

        # 5. Cria o grupo de anúncios
        ad_group_resource = create_ad_group(client, customer_id, campaign_resource)

        # 6. Adiciona as palavras-chave
        keywords_resources = add_keywords_to_ad_group(client, customer_id, ad_group_resource, body.keywords)

        # 7. Cria critérios (dispositivos)
        criteria_resources = create_campaign_criteria(client, customer_id, campaign_resource)

        # Monta o resultado
        result = {
            "customer_id": customer_id,
            "campaign_budget_resource": campaign_budget_resource,
            "campaign_resource": campaign_resource,
            "ad_group_resource": ad_group_resource,
            "keywords_resources": keywords_resources,
            "criteria_resources": criteria_resources,
            "objective": body.campaign_objective,
            "cover_photo": body.cover_photo_path,
            "traffic_destination": body.traffic_destination,
            "campaign_description": body.campaign_description
        }
        return result

    except HTTPException as http_e:
        raise http_e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- EXECUÇÃO --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
