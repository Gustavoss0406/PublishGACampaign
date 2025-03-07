import logging
import sys
import uuid
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Configuração detalhada dos logs (DEBUG) com saída para stdout
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Handler de lifespan sem uso de @app.on_event
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Startup: A aplicação foi iniciada (via lifespan handler).")
    yield
    logging.info("Shutdown: A aplicação está sendo encerrada (via lifespan handler).")

app = FastAPI(lifespan=lifespan)

# Middleware CORS para tratar requisições OPTIONS e evitar problemas de preflight
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ajuste conforme sua política de segurança
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware para logar detalhes de cada requisição (método, URL, headers e body)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logging.info(f"Recebendo request: {request.method} {request.url}")
    logging.debug(f"Request headers: {request.headers}")

    # Lê o corpo da requisição e faz log
    body_bytes = await request.body()
    try:
        body_text = body_bytes.decode("utf-8")
    except Exception:
        body_text = str(body_bytes)
    logging.info(f"Request body: {body_text}")

    # Reatribui a função _receive para que o body esteja disponível para o endpoint
    async def receive():
        return {"type": "http.request", "body": body_bytes}
    request._receive = receive

    response = await call_next(request)
    logging.info(f"Response status: {response.status_code} para {request.method} {request.url}")
    return response

# Modelo de dados para o corpo da requisição, com suporte a campos extras e validadores
class CampaignRequest(BaseModel):
    refresh_token: str
    keyword2: str
    keyword3: str
    budget: int  # valor em micros (será convertido)
    start_date: str  # formato YYYYMMDD
    end_date: str    # formato YYYYMMDD
    price_model: str
    campaign_type: str
    audience_gender: str
    audience_min_age: int
    audience_max_age: int
    devices: list[str]  # exemplo: ["mobile", "desktop"]

    class Config:
        extra = "allow"  # Permite campos extras que serão ignorados

    @validator("budget", pre=True)
    def convert_budget(cls, value):
        if isinstance(value, str):
            # Remove o símbolo "$" se presente e converte para float
            value = value.replace("$", "").strip()
            try:
                numeric_value = float(value)
            except Exception as e:
                raise ValueError("Formato inválido para budget. Exemplo esperado: '$100'")
            # Converte o valor para micros (multiplicando por 1e6)
            return int(numeric_value * 1_000_000)
        return value

    @validator("audience_min_age", "audience_max_age", pre=True)
    def convert_age(cls, value):
        if isinstance(value, str):
            try:
                return int(value)
            except Exception as e:
                raise ValueError("Idade deve ser um número inteiro.")
        return value

@app.post("/create_campaign")
async def create_campaign(request_data: CampaignRequest):
    logging.info("Endpoint /create_campaign acionado.")
    # Utiliza model_dump_json() em vez de json()
    logging.debug(f"Dados recebidos (pós-validação): {request_data.model_dump_json()}")

    try:
        # Configuração do Google Ads Client utilizando o refresh token do usuário
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

        # Criação da campanha vinculando o budget criado e os demais parâmetros
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

    # Seleciona o primeiro customer retornado e extrai o ID
    customer_resource_name = response.resource_names[0]
    customer_id = customer_resource_name.split("/")[-1]
    logging.debug(f"Customer ID extraído: {customer_id} do resource: {customer_resource_name}")
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
        logging.info(f"Budget criado com sucesso: {budget_resource_name}")
        return budget_resource_name
    except GoogleAdsException as ex:
        logging.error("Erro ao criar o campaign budget.", exc_info=True)
        raise

def create_campaign_resource(client: GoogleAdsClient, customer_id: str, budget_resource_name: str, request_data: CampaignRequest) -> str:
    logging.info("Iniciando criação da campanha.")
    campaign_service = client.get_service("CampaignService")
    campaign_operation = client.get_type("CampaignOperation")
    campaign = campaign_operation.create

    # Define o nome da campanha com base nas keywords e um identificador único
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
    logging.debug(f"Datas da campanha definidas: Início = {campaign.start_date} | Fim = {campaign.end_date}")

    # Configuração da estratégia de lance com base no price_model
    if request_data.price_model.upper() == "CPC":
        campaign.manual_cpc = client.get_type("ManualCpc")()
        logging.debug("Estratégia de lance configurada: Manual CPC.")
    elif request_data.price_model.upper() == "CPM":
        campaign.manual_cpm = client.get_type("ManualCpm")()
        logging.debug("Estratégia de lance configurada: Manual CPM.")
    else:
        # Fallback para Manual CPC para modelos não tratados (ex: CPA)
        campaign.manual_cpc = client.get_type("ManualCpc")()
        logging.debug("Estratégia de lance padrão (Manual CPC) aplicada.")

    logging.info("Configurações básicas da campanha definidas. Enviando criação via API.")
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
    # Utiliza a porta definida na variável de ambiente PORT (compatível com Railway e outras plataformas)
    port = int(os.environ.get("PORT", 8000))
    logging.info(f"Iniciando a aplicação com uvicorn no host 0.0.0.0 e porta {port}.")
    uvicorn.run(app, host="0.0.0.0", port=port)
