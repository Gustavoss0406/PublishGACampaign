import os
import sys
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List
import yaml

# Importa o client library oficial do Google Ads e erros
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Configuração de logs detalhados
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = FastAPI(title="Google Ads Campaign API", version="1.0")

# Configuração do middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ajuste conforme necessário
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Modelo de request com os campos necessários
class CampaignRequest(BaseModel):
    keyword2: str = Field(..., example="segundakeyword")
    keyword3: str = Field(..., example="terceirakeyword")
    budget: str = Field(..., example="budget_micros")
    start_date: str = Field(..., example="2025-03-06")
    end_date: str = Field(..., example="2025-04-06")
    price_model: str = Field(..., example="price_model")
    campaign_type: str = Field(..., example="SEARCH")
    audience_gender: str = Field(..., example="audience_gender")
    audience_min_age: str = Field(..., example="min_age")
    audience_max_age: str = Field(..., example="max_age")
    devices: List[str] = Field(..., example=["mobile", "desktop"])
    refresh_token: str = Field(..., example="your_refresh_token_here")

def get_google_ads_client(refresh_token: str) -> GoogleAdsClient:
    """
    Carrega as configurações do arquivo google-ads.yaml, sobrescreve o refresh token e retorna o GoogleAdsClient.
    """
    try:
        config_path = os.path.join(os.getcwd(), "google-ads.yaml")
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)
        # Sobrescreve o refresh token com o informado na requisição
        config_data["refresh_token"] = refresh_token
        client = GoogleAdsClient.load_from_dict(config_data)
        logging.debug("Google Ads Client criado com sucesso.")
        return client
    except Exception as e:
        logging.error("Erro ao carregar google-ads.yaml: %s", str(e))
        raise HTTPException(status_code=500, detail="Erro ao configurar o Google Ads Client")

def list_accessible_customers(client: GoogleAdsClient) -> str:
    """
    Utiliza o CustomerService para listar os customers acessíveis e retorna o ID do primeiro.
    """
    try:
        customer_service = client.get_service("CustomerService")
        response = customer_service.list_accessible_customers()
        logging.debug("Customer resource names: %s", response.resource_names)
        if not response.resource_names:
            raise Exception("Nenhum customer encontrado.")
        first_customer = response.resource_names[0]  # Formato: "customers/{customer_id}"
        customer_id = first_customer.split("/")[-1]
        logging.debug("Customer ID selecionado: %s", customer_id)
        return customer_id
    except GoogleAdsException as ex:
        logging.error("Google Ads API Exception ao listar customers: %s", ex)
        raise HTTPException(status_code=500, detail="Erro ao listar customers")
    except Exception as e:
        logging.error("Erro inesperado ao listar customers: %s", str(e))
        raise HTTPException(status_code=500, detail="Erro inesperado ao listar customers")

def create_campaign(client: GoogleAdsClient, customer_id: str, campaign_data: dict) -> dict:
    """
    Cria uma campanha utilizando o CampaignService.
    OBS: Essa é uma implementação simplificada. A criação real de uma campanha
    exige a criação de um orçamento e outras configurações.
    """
    try:
        campaign_service = client.get_service("CampaignService")
        # Cria a operação para a campanha
        campaign_operation = client.get_type("CampaignOperation")
        campaign = campaign_operation.create

        # Define os campos mínimos para a campanha
        campaign.name = f"Campanha - {campaign_data.get('keyword2', 'SemNome')}"
        # Define o tipo de canal de publicidade; aqui usamos SEARCH como exemplo
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
        # As datas devem estar no formato YYYYMMDD
        campaign.start_date = campaign_data.get("start_date").replace("-", "")
        campaign.end_date = campaign_data.get("end_date").replace("-", "")
        # Campo obrigatório: campanha deve ter um orçamento (ID de orçamento já criado)
        campaign.campaign_budget = f"customers/{customer_id}/campaignBudgets/INSERT_BUDGET_ID"
        # Ativa a campanha
        campaign.status = client.enums.CampaignStatusEnum.ENABLED

        # Outras configurações (como estratégia de lances, segmentação) podem ser adicionadas aqui

        # Envia a operação de mutação para criar a campanha
        response = campaign_service.mutate_campaigns(customer_id=customer_id, operations=[campaign_operation])
        logging.debug("Resposta da criação da campanha: %s", response)
        return {"resource_name": response.results[0].resource_name}
    except GoogleAdsException as ex:
        logging.error("Google Ads API Exception ao criar campanha: %s", ex)
        raise HTTPException(status_code=500, detail="Erro ao criar campanha")
    except Exception as e:
        logging.error("Erro inesperado ao criar campanha: %s", str(e))
        raise HTTPException(status_code=500, detail="Erro inesperado ao criar campanha")

@app.post("/create_campaign")
async def create_campaign_endpoint(campaign_request: CampaignRequest):
    logging.debug("Requisição recebida: %s", campaign_request.dict())
    try:
        # Cria o cliente do Google Ads com o refresh token informado pelo usuário
        client = get_google_ads_client(campaign_request.refresh_token)
        # Obtém o customer ID a partir do método list_accessible_customers
        customer_id = list_accessible_customers(client)
        # Converte os dados da campanha para dicionário e remove o refresh_token
        campaign_data = campaign_request.dict()
        campaign_data.pop("refresh_token", None)
        # Cria a campanha utilizando os dados informados
        result = create_campaign(client, customer_id, campaign_data)
        logging.debug("Campanha criada com sucesso: %s", result)
        return {
            "message": "Campanha criada com sucesso",
            "campaign_result": result
        }
    except Exception as e:
        logging.exception("Erro durante o processo de criação da campanha")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logging.debug("Iniciando a API FastAPI na porta %s", port)
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
