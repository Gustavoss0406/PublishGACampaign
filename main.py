import logging
import sys
import uuid
import os
import re
import json
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, ConfigDict
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
import requests
from PIL import Image, ImageOps
from io import BytesIO

# Configuração de logs detalhados
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.info("Startup: A aplicação foi iniciada (via lifespan handler).")
    yield
    logging.info("Shutdown: A aplicação está sendo encerrada (via lifespan handler).")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ajuste conforme necessário
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def process_logo_image(logo_path: str) -> bytes:
    """
    Carrega o logotipo a partir do arquivo padrao.jpg.
    Assume que o arquivo já possui exatamente 1200x1200 pixels.
    Apenas remove metadados e converte para JPEG.
    (Esta função não será utilizada, pois removemos o logo.)
    """
    try:
        if not os.path.exists(logo_path):
            raise FileNotFoundError(f"Arquivo {logo_path} não encontrado.")
        with Image.open(logo_path) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            logging.debug(f"Logo original: {img.size}")
            if img.size != (1200, 1200):
                raise ValueError("A imagem do logotipo deve ter exatamente 1200x1200 pixels.")
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=95)
            processed_data = buf.getvalue()
            logging.debug(f"Logo processada: tamanho {img.size}, {len(processed_data)} bytes")
            return processed_data
    except Exception as e:
        logging.error(f"Erro ao processar o logotipo: {e}")
        raise

def process_cover_photo(image_data: bytes) -> bytes:
    img = Image.open(BytesIO(image_data))
    width, height = img.size
    target_ratio = 1.91
    current_ratio = width / height
    logging.debug(f"Cover original: {width}x{height}, razão: {current_ratio:.2f}")
    if current_ratio > target_ratio:
        new_width = int(height * target_ratio)
        left = (width - new_width) // 2
        img = img.crop((left, 0, left + new_width, height))
    elif current_ratio < target_ratio:
        new_height = int(width / target_ratio)
        top = (height - new_height) // 2
        img = img.crop((0, top, width, top + new_height))
    img = img.resize((1200, 628))
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    processed_data = buf.getvalue()
    logging.debug(f"Cover processada: tamanho 1200x628, {len(processed_data)} bytes")
    return processed_data

def process_square_image(image_data: bytes) -> bytes:
    img = Image.open(BytesIO(image_data))
    img = img.convert("RGB")
    width, height = img.size
    min_dim = min(width, height)
    left = (width - min_dim) // 2
    top = (height - min_dim) // 2
    right = left + min_dim
    bottom = top + min_dim
    img_cropped = img.crop((left, top, right, bottom))
    img_resized = img_cropped.resize((1200, 1200))
    buf = BytesIO()
    img_resized.save(buf, format="PNG", optimize=True)
    processed_data = buf.getvalue()
    logging.debug(f"Imagem quadrada processada: tamanho {img_resized.size}, {len(processed_data)} bytes")
    return processed_data

def upload_image_asset(client: GoogleAdsClient, customer_id: str, image_url: str, process: bool = False) -> str:
    logging.info(f"Fazendo download da imagem a partir do URL: {image_url}")
    response = requests.get(image_url)
    if response.status_code != 200:
        raise Exception(f"Falha ao fazer download da imagem. Status: {response.status_code}")
    image_data = response.content
    if process:
        image_data = process_cover_photo(image_data)
    asset_service = client.get_service("AssetService")
    asset_operation = client.get_type("AssetOperation")
    asset = asset_operation.create
    asset.name = f"Image_asset_{uuid.uuid4()}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = image_data
    mutate_response = asset_service.mutate_assets(customer_id=customer_id, operations=[asset_operation])
    resource_name = mutate_response.results[0].resource_name
    logging.info(f"Imagem enviada com sucesso. Resource name: {resource_name}")
    return resource_name

def upload_square_image_asset(client: GoogleAdsClient, customer_id: str, image_url: str) -> str:
    logging.info(f"Fazendo download da imagem quadrada a partir do URL: {image_url}")
    response = requests.get(image_url)
    if response.status_code != 200:
        raise Exception(f"Falha ao fazer download da imagem. Status: {response.status_code}")
    image_data = response.content
    processed_data = process_square_image(image_data)
    asset_service = client.get_service("AssetService")
    asset_operation = client.get_type("AssetOperation")
    asset = asset_operation.create
    asset.name = f"Square_Image_asset_{uuid.uuid4()}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = processed_data
    mutate_response = asset_service.mutate_assets(customer_id=customer_id, operations=[asset_operation])
    resource_name = mutate_response.results[0].resource_name
    logging.info(f"Imagem quadrada enviada com sucesso. Resource name: {resource_name}")
    return resource_name

def get_customer_id(client: GoogleAdsClient) -> str:
    customer_service = client.get_service("CustomerService")
    accessible_customers = customer_service.list_accessible_customers()
    if not accessible_customers.resource_names:
        raise Exception("Nenhum customer acessível encontrado.")
    resource_name = accessible_customers.resource_names[0]
    logging.debug(f"Accessible customer: {resource_name}")
    return resource_name.split("/")[-1]

@app.middleware("http")
async def preprocess_request_body(request: Request, call_next):
    logging.info(f"Recebendo request: {request.method} {request.url}")
    logging.debug(f"Request headers: {request.headers}")
    body_bytes = await request.body()
    try:
        body_text = body_bytes.decode("utf-8")
    except Exception:
        body_text = str(body_bytes)
    body_text = re.sub(r'("cover_photo":\s*".+?)[\";]+\s*,', r'\1",', body_text, flags=re.DOTALL)
    logging.info(f"Request body (modificado): {body_text}")
    modified_body_bytes = body_text.encode("utf-8")
    
    async def receive():
        return {"type": "http.request", "body": modified_body_bytes}
    request._receive = receive
    response = await call_next(request)
    logging.info(f"Response status: {response.status_code} para {request.method} {request.url}")
    return response

class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    refresh_token: str
    campaign_name: str
    campaign_description: str
    objective: str
    cover_photo: str
    final_url: str  # Novo campo para a URL final
    keyword1: str
    keyword2: str
    keyword3: str
    budget: int
    start_date: str
    end_date: str
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
            logging.debug(f"Budget convertido: {numeric_value}")
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
            cleaned = value.strip().rstrip(" ;")
            if cleaned and not urlparse(cleaned).scheme:
                cleaned = "http://" + cleaned
            logging.debug(f"Cover photo após limpeza: {cleaned}")
            return cleaned
        return value

def create_campaign_budget(client: GoogleAdsClient, customer_id: str, budget_micros: int) -> str:
    logging.info("Criando Campaign Budget.")
    campaign_budget_service = client.get_service("CampaignBudgetService")
    campaign_budget_operation = client.get_type("CampaignBudgetOperation")
    campaign_budget = campaign_budget_operation.create
    campaign_budget.name = f"Budget_{uuid.uuid4()}"
    campaign_budget.amount_micros = budget_micros
    campaign_budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    logging.debug(f"Budget object: {campaign_budget}")
    response = campaign_budget_service.mutate_campaign_budgets(
        customer_id=customer_id, operations=[campaign_budget_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Campaign Budget criado: {resource_name}")
    return resource_name

def create_campaign_resource(client: GoogleAdsClient, customer_id: str, budget_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Campaign.")
    campaign_service = client.get_service("CampaignService")
    campaign_operation = client.get_type("CampaignOperation")
    campaign = campaign_operation.create
    unique_campaign_name = f"{data.campaign_name.strip()}_{uuid.uuid4().hex[:6]}"
    campaign.name = unique_campaign_name
    logging.debug(f"Nome da campanha único: {campaign.name}")
    if data.campaign_type.upper() == "DISPLAY":
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    else:
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    campaign.status = client.enums.CampaignStatusEnum.ENABLED
    campaign.campaign_budget = budget_resource_name
    campaign.start_date = data.start_date
    campaign.end_date = data.end_date
    campaign.manual_cpc = client.get_type("ManualCpc")
    logging.debug(f"Campaign object: {campaign}")
    response = campaign_service.mutate_campaigns(
        customer_id=customer_id, operations=[campaign_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Campaign criado: {resource_name}")
    return resource_name

def create_ad_group(client: GoogleAdsClient, customer_id: str, campaign_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Ad Group.")
    ad_group_service = client.get_service("AdGroupService")
    ad_group_operation = client.get_type("AdGroupOperation")
    ad_group = ad_group_operation.create
    ad_group.name = f"{data.campaign_name.strip()}_AdGroup_{uuid.uuid4().hex[:6]}"
    ad_group.campaign = campaign_resource_name
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    ad_group.type_ = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ad_group.cpc_bid_micros = 1_000_000
    logging.debug(f"Ad Group object: {ad_group}")
    response = ad_group_service.mutate_ad_groups(
        customer_id=customer_id, operations=[ad_group_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Ad Group criado: {resource_name}")
    return resource_name

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
        logging.debug(f"Keyword object: {criterion.keyword}")
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

def create_responsive_display_ad(client: GoogleAdsClient, customer_id: str, ad_group_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Responsive Display Ad.")
    ad_group_ad_service = client.get_service("AdGroupAdService")
    ad_group_ad_operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_resource_name
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad = ad_group_ad.ad
    # Agora a URL final é obtida do campo final_url do JSON
    ad.final_urls.append(data.final_url)
    
    # Headlines (títulos curtos)
    headline1 = client.get_type("AdTextAsset")
    headline1.text = data.keyword1 if data.keyword1 else data.campaign_name.strip()
    ad.responsive_display_ad.headlines.append(headline1)
    logging.debug(f"Headline 1: {headline1.text}")
    
    headline2 = client.get_type("AdTextAsset")
    headline2.text = data.keyword2
    ad.responsive_display_ad.headlines.append(headline2)
    logging.debug(f"Headline 2: {headline2.text}")
    
    headline3 = client.get_type("AdTextAsset")
    headline3.text = data.keyword3
    ad.responsive_display_ad.headlines.append(headline3)
    logging.debug(f"Headline 3: {headline3.text}")
    
    # Descrições
    desc1 = client.get_type("AdTextAsset")
    desc1.text = data.campaign_description
    ad.responsive_display_ad.descriptions.append(desc1)
    logging.debug(f"Descrição 1: {desc1.text}")
    
    desc2 = client.get_type("AdTextAsset")
    desc2.text = data.objective
    ad.responsive_display_ad.descriptions.append(desc2)
    logging.debug(f"Descrição 2: {desc2.text}")
    
    ad.responsive_display_ad.business_name = data.campaign_name.strip()
    logging.debug(f"Business name definido: {ad.responsive_display_ad.business_name}")
    
    # Campo obrigatório: Long Headline
    ad.responsive_display_ad.long_headline.text = f"{data.campaign_name.strip()} - {data.objective.strip()}"
    logging.debug(f"Long Headline definido: {ad.responsive_display_ad.long_headline.text}")
    
    if data.cover_photo:
        if data.cover_photo.startswith("http"):
            marketing_asset_resource = upload_image_asset(client, customer_id, data.cover_photo, process=True)
            square_asset_resource = upload_square_image_asset(client, customer_id, data.cover_photo)
        else:
            marketing_asset_resource = data.cover_photo
            square_asset_resource = data.cover_photo
        img = client.get_type("AdImageAsset")
        img.asset = marketing_asset_resource
        ad.responsive_display_ad.marketing_images.append(img)
        logging.debug(f"Marketing image asset: {img.asset}")
        square_img = client.get_type("AdImageAsset")
        square_img.asset = square_asset_resource
        ad.responsive_display_ad.square_marketing_images.append(square_img)
        logging.debug(f"Square marketing image asset: {square_img.asset}")
    else:
        raise Exception("O campo 'cover_photo' está vazio.")
    
    response = ad_group_ad_service.mutate_ad_group_ads(
        customer_id=customer_id, operations=[ad_group_ad_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Responsive Display Ad criado: {resource_name}")
    return resource_name

# Como vamos aplicar apenas a segmentação por gênero,
# utilizamos a abordagem de exclusão: se o usuário deseja atingir um determinado gênero,
# excluímos os outros.
def apply_targeting_criteria(client: GoogleAdsClient, customer_id: str, campaign_resource_name: str, data: CampaignRequest):
    logging.info("Aplicando targeting na Campaign.")
    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operations = []
    if data.audience_gender and data.audience_gender.upper() in ["MALE", "FEMALE"]:
        desired_gender = data.audience_gender.upper()
        # Se deseja atingir somente MALE, excluímos FEMALE e UNDETERMINED.
        # Se deseja atingir somente FEMALE, excluímos MALE e UNDETERMINED.
        if desired_gender == "MALE":
            exclusions = ["FEMALE", "UNDETERMINED"]
        else:
            exclusions = ["MALE", "UNDETERMINED"]
        for gender_to_exclude in exclusions:
            op = client.get_type("CampaignCriterionOperation")
            criterion = op.create
            criterion.campaign = campaign_resource_name
            criterion.gender.type_ = client.enums.GenderTypeEnum[gender_to_exclude]
            # Usamos negative=True para excluir o gênero indesejado
            criterion.negative = True
            criterion.status = client.enums.CampaignCriterionStatusEnum.ENABLED
            operations.append(op)
    if operations:
        response = campaign_criterion_service.mutate_campaign_criteria(
            customer_id=customer_id, operations=operations
        )
        for result in response.results:
            logging.info(f"Campaign Criterion criado: {result.resource_name}")

@app.post("/create_campaign")
async def create_campaign(request_data: CampaignRequest):
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
        logging.error(f"Erro na API do Google Ads: {ex.failure}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"GoogleAdsException: {ex.failure}")
    except Exception as ex:
        logging.exception("Erro inesperado.")
        raise HTTPException(status_code=500, detail=str(ex))

app.post("/create_campaign/")(create_campaign)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    logging.info(f"Iniciando a aplicação com uvicorn no host 0.0.0.0 e porta {port}.")
    uvicorn.run(app, host="0.0.0.0", port=port)
