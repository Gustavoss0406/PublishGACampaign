import os
import logging
import json
import uuid
import time
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
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

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
    logging.debug(f"[Middleware] Raw request body: {body_str}")
    
    response = await call_next(request)
    logging.debug(f"[Middleware] Response status code: {response.status_code}")
    return response

# -------------------- MODELOS --------------------
class CampaignCreationRequest(BaseModel):
    refresh_token: str
    objective: str                     # Ex: "Leads", "Vendas", "Alcance de marca", "Trafego em site"
    cover_photo: str                   # URL ou caminho da foto de capa
    campaign_name: str                 # Nome da campanha
    campaign_description: str          # Descrição da campanha
    keyword1: Optional[str] = None
    keyword2: Optional[str] = None
    keyword3: Optional[str] = None
    budget: str                        # Recebido no formato "$100"
    start_date: str                    # Data de início no formato "YYYYMMDD"
    end_date: str                      # Data de fim no formato "YYYYMMDD"
    price_model: str                   # "CPA" ou "CPC"
    campaign_type: str = "SEARCH"      # "SEARCH" ou "DISPLAY"
    audience_gender: Optional[str] = None       # Ex: "FEMALE", "MALE"
    audience_min_age: Optional[int] = None
    audience_max_age: Optional[int] = None
    devices: List[str] = []

    @validator("devices", pre=True, always=True)
    def filter_empty_devices(cls, v):
        if isinstance(v, list):
            filtered = [item for item in v if item and item.strip()]
            logging.debug(f"[Validator] Devices filtrados: {filtered}")
            return filtered
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
    logging.debug("[initialize_google_ads_client] Iniciando com refresh_token: %s", refresh_token)
    config = {
        "developer_token": DEVELOPER_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    try:
        start = time.time()
        client = GoogleAdsClient.load_from_dict(config, version="v12")
        elapsed = time.time() - start
        logging.info(f"[initialize_google_ads_client] Cliente inicializado em {elapsed:.2f}s.")
        return client
    except Exception as e:
        logging.error(f"[initialize_google_ads_client] Erro: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def get_accessible_customers(client: GoogleAdsClient) -> List[str]:
    logging.debug("[get_accessible_customers] Iniciando consulta de contas acessíveis.")
    try:
        start = time.time()
        customer_service = client.get_service("CustomerService")
        accessible_customers = customer_service.list_accessible_customers()
        customers = [res.split("/")[-1] for res in accessible_customers.resource_names]
        elapsed = time.time() - start
        logging.info(f"[get_accessible_customers] Contas obtidas: {customers} em {elapsed:.2f}s.")
        return customers
    except Exception as e:
        logging.error(f"[get_accessible_customers] Erro: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def convert_budget_to_micros(budget_str: str) -> int:
    logging.debug(f"[convert_budget_to_micros] Budget recebido: {budget_str}")
    try:
        numeric_str = budget_str.replace("$", "").strip()
        budget_value = float(numeric_str)
        micros = int(budget_value * 1_000_000)
        logging.debug(f"[convert_budget_to_micros] Budget convertido para micros: {micros}")
        return micros
    except Exception as e:
        logging.error(f"[convert_budget_to_micros] Erro: {e}")
        raise HTTPException(status_code=400, detail=str(e))

def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_micros: int) -> str:
    logging.debug(f"[create_campaign_budget] Criando orçamento para customer_id: {customer_id} com budget {budget_micros} micros.")
    campaign_budget_service = client.get_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.create

    budget.name = f"Orcamento_{str(uuid.uuid4())[:8]}"
    budget.amount_micros = budget_micros
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    try:
        start = time.time()
        response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[operation]
        )
        elapsed = time.time() - start
        resource_name = response.results[0].resource_name
        logging.info(f"[create_campaign_budget] Orçamento criado: {resource_name} em {elapsed:.2f}s.")
        return resource_name
    except GoogleAdsException as ex:
        logging.error(f"[create_campaign_budget] Erro: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

def create_campaign(client: GoogleAdsClient, customer_id: str, campaign_budget_resource: str,
                    campaign_name: str, start_date: str, end_date: str,
                    price_model: str, campaign_type: str) -> str:
    logging.debug(f"[create_campaign] Criando campanha '{campaign_name}' para customer_id: {customer_id}")
    campaign_service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.create

    campaign.name = campaign_name
    logging.debug(f"[create_campaign] campaign_type: {campaign_type}")
    if campaign_type.upper() == "DISPLAY":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    else:
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH

    campaign.status = client.enums.CampaignStatusEnum.ENABLED
    campaign.campaign_budget = campaign_budget_resource
    campaign.start_date = start_date
    campaign.end_date = end_date

    logging.debug(f"[create_campaign] price_model: {price_model}")
    if price_model.upper() == "CPA":
        campaign.target_cpa.target_cpa_micros = 1_000_000
        campaign.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.TARGET_CPA
    else:
        campaign.manual_cpc.enhanced_cpc_enabled = False
        campaign.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.MANUAL_CPC

    try:
        start = time.time()
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[operation]
        )
        elapsed = time.time() - start
        resource_name = response.results[0].resource_name
        logging.info(f"[create_campaign] Campanha criada: {resource_name} em {elapsed:.2f}s.")
        return resource_name
    except GoogleAdsException as ex:
        logging.error(f"[create_campaign] Erro: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

def create_ad_group(client: GoogleAdsClient, customer_id: str, campaign_resource: str) -> str:
    logging.debug(f"[create_ad_group] Criando grupo de anúncios para campanha: {campaign_resource}")
    ad_group_service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.create

    ad_group.name = f"Grupo_{str(uuid.uuid4())[:8]}"
    ad_group.campaign = campaign_resource
    ad_group.cpc_bid_micros = 500_000
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD

    try:
        start = time.time()
        response = ad_group_service.mutate_ad_groups(
            customer_id=customer_id, operations=[operation]
        )
        elapsed = time.time() - start
        resource_name = response.results[0].resource_name
        logging.info(f"[create_ad_group] Grupo de anúncios criado: {resource_name} em {elapsed:.2f}s.")
        return resource_name
    except GoogleAdsException as ex:
        logging.error(f"[create_ad_group] Erro: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

def add_keywords_to_ad_group(client: GoogleAdsClient, customer_id: str,
                             ad_group_resource: str, keywords: List[str]) -> List[str]:
    logging.debug(f"[add_keywords_to_ad_group] Adicionando palavras-chave: {keywords} para grupo: {ad_group_resource}")
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
        start = time.time()
        response = ad_group_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
        elapsed = time.time() - start
        resource_names = [res.resource_name for res in response.results]
        logging.info(f"[add_keywords_to_ad_group] Palavras-chave adicionadas: {resource_names} em {elapsed:.2f}s.")
        return resource_names
    except GoogleAdsException as ex:
        logging.error(f"[add_keywords_to_ad_group] Erro: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

def create_campaign_criteria(client: GoogleAdsClient, customer_id: str, campaign_resource: str,
                             campaign_type: str, audience_gender: Optional[str],
                             audience_min_age: Optional[int], audience_max_age: Optional[int],
                             devices: List[str]) -> List[str]:
    logging.debug("[create_campaign_criteria] Iniciando criação de critérios para campanha.")
    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operations = []

    # Dispositivos (sempre aplicados)
    for device in devices:
        op = client.get_type("CampaignCriterionOperation")
        crit = op.create
        crit.campaign = campaign_resource
        try:
            device_enum = getattr(client.enums.DeviceEnum, device.upper())
            crit.device.type_ = device_enum
            logging.debug(f"[create_campaign_criteria] Adicionando dispositivo: {device.upper()}")
            operations.append(op)
        except AttributeError:
            logging.warning(f"[create_campaign_criteria] Dispositivo '{device}' não reconhecido. Ignorando.")

    # Para DISPLAY, adiciona demografia
    if campaign_type.upper() == "DISPLAY":
        if audience_gender:
            op = client.get_type("CampaignCriterionOperation")
            crit = op.create
            crit.campaign = campaign_resource
            try:
                gender_enum = getattr(client.enums.GenderTypeEnum, audience_gender.upper())
                crit.gender.type_ = gender_enum
                logging.debug(f"[create_campaign_criteria] Adicionando gênero: {audience_gender.upper()}")
                operations.append(op)
            except AttributeError:
                logging.warning(f"[create_campaign_criteria] Gênero '{audience_gender}' não reconhecido. Ignorando.")
        if audience_min_age is not None and audience_max_age is not None:
            age_op = client.get_type("CampaignCriterionOperation")
            age_crit = age_op.create
            age_crit.campaign = campaign_resource
            age_range_str = f"AGE_RANGE_{audience_min_age}_{audience_max_age}"
            age_enum = getattr(client.enums.AgeRangeTypeEnum, age_range_str.upper(), None)
            if age_enum is None:
                logging.warning(f"[create_campaign_criteria] Faixa etária '{age_range_str}' não reconhecida. Critério ignorado.")
            else:
                age_crit.age_range.type_ = age_enum
                logging.debug(f"[create_campaign_criteria] Adicionando faixa etária: {age_range_str.upper()}")
                operations.append(age_op)

    if not operations:
        logging.info("[create_campaign_criteria] Nenhum critério adicional aplicado.")
        return []

    try:
        start = time.time()
        response = campaign_criterion_service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        elapsed = time.time() - start
        resource_names = [res.resource_name for res in response.results]
        logging.info(f"[create_campaign_criteria] Critérios criados: {resource_names} em {elapsed:.2f}s.")
        return resource_names
    except GoogleAdsException as ex:
        logging.error(f"[create_campaign_criteria] Erro: {ex.failure}")
        raise HTTPException(status_code=500, detail=str(ex.failure))

# -------------------- ENDPOINT DA API (SÍNCRONO) --------------------
@app.post("/create_campaign", response_model=CampaignCreationResponse)
def create_campaign_endpoint(request_data: CampaignCreationRequest):
    try:
        logging.debug(f"[Endpoint] Início do processamento. Body recebido: {json.dumps(request_data.dict(), indent=4)}")

        # 1) Inicializa o cliente
        logging.debug("[Endpoint] Inicializando cliente Google Ads...")
        client = initialize_google_ads_client(request_data.refresh_token)
        logging.debug("[Endpoint] Cliente inicializado.")

        # 2) Obtém customer_id
        logging.debug("[Endpoint] Obtendo contas acessíveis...")
        customers = get_accessible_customers(client)
        if not customers:
            raise HTTPException(status_code=500, detail="Nenhuma conta acessível encontrada.")
        customer_id = customers[0]
        logging.debug(f"[Endpoint] Utilizando customer_id: {customer_id}")

        # 3) Converte o budget
        logging.debug(f"[Endpoint] Convertendo budget: {request_data.budget}")
        budget_micros = convert_budget_to_micros(request_data.budget)
        logging.debug(f"[Endpoint] Budget convertido: {budget_micros} micros")

        # 4) Cria o orçamento
        logging.debug("[Endpoint] Criando orçamento...")
        campaign_budget_resource = create_campaign_budget(client, customer_id, budget_micros)
        logging.debug(f"[Endpoint] Orçamento criado: {campaign_budget_resource}")

        # 5) Cria a campanha
        logging.debug("[Endpoint] Criando campanha...")
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
        logging.debug(f"[Endpoint] Campanha criada: {campaign_resource}")

        # 6) Cria o grupo de anúncios
        logging.debug("[Endpoint] Criando grupo de anúncios...")
        ad_group_resource = create_ad_group(client, customer_id, campaign_resource)
        logging.debug(f"[Endpoint] Grupo de anúncios criado: {ad_group_resource}")

        # 7) Adiciona palavras-chave
        keywords_list = [request_data.keyword1, request_data.keyword2, request_data.keyword3]
        logging.debug(f"[Endpoint] Adicionando palavras-chave: {keywords_list}")
        keywords_resources = add_keywords_to_ad_group(client, customer_id, ad_group_resource, keywords_list)
        logging.debug(f"[Endpoint] Palavras-chave adicionadas: {keywords_resources}")

        # 8) Cria critérios de campanha
        logging.debug("[Endpoint] Criando critérios de campanha...")
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
        logging.debug(f"[Endpoint] Critérios de campanha criados: {criteria_resources}")

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
        logging.info(f"[Endpoint] Campanha criada com sucesso: {json.dumps(result.dict(), ensure_ascii=False)}")
        return result

    except Exception as e:
        logging.exception("[Endpoint] Erro inesperado ao criar a campanha.")
        raise HTTPException(status_code=500, detail=str(e))

# -------------------- EXECUÇÃO --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logging.info(f"Aplicação iniciando na porta {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="debug")
