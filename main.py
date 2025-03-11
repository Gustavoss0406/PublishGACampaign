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
from PIL import Image
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

# Função para processar a imagem do logotipo e garantir que ela seja quadrada (1200x1200).
def process_logo_image(default_logo_path: str) -> bytes:
    with Image.open(default_logo_path) as img:
        img = img.convert("RGB")
        width, height = img.size
        logging.debug(f"Logo original: {width}x{height}")
        min_dim = min(width, height)
        left = (width - min_dim) / 2
        top = (height - min_dim) / 2
        right = (width + min_dim) / 2
        bottom = (height + min_dim) / 2
        img_cropped = img.crop((left, top, right, bottom))
        # Redimensiona para 1200x1200
        img_resized = img_cropped.resize((1200, 1200))
        buf = BytesIO()
        img_resized.save(buf, format="PNG")
        processed_data = buf.getvalue()
        logging.debug(f"Logo processada: tamanho {img_resized.size}, {len(processed_data)} bytes")
        return processed_data

# Função para processar a imagem de capa para garantir a proporção 1.91:1 com tamanho 1200x628.
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
    img.save(buf, format="PNG")
    processed_data = buf.getvalue()
    logging.debug(f"Cover processada: tamanho 1200x628, {len(processed_data)} bytes")
    return processed_data

# Middleware para pré-processar o corpo da requisição e limpar caracteres indesejados.
@app.middleware("http")
async def preprocess_request_body(request: Request, call_next):
    logging.info(f"Recebendo request: {request.method} {request.url}")
    logging.debug(f"Request headers: {request.headers}")
    body_bytes = await request.body()
    try:
        body_text = body_bytes.decode("utf-8")
    except Exception:
        body_text = str(body_bytes)
    # Remove ocorrências de '";' imediatamente antes de uma vírgula no campo cover_photo.
    body_text = re.sub(r'("cover_photo":\s*".+?)["\s;]+,', r'\1",', body_text)
    logging.info(f"Request body (modificado): {body_text}")
    modified_body_bytes = body_text.encode("utf-8")
    
    async def receive():
        return {"type": "http.request", "body": modified_body_bytes}
    request._receive = receive
    response = await call_next(request)
    logging.info(f"Response status: {response.status_code} para {request.method} {request.url}")
    return response

# Modelo de dados para a campanha.
class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    
    refresh_token: str
    campaign_name: str
    campaign_description: str
    objective: str
    cover_photo: str  # URL ou resource name do asset.
    keyword1: str
    keyword2: str
    keyword3: str
    budget: int  # Valor em micros (se for string, ex: "$50", será convertido).
    start_date: str  # Formato YYYYMMDD.
    end_date: str    # Formato YYYYMMDD.
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

# Função para fazer o upload da imagem; se process=True, processa a imagem (para capa ou logotipo).
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

# Função para obter o customer ID.
def get_customer_id(client: GoogleAdsClient) -> str:
    customer_service = client.get_service("CustomerService")
    accessible_customers = customer_service.list_accessible_customers()
    if not accessible_customers.resource_names:
        raise Exception("Nenhum customer acessível encontrado.")
    resource_name = accessible_customers.resource_names[0]
    logging.debug(f"Accessible customer: {resource_name}")
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
    logging.debug(f"Budget object: {campaign_budget}")
    response = campaign_budget_service.mutate_campaign_budgets(
        customer_id=customer_id, operations=[campaign_budget_operation]
    )
    resource_name = response.results[0].resource_name
    logging.info(f"Campaign Budget criado: {resource_name}")
    return resource_name

# Cria a Campaign (com sufixo único) e define o business_name igual ao campaign_name.
def create_campaign_resource(client: GoogleAdsClient, customer_id: str, budget_resource_name: str, data: CampaignRequest) -> str:
    logging.info("Criando Campaign.")
    campaign_service =
