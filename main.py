import logging
import sys
import uuid
import os
import re
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import requests

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
    allow_origins=["*"],  # Ajuste conforme necessário
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware para logar e limpar o corpo da requisição
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logging.info(f"Recebendo request: {request.method} {request.url}")
    logging.debug(f"Request headers: {request.headers}")
    body_bytes = await request.body()
    try:
        body_text = body_bytes.decode("utf-8")
    except Exception:
        body_text = str(body_bytes)
    
    # Tenta remover a ocorrência de '";' ao final do valor do cover_photo
    body_text = re.sub(r'("cover_photo":\s*".+?)[;\s]+"', r'\1"', body_text)
    logging.info(f"Request body (modificado): {body_text}")
    modified_body_bytes = body_text.encode("utf-8")
    
    async def receive():
        return {"type": "http.request", "body": modified_body_bytes}
    request._receive = receive
    response = await call_next(request)
    logging.info(f"Response status: {response.status_code} para {request.method} {request.url}")
    return response

# Modelo de dados para a campanha; permite campos extras.
class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    refresh_token: str
    campaign_name: str
    campaign_description: str
    objective: str
    cover_photo: str  # URL ou resource name do asset
    # Campo "logo_image" removido; será sempre utilizado o asset padrão.
    keyword1: str
    keyword2: str
    keyword3: str
    budget: int  # valor em micros (converteremos se for string, ex: "$50")
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

    @field_validator("cover_photo", mode="before")
    def clean_cover_photo(cls, value):
        if isinstance(value, str):
            # Remove espaços e qualquer ponto-e-vírgula no final da string
            cleaned = re.sub(r'[;\s]+$', '', value.strip())
            # Se a URL não tiver esquema, adicione "http://"
            if cleaned and not urlparse(cleaned).scheme:
                cleaned = "http://" + cleaned
            return cleaned
        return value

# Função para fazer o upload da imagem a partir de um URL e retornar o resource name do asset.
def upload_image_asset(client: GoogleAdsClient, customer_id: str, image_url: str) -> str:
    logging.info(f"Fazendo download da imagem a partir do URL: {image_url}")
    response = requests.get(image_url)
    if response.status_code != 200:
        raise Exception(f"Falha ao fazer download da imagem. Status: {response.status_code}")
    image_data = response.content
    asset_service = client.get_service("AssetService")
    asset_operation = client.get_type("AssetOperation")
    asset = asset_operation.create
    asset.name = f"Image asset {uuid.uuid4()}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = image_data
    mutate_response = asset_service.mutate_assets(customer_id=customer_id, operations=[asset_operation])
    resource_name = mutate_response.results[0].resource_name
    logging.info(f"Imagem enviada com sucesso. Resource name: {resource_name}")
    return resource_name

# Função para obter o customer ID.
def get_customer_id(client: GoogleAdsClient) -> str:
    customer_service = client.get_service("CustomerService")
    accessible_customers = customer_service.list_accessible_customers()
    if not accessible_customers.resource_names:
        raise Exception("Nenhum customer acessível encontrado.")
    resource_name = accessible_customers.resource_names[0]
    return resource_name.split("/")[-1]

# Cria o Campaign Budget.
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

# Cria a Campaign.
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
    campaign.manual_cpc = client.get_type("ManualCpc")
    response = campaign_service.mutate_campaigns(
        customer_id=customer_id, operations=[campaign_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Campaign criado: {resource_name}")
    return resource_name

# Cria o Ad Group.
def create_ad_group(client: GoogleAdsClient, customer_id: str, campaign_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Ad Group.")
    ad_group_service = client.get_service("AdGroupService")
    ad_group_operation = client.get_type("AdGroupOperation")
    ad_group = ad_group_operation.create
    ad_group.name = f"{data.campaign_name}_AdGroup_{uuid.uuid4()}"
    ad_group.campaign = campaign_resource_name
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ad_group.cpc_bid_micros = 1_000_000
    response = ad_group_service.mutate_ad_groups(
        customer_id=customer_id, operations=[ad_group_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Ad Group criado: {resource_name}")
    return resource_name

# Cria as Keywords (Ad Group Criterion) para Display.
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

# Cria o Responsive Display Ad usando os dados do JSON.
def create_responsive_display_ad(client: GoogleAdsClient, customer_id: str, ad_group_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Responsive Display Ad.")
    ad_group_ad_service = client.get_service("AdGroupAdService")
    ad_group_ad_operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad = ad_group_ad.ad
    ad.final_urls.append("https://example.com")
    
    # Headlines
    headline1 = client.get_type("AdTextAsset")
    headline1.text = data.keyword1 if data.keyword1 else data.campaign_name
    ad.responsive_display_ad.headlines.append(headline1)
    
    headline2 = client.get_type("AdTextAsset")
    headline2.text = data.keyword2
    ad.responsive_display_ad.headlines.append(headline2)
    
    headline3 = client.get_type("AdTextAsset")
    headline3.text = data.keyword3
    ad.responsive_display_ad.headlines.append(headline3)
    
    # Descrições
    desc1 = client.get_type("AdTextAsset")
    desc1.text = data.campaign_description
    ad.responsive_display_ad.descriptions.append(desc1)
    
    desc2 = client.get_type("AdTextAsset")
    desc2.text = data.objective
    ad.responsive_display_ad.descriptions.append(desc2)
    
    # Business name
    ad.responsive_display_ad.business_name = data.campaign_name
    
    # Marketing image: se cover_photo for um URL, faz o upload; caso contrário, assume que já é o resource name.
    if data.cover_photo:
        if data.cover_photo.startswith("http"):
            marketing_asset_resource = upload_image_asset(client, customer_id, data.cover_photo)
        else:
            marketing_asset_resource = data.cover_photo
        img = client.get_type("AdImageAsset")
        img.asset = marketing_asset_resource
        ad.responsive_display_ad.marketing_images.append(img)
    else:
        raise Exception("O campo 'cover_photo' está vazio. É necessário fornecer um URL ou resource name válido.")
    
    # Logo: sempre utiliza o asset padrão.
    logo_asset_resource = os.environ.get("DEFAULT_LOGO_ASSET")
    if not logo_asset_resource:
        default_logo_path = "icon-Adstock-Vetor2.png"
        if not os.path.exists(default_logo_path):
            raise Exception("Nenhum asset de logotipo foi fornecido, DEFAULT_LOGO_ASSET não está definida e o arquivo icon-Adstock-Vetor2.png não foi encontrado.")
        with open(default_logo_path, "rb") as f:
            image_data = f.read()
        asset_service = client.get_service("AssetService")
        asset_operation = client.get_type("AssetOperation")
        asset = asset_operation.create
        asset.name = f"Default Logo {uuid.uuid4()}"
        asset.type_ = client.enums.AssetTypeEnum.IMAGE
        asset.image_asset.data = image_data
        mutate_response = asset_service.mutate_assets(customer_id=customer_id, operations=[asset_operation])
        logo_asset_resource = mutate_response.results[0].resource_name
    
    logo = client.get_type("AdImageAsset")
    logo.asset = logo_asset_resource
    ad.responsive_display_ad.logo_images.append(logo)
    
    response = ad_group_ad_service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[ad_group_ad_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Responsive Display Ad criado: {resource_name}")
    return resource_name

# Aplica critérios de targeting à campanha (gênero, idade e dispositivos).
def apply_targeting_criteria(client: GoogleAdsClient, customer_id: str, campaign_resource_name: str, data: CampaignRequest):
    logging.info("Aplicando targeting na Campaign.")
    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operations = []
    # Gênero
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
    # Idade – exemplo para faixa 18-24
    if data.audience_min_age <= 18 <= data.audience_max_age:
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = campaign_resource_name
        criterion.age_range.type_ = client.enums.AgeRangeTypeEnum.AGE_RANGE_18_24
        criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
        operations.append(op)
    # Dispositivos
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
