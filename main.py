import logging
import sys
import uuid
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Configuração dos logs com nível DEBUG e saída para stdout
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = FastAPI()

# Modelo que define o corpo do request
class CampaignRequest(BaseModel):
    refresh_token: str
    keyword2: str
    keyword3: str
    budget: int  # valor em micros
    start_date: str  # formato YYYYMMDD
    end_date: str    # formato YYYYMMDD
    price_model: str
    campaign_type: str
    audience_gender: str
    audience_min_age: int
    audience_max_age: int
    devices: list[str]  # lista de dispositivos (ex.: ["mobile", "desktop"])

@app.post("/create_campaign")
async def create_campaign(request: CampaignRequest):
    logging.info("Recebido request para criação de campanha")
    logging.debug(f"Dados do request: {request.dict()}")

    try:
        # Configuração do cliente do Google Ads usando os dados fornecidos
        config_dict = {
            "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
            "client_id": "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com",
            "client_secret": "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",
            "refresh_token": request.refresh_token,
            "use_proto_plus": True
        }
        logging.info("Inicializando Google Ads Client com o refresh token fornecido")
        client = GoogleAdsClient.load_from_dict(config_dict)
        logging.debug("Google Ads Client inicializado com sucesso")

        # Recupera o customer ID com base no refresh token
        customer_id = get_customer_id(client)
        logging.info(f"Customer ID recuperado: {customer_id}")

        # Cria o campaign budget com o valor informado
        budget_resource_name = create_campaign_budget(client, customer_id, request.budget)
        logging.info(f"Campaign budget criado: {budget_resource_name}")

        # Cria a campanha com os dados do request e vinculando o budget criado
        campaign_resource_name = create_campaign_resource(client, customer_id, budget_resource_name, request)
        logging.info(f"Campanha criada: {campaign_resource_name}")

        # Aqui poderiam ser adicionadas funções para configurar critérios de targeting (idade, gênero, dispositivos, etc)
        # Devido à complexidade da implementação, esse exemplo foca na criação básica da campanha

        return {"status": "success", "campaign_resource_name": campaign_resource_name}

    except GoogleAdsException as ex:
        logging.error(f"Exceção na API do Google Ads: {ex}")
        raise HTTPException(status_code=400, detail=f"Erro na API do Google Ads: {ex}")
    except Exception as ex:
        logging.exception("Erro inesperado durante a criação da campanha")
        raise HTTPException(status_code=500, detail=str(ex))


def get_customer_id(client: GoogleAdsClient) -> str:
    """
    Usa o serviço CustomerService para listar os customers acessíveis e extrair o customer ID.
    """
    logging.info("Buscando lista de customers acessíveis")
    customer_service = client.get_service("CustomerService")
    response = customer_service.list_accessible_customers()
    logging.debug(f"Response dos customers acessíveis: {response.resource_names}")

    if not response.resource_names:
        raise Exception("Nenhum customer acessível foi encontrado.")

    # Seleciona o primeiro customer retornado
    customer_resource_name = response.resource_names[0]
    customer_id = customer_resource_name.split("/")[-1]
    logging.debug(f"Customer ID extraído: {customer_id} a partir do resource name: {customer_resource_name}")
    return customer_id


def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_micros: int) -> str:
    """
    Cria um campaign budget com o valor informado (em micros).
    """
    logging.info("Iniciando criação do campaign budget")
    campaign_budget_service = client.get_service("CampaignBudgetService")
    campaign_budget_operation = client.get_type("CampaignBudgetOperation")
    campaign_budget = campaign_budget_operation.create

    # Cria um nome único para o budget
    campaign_budget.name = f"Budget_{uuid.uuid4()}"
    campaign_budget.amount_micros = budget_micros
    campaign_budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    logging.debug(f"Detalhes do budget: Nome: {campaign_budget.name}, Valor (micros): {budget_micros}")

    try:
        response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[campaign_budget_operation]
        )
        budget_resource_name = response.results[0].resource_name
        logging.info(f"Budget criado com sucesso: {budget_resource_name}")
        return budget_resource_name
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar campaign budget: {ex}")
        raise


def create_campaign_resource(client: GoogleAdsClient, customer_id: str, budget_resource_name: str, request: CampaignRequest) -> str:
    """
    Cria a campanha com os parâmetros informados.
    """
    logging.info("Iniciando criação da campanha")
    campaign_service = client.get_service("CampaignService")
    campaign_operation = client.get_type("CampaignOperation")
    campaign = campaign_operation.create

    # Define o nome da campanha usando keyword2 e keyword3 para identificação
    campaign.name = f"Campaign_{request.keyword2}_{request.keyword3}_{uuid.uuid4()}"

    # Mapeia o campaign_type para o AdvertisingChannelTypeEnum
    if request.campaign_type.upper() == "SEARCH":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    elif request.campaign_type.upper() == "DISPLAY":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    else:
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH

    # Define a campanha como ativa (ENABLED)
    campaign.status = client.enums.CampaignStatusEnum.ENABLED
    campaign.campaign_budget = budget_resource_name
    campaign.start_date = request.start_date
    campaign.end_date = request.end_date

    logging.debug(f"Configuração da campanha: {campaign}")

    # Configuração da estratégia de lance de acordo com price_model
    if request.price_model.upper() == "CPC":
        campaign.manual_cpc.CopyFrom(client.get_type("ManualCpc"))
        logging.debug("Estratégia de lance configurada: Manual CPC")
    elif request.price_model.upper() == "CPM":
        campaign.manual_cpm.CopyFrom(client.get_type("ManualCpm"))
        logging.debug("Estratégia de lance configurada: Manual CPM")
    else:
        campaign.manual_cpc.CopyFrom(client.get_type("ManualCpc"))
        logging.debug("Estratégia de lance padrão configurada: Manual CPC")

    # Observação: Para aplicar targeting (gênero, faixa etária, dispositivos) seria necessário criar recursos adicionais
    # (CampaignCriterion, etc.). Neste exemplo, focamos na criação básica da campanha.

    logging.info("Configurações básicas da campanha aplicadas")
    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_operation]
        )
        campaign_resource_name = response.results[0].resource_name
        logging.info(f"Campanha criada com sucesso: {campaign_resource_name}")
        return campaign_resource_name
    except GoogleAdsException as ex:
        logging.error(f"Erro ao criar a campanha: {ex}")
        raise

if __name__ == "__main__":
    import uvicorn
    logging.info("Iniciando aplicação via uvicorn")
    uvicorn.run(app, host="0.0.0.0", port=8000)
