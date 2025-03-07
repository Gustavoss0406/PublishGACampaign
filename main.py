import logging
import sys
import uuid
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Configuração de logs detalhados (DEBUG) com saída para stdout
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = FastAPI()

# Middleware CORS para lidar com requisições OPTIONS e evitar problemas de preflight
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ajuste conforme necessário
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Eventos de startup e shutdown para logar início e fim da aplicação
@app.on_event("startup")
async def startup_event():
    logging.info("Startup: A aplicação foi iniciada.")

@app.on_event("shutdown")
async def shutdown_event():
    logging.info("Shutdown: A aplicação está sendo encerrada.")

# Middleware para logar cada requisição recebida e a resposta enviada
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logging.info(f"Recebendo request: {request.method} {request.url}")
    response = await call_next(request)
    logging.info(f"Response status: {response.status_code} para {request.method} {request.url}")
    return response

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
    devices: list[str]  # exemplo: ["mobile", "desktop"]

@app.post("/create_campaign")
async def create_campaign(request_data: CampaignRequest):
    logging.info("Endpoint /create_campaign acionado.")
    logging.debug(f"Dados recebidos: {request_data.json()}")

    try:
        # Configuração do cliente do Google Ads com os dados fornecidos
        config_dict = {
            "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
            "client_id": "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com",
            "client_secret": "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",
            "refresh_token": request_data.refresh_token,
            "use_proto_plus": True
        }
        logging.info("Inicializando Google Ads Client com o refresh token fornecido.")
        client = GoogleAdsClient.load_from_dict(config_dict)
        logging.debug("Google Ads Client inicializado com sucesso.")

        # Obter o customer ID com base no refresh token
        customer_id = get_customer_id(client)
        logging.info(f"Customer ID obtido: {customer_id}")

        # Criação do campaign budget com o valor informado
        budget_resource_name = create_campaign_budget(client, customer_id, request_data.budget)
        logging.info(f"Campaign budget criado: {budget_resource_name}")

        # Criação da campanha com os parâmetros do request e vinculando o budget criado
        campaign_resource_name = create_campaign_resource(client, customer_id, budget_resource_name, request_data)
        logging.info(f"Campanha criada com sucesso: {campaign_resource_name}")

        return {"status": "success", "campaign_resource_name": campaign_resource_name}
    except GoogleAdsException as ex:
        logging.error("Erro na API do Google Ads ao criar a campanha.", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Erro na API do Google Ads: {ex}")
    except Exception as ex:
        logging.exception("Exceção inesperada ao criar a campanha.")
        raise HTTPException(status_code=500, detail=str(ex))

def get_customer_id(client: GoogleAdsClient) -> str:
    logging.info("Buscando customer ID através do CustomerService.")
    customer_service = client.get_service("CustomerService")
    response = customer_service.list_accessible_customers()
    logging.debug(f"Customers acessíveis: {response.resource_names}")

    if not response.resource_names:
        logging.error("Nenhum customer acessível encontrado.")
        raise Exception("Nenhum customer acessível foi encontrado.")

    # Seleciona o primeiro customer disponível
    customer_resource_name = response.resource_names[0]
    customer_id = customer_resource_name.split("/")[-1]
    logging.debug(f"Customer ID extraído: {customer_id} a partir do resource: {customer_resource_name}")
    return customer_id

def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_micros: int) -> str:
    logging.info("Iniciando criação do campaign budget.")
    campaign_budget_service = client.get_service("CampaignBudgetService")
    campaign_budget_operation = client.get_type("CampaignBudgetOperation")
    campaign_budget = campaign_budget_operation.create

    # Gera um nome único para o budget
    campaign_budget.name = f"Budget_{uuid.uuid4()}"
    campaign_budget.amount_micros = budget_micros
    campaign_budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    logging.debug(f"Detalhes do budget: Nome: {campaign_budget.name} | Valor (micros): {budget_micros}")
    
    try:
        response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[campaign_budget_operation]
        )
        budget_resource_name = response.results[0].resource_name
        logging.info(f"Campaign budget criado: {budget_resource_name}")
        return budget_resource_name
    except GoogleAdsException as ex:
        logging.error("Erro ao criar o campaign budget.", exc_info=True)
        raise

def create_campaign_resource(client: GoogleAdsClient, customer_id: str, budget_resource_name: str, request_data: CampaignRequest) -> str:
    logging.info("Iniciando criação da campanha.")
    campaign_service = client.get_service("CampaignService")
    campaign_operation = client.get_type("CampaignOperation")
    campaign = campaign_operation.create

    # Define o nome da campanha utilizando as keywords
    campaign.name = f"Campaign_{request_data.keyword2}_{request_data.keyword3}_{uuid.uuid4()}"
    logging.debug(f"Nome da campanha definido: {campaign.name}")

    # Mapeamento do tipo de campanha com base no parâmetro recebido
    if request_data.campaign_type.upper() == "SEARCH":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
        logging.debug("Tipo de campanha configurado como SEARCH.")
    elif request_data.campaign_type.upper() == "DISPLAY":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
        logging.debug("Tipo de campanha configurado como DISPLAY.")
    else:
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
        logging.debug("Tipo de campanha padrão (SEARCH) aplicado.")

    # Define a campanha como ativa e vincula o budget
    campaign.status = client.enums.CampaignStatusEnum.ENABLED
    campaign.campaign_budget = budget_resource_name
    campaign.start_date = request_data.start_date
    campaign.end_date = request_data.end_date
    logging.debug(f"Datas definidas para a campanha: Início = {campaign.start_date} | Fim = {campaign.end_date}")

    # Configuração da estratégia de lance com base no price_model
    if request_data.price_model.upper() == "CPC":
        campaign.manual_cpc.CopyFrom(client.get_type("ManualCpc"))
        logging.debug("Estratégia de lance: Manual CPC.")
    elif request_data.price_model.upper() == "CPM":
        campaign.manual_cpm.CopyFrom(client.get_type("ManualCpm"))
        logging.debug("Estratégia de lance: Manual CPM.")
    else:
        campaign.manual_cpc.CopyFrom(client.get_type("ManualCpc"))
        logging.debug("Estratégia de lance padrão (Manual CPC) aplicada.")

    # Observação: Para aplicar critérios de segmentação (gênero, idade, dispositivos)
    # é necessário criar recursos adicionais (CampaignCriterion, etc).
    logging.info("Configurações básicas da campanha definidas. Iniciando criação via API.")
    
    try:
        response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_operation]
        )
        campaign_resource_name = response.results[0].resource_name
        logging.info(f"Campanha criada: {campaign_resource_name}")
        return campaign_resource_name
    except GoogleAdsException as ex:
        logging.error("Erro ao criar a campanha.", exc_info=True)
        raise

if __name__ == "__main__":
    import uvicorn
    logging.info("Iniciando a aplicação com uvicorn.")
    uvicorn.run(app, host="0.0.0.0", port=8000)
