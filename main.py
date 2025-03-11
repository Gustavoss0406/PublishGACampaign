import logging
import sys
import uuid
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

# Configuração de logs detalhados
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Handler de lifespan via asynccontextmanager
@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Startup: A aplicação foi iniciada (via lifespan handler).")
    yield
    logging.info("Shutdown: A aplicação está sendo encerrada (via lifespan handler).")

app = FastAPI(lifespan=lifespan)

# Middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ajuste conforme sua política de segurança
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware para logar detalhes da requisição (método, URL, headers e body)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logging.info(f"Recebendo request: {request.method} {request.url}")
    logging.debug(f"Request headers: {request.headers}")
    body_bytes = await request.body()
    try:
        body_text = body_bytes.decode("utf-8")
    except Exception:
        body_text = str(body_bytes)
    logging.info(f"Request body: {body_text}")
    async def receive():
        return {"type": "http.request", "body": body_bytes}
    request._receive = receive
    response = await call_next(request)
    logging.info(f"Response status: {response.status_code} para {request.method} {request.url}")
    return response

# Modelo de dados mapeando os campos do JSON recebido; permite campos extras.
class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    refresh_token: str
    campaign_name: str
    campaign_description: str
    objective: str
    cover_photo: str  # Deve ser o resource name de um asset de imagem válido
    keyword1: str
    keyword2: str
    keyword3: str
    budget: int  # valor em micros (converteremos se for string, ex: "$100")
    start_date: str  # formato YYYYMMDD
    end_date: str    # formato YYYYMMDD
    price_model: str
    campaign_type: str
    audience_gender: str
    audience_min_age: int
    audience_max_age: int
    devices: list[str]

    @field_validator("budget", mode="before")
    def convert_budget(cls, value):
        if isinstance(value, str):
            value = value.replace("$", "").strip()
            numeric_value = float(value)
            return int(numeric_value * 1_000_000)
        return value

    @field_validator("audience_min_age", "audience_max_age", mode="before")
    def convert_age(cls, value):
        if isinstance(value, str):
            return int(value)
        return value

# Endpoint que cria a campanha completa (campanha, ad group, anúncio, targeting e keywords)
@app.post("/create_campaign")
async def create_campaign(request_data: CampaignRequest):
    logging.info("Endpoint /create_campaign acionado.")
    logging.debug(f"Dados recebidos (pós-validação): {request_data.model_dump_json()}")

    try:
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
    except Exception as e:
        logging.error("Erro ao inicializar Google Ads Client.", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
        
    try:
        customer_id = get_customer_id(client)
        logging.info(f"Customer ID obtido: {customer_id}")

        budget_resource_name = create_campaign_budget(client, customer_id, request_data.budget)
        campaign_resource_name = create_campaign_resource(client, customer_id, budget_resource_name, request_data)
        ad_group_resource_name = create_ad_group(client, customer_id, campaign_resource_name, request_data)
        create_ad_group_keywords(client, customer_id, ad_group_resource_name, request_data)
        ad_group_ad_resource_name = create_responsive_display_ad(client, customer_id, ad_group_resource_name, request_data)
        apply_targeting_criteria(client, customer_id, campaign_resource_name, request_data)

        return {
            "status": "success",
            "campaign_resource_name": campaign_resource_name,
            "ad_group_resource_name": ad_group_resource_name,
            "ad_group_ad_resource_name": ad_group_ad_resource_name
        }
    except GoogleAdsException as ex:
        logging.error("Erro na API do Google Ads.", exc_info=True)
        raise HTTPException(status_code=400, detail=f"GoogleAdsException: {ex}")
    except Exception as ex:
        logging.exception("Erro inesperado.")
        raise HTTPException(status_code=500, detail=str(ex))

# Função para obter o customer ID
def get_customer_id(client: GoogleAdsClient) -> str:
    customer_service = client.get_service("CustomerService")
    accessible_customers = customer_service.list_accessible_customers()
    if not accessible_customers.resource_names:
        raise Exception("Nenhum customer acessível encontrado.")
    resource_name = accessible_customers.resource_names[0]
    return resource_name.split("/")[-1]

# Cria o Campaign Budget
def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_micros: int) -> str:
    logging.info("Criando Campaign Budget.")
    campaign_budget_service = client.get_service("CampaignBudgetService")
    campaign_budget_operation = client.get_type("CampaignBudgetOperation")
    campaign_budget = campaign_budget_operation.create
    campaign_budget.name = f"Budget_{uuid.uuid4()}"
    campaign_budget.amount_micros = budget_micros
    campaign_budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    response = campaign_budget_service.mutate_campaign_budgets(
        customer_id=customer_id, operations=[campaign_budget_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Campaign Budget criado: {resource_name}")
    return resource_name

# Cria a Campaign
def create_campaign_resource(client: GoogleAdsClient, customer_id: str, budget_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Campaign.")
    campaign_service = client.get_service("CampaignService")
    campaign_operation = client.get_type("CampaignOperation")
    campaign = campaign_operation.create
    campaign.name = data.campaign_name
    if data.campaign_type.upper() == "DISPLAY":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    else:
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    campaign.status = client.enums.CampaignStatusEnum.ENABLED
    campaign.campaign_budget = budget_resource_name
    campaign.start_date = data.start_date
    campaign.end_date = data.end_date
    # Mesmo que o price_model seja CPA, usamos fallback para Manual CPC neste exemplo.
    campaign.manual_cpc = client.get_type("ManualCpc")
    response = campaign_service.mutate_campaigns(
        customer_id=customer_id, operations=[campaign_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Campaign criado: {resource_name}")
    return resource_name

# Cria o Ad Group
def create_ad_group(client: GoogleAdsClient, customer_id: str, campaign_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Ad Group.")
    ad_group_service = client.get_service("AdGroupService")
    ad_group_operation = client.get_type("AdGroupOperation")
    ad_group = ad_group_operation.create
    ad_group.name = f"{data.campaign_name}_AdGroup_{uuid.uuid4()}"
    ad_group.campaign = campaign_resource_name
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ad_group.cpc_bid_micros = 1_000_000  # Lance de US$1.00
    response = ad_group_service.mutate_ad_groups(
        customer_id=customer_id, operations=[ad_group_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Ad Group criado: {resource_name}")
    return resource_name

# Cria as Keywords (Ad Group Criterion) para Display
def create_ad_group_keywords(client: GoogleAdsClient, customer_id: str, ad_group_resource_name: str, data: CampaignRequest):
    logging.info("Criando Display Keywords no Ad Group.")
    ad_group_criterion_service = client.get_service("AdGroupCriterionService")
    operations = []
    def make_keyword_op(keyword_text: str):
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.create
        criterion.ad_group = ad_group_resource_name
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        criterion.keyword.text = keyword_text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
        return op
    for kw in [data.keyword1, data.keyword2, data.keyword3]:
        if kw:
            operations.append(make_keyword_op(kw))
    if operations:
        response = ad_group_criterion_service.mutate_ad_group_criteria(
            customer_id=customer_id, operations=operations
        )
        for result in response.results:
            logging.info(f"Ad Group Criterion criado: {result.resource_name}")

# Cria o Responsive Display Ad usando os dados do JSON
def create_responsive_display_ad(client: GoogleAdsClient, customer_id: str, ad_group_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Responsive Display Ad.")
    ad_group_ad_service = client.get_service("AdGroupAdService")
    ad_group_ad_operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad = ad_group_ad.ad
    ad.final_urls.append("https://example.com")  # URL de destino – ajuste conforme necessário

    # Headlines: usando keyword1, keyword2 e keyword3
    headline1 = client.get_type("AdTextAsset")()
    headline1.text = data.keyword1 if data.keyword1 else data.campaign_name
    ad.responsive_display_ad.headlines.append(headline1)
    headline2 = client.get_type("AdTextAsset")()
    headline2.text = data.keyword2
    ad.responsive_display_ad.headlines.append(headline2)
    headline3 = client.get_type("AdTextAsset")()
    headline3.text = data.keyword3
    ad.responsive_display_ad.headlines.append(headline3)

    # Descrições: usando campaign_description e objective
    desc1 = client.get_type("AdTextAsset")()
    desc1.text = data.campaign_description
    ad.responsive_display_ad.descriptions.append(desc1)
    desc2 = client.get_type("AdTextAsset")()
    desc2.text = data.objective
    ad.responsive_display_ad.descriptions.append(desc2)

    # Business name
    ad.responsive_display_ad.business_name = data.campaign_name

    # Marketing image: utiliza cover_photo; se vazio, lança exceção
    if data.cover_photo:
        img = client.get_type("AdImageAsset")()
        img.asset = data.cover_photo
        ad.responsive_display_ad.marketing_images.append(img)
    else:
        raise Exception("O campo 'cover_photo' deve conter o resource name de um asset de imagem válido.")

    # Logo: utiliza um asset definido via variável de ambiente
    default_logo = os.environ.get("DEFAULT_LOGO_ASSET")
    if not default_logo:
        raise Exception("Variável de ambiente DEFAULT_LOGO_ASSET não definida.")
    logo = client.get_type("AdImageAsset")()
    logo.asset = default_logo
    ad.responsive_display_ad.logo_images.append(logo)

    response = ad_group_ad_service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[ad_group_ad_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Responsive Display Ad criado: {resource_name}")
    return resource_name

# Aplica critérios de targeting à campanha (gênero, idade e dispositivos)
def apply_targeting_criteria(client: GoogleAdsClient, customer_id: str, campaign_resource_name: str, data: CampaignRequest):
    logging.info("Aplicando targeting na Campaign.")
    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operations = []
    gender_mapping = {
        "MALE": client.enums.GenderTypeEnum.MALE,
        "FEMALE": client.enums.GenderTypeEnum.FEMALE
    }
    gender = gender_mapping.get(data.audience_gender.upper())
    if gender is not None:
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = campaign_resource_name
        criterion.gender.type_ = gender
        criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
        operations.append(op)
    if data.audience_min_age <= 18 <= data.audience_max_age:
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = campaign_resource_name
        criterion.age_range.type_ = client.enums.AgeRangeTypeEnum.AGE_RANGE_18_24
        criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
        operations.append(op)
    device_mapping = {
        "SMARTPHONE": client.enums.DeviceEnum.MOBILE,
        "DESKTOP": client.enums.DeviceEnum.DESKTOP,
        "TABLET": client.enums.DeviceEnum.TABLET
    }
    for d in data.devices:
        d_upper = d.strip().upper()
        if d_upper in device_mapping:
            op = client.get_type("CampaignCriterionOperation")
            criterion = op.create
            criterion.campaign = campaign_resource_name
            criterion.device.type_ = device_mapping[d_upper]
            criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
            operations.append(op)
    if operations:
        response = campaign_criterion_service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        for result in response.results:
            logging.info(f"Campaign Criterion criado: {result.resource_name}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logging.info(f"Iniciando a aplicação com uvicorn no host 0.0.0.0 e porta {port}.")
    uvicorn.run(app, host="0.0.0.0", port=port)
