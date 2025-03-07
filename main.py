import logging
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import List
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Configuração das credenciais do Google Ads
DEVELOPER_TOKEN = "D4yv61IQ8R0JaE5dxrd1Uw"
CLIENT_ID = "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI = "https://app.adstock.ai/dashboard"

# Configuração dos logs (quanto mais detalhados, melhor para debug)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = FastAPI()

# Modelo do corpo da requisição
class CampaignRequest(BaseModel):
    refresh_token: str
    keyword2: str
    keyword3: str
    budget: int           # valor em micros (budget_micros)
    start_date: str       # no formato "YYYYMMDD"
    end_date: str         # no formato "YYYYMMDD"
    price_model: str
    campaign_type: str
    audience_gender: str
    audience_min_age: int
    audience_max_age: int
    devices: List[str]

def get_googleads_client(refresh_token: str) -> GoogleAdsClient:
    """
    Cria o cliente do Google Ads a partir do refresh token e das credenciais.
    """
    config = {
        "developer_token": DEVELOPER_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    logging.debug("Criando cliente Google Ads com a seguinte configuração: %s", config)
    try:
        client = GoogleAdsClient.load_from_dict(config)
        logging.debug("Cliente Google Ads criado com sucesso.")
        return client
    except Exception as e:
        logging.error("Erro ao criar o cliente Google Ads: %s", e)
        raise

def get_customer_id(googleads_client: GoogleAdsClient) -> str:
    """
    Recupera o Customer ID utilizando o serviço CustomerService do Google Ads.
    """
    logging.debug("Recuperando Customer ID a partir do refresh token.")
    try:
        customer_service = googleads_client.get_service("CustomerService")
        response = customer_service.list_accessible_customers()
        logging.debug("Resposta do list_accessible_customers: %s", response)
        if not response.resource_names:
            raise Exception("Nenhum Customer acessível foi encontrado.")
        # O resource name tem o formato "customers/{customer_id}"
        customer_id = response.resource_names[0].split("/")[-1]
        logging.info("Customer ID obtido: %s", customer_id)
        return customer_id
    except Exception as e:
        logging.error("Erro ao recuperar o Customer ID: %s", e)
        raise

def create_campaign(googleads_client: GoogleAdsClient, customer_id: str, request_data: CampaignRequest) -> str:
    """
    Cria o orçamento e a campanha no Google Ads.
    A campanha será criada com status ENABLED (ativa).
    """
    logging.debug("Iniciando a criação da campanha para o Customer ID: %s", customer_id)
    
    # Criação do Orçamento da Campanha
    try:
        campaign_budget_service = googleads_client.get_service("CampaignBudgetService")
        campaign_budget_operation = googleads_client.get_type("CampaignBudgetOperation")
        campaign_budget = campaign_budget_operation.create
        campaign_budget.name = f"Orçamento - {request_data.keyword2} {request_data.keyword3}"
        campaign_budget.amount_micros = request_data.budget
        campaign_budget.delivery_method = googleads_client.get_type("BudgetDeliveryMethodEnum").STANDARD
        logging.debug("Orçamento configurado: %s", campaign_budget)
        budget_response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[campaign_budget_operation]
        )
        campaign_budget_resource = budget_response.results[0].resource_name
        logging.info("Orçamento criado com sucesso: %s", campaign_budget_resource)
    except GoogleAdsException as ex:
        logging.error("Erro no Google Ads ao criar o orçamento da campanha: %s", ex)
        raise HTTPException(status_code=500, detail=f"Erro ao criar o orçamento: {ex}")
    except Exception as e:
        logging.error("Erro inesperado ao criar o orçamento da campanha: %s", e)
        raise HTTPException(status_code=500, detail=f"Erro inesperado: {e}")
    
    # Criação da Campanha
    try:
        campaign_service = googleads_client.get_service("CampaignService")
        campaign_operation = googleads_client.get_type("CampaignOperation")
        campaign = campaign_operation.create
        campaign.name = f"Campanha - {request_data.keyword2} {request_data.keyword3}"
        # Exemplo: definindo o canal de publicidade com base no campaign_type recebido. Aqui, usamos SEARCH como padrão.
        campaign.advertising_channel_type = googleads_client.get_type("AdvertisingChannelTypeEnum").SEARCH
        # A campanha será criada ativa (ENABLED)
        campaign.status = googleads_client.get_type("CampaignStatusEnum").ENABLED
        campaign.campaign_budget = campaign_budget_resource
        campaign.start_date = request_data.start_date
        campaign.end_date = request_data.end_date
        # Outros atributos, como price_model, audience e devices, poderiam ser configurados aqui conforme a necessidade.
        logging.debug("Configuração final da campanha: %s", campaign)
        campaign_response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_operation]
        )
        campaign_resource = campaign_response.results[0].resource_name
        logging.info("Campanha criada com sucesso: %s", campaign_resource)
        return campaign_resource
    except GoogleAdsException as ex:
        logging.error("Erro no Google Ads ao criar a campanha: %s", ex)
        raise HTTPException(status_code=500, detail=f"Erro ao criar a campanha: {ex}")
    except Exception as e:
        logging.error("Erro inesperado ao criar a campanha: %s", e)
        raise HTTPException(status_code=500, detail=f"Erro inesperado: {e}")

@app.post("/create_campaign")
async def create_campaign_endpoint(request: CampaignRequest):
    logging.info("Recebida requisição para criação de campanha: %s", request.json())
    try:
        # Cria o cliente do Google Ads utilizando o refresh token recebido
        googleads_client = get_googleads_client(request.refresh_token)
        # Recupera o Customer ID a partir do refresh token
        customer_id = get_customer_id(googleads_client)
        logging.debug("Customer ID obtido: %s", customer_id)
        # Cria a campanha com base nos dados da requisição
        campaign_resource = create_campaign(googleads_client, customer_id, request)
        logging.info("Processo de criação de campanha concluído com sucesso.")
        return {"campaign_resource": campaign_resource}
    except Exception as e:
        logging.error("Falha ao criar a campanha: %s", e)
        raise HTTPException(status_code=500, detail=f"Falha ao criar a campanha: {e}")
