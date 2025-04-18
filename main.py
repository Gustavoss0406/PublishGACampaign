from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, validator, constr
from typing import Optional
from datetime import datetime, date
import re, json, urllib.parse
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

app = FastAPI()

# Middleware para limpar JSON malformatado
class JSONCleanupMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.headers.get("content-type", "").startswith("application/json"):
            raw_body = await request.body()
            if raw_body:
                text = raw_body.decode("utf-8", errors="ignore")
                # Correções simples de formatação:
                text = text.replace(";,", ",")            # corrige ";," -> ","
                text = re.sub(r",\s*,", ",", text)        # remove vírgulas duplicadas
                text = re.sub(r",\s*([\]\}])", r"\1", text)  # remove vírgula antes de ']' ou '}' 
                # Remover chaves duplicadas simples (mantém a última ocorrência):
                # (Conversão para dict e volta para JSON para eliminar duplicatas)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    # JSON ainda inválido após limpeza
                    return JSONResponse(
                        {"detail": "JSON mal formatado"}, status_code=422
                    )
                # Se conseguiu carregar, re-dump para string para garantir formatação JSON válida
                text = json.dumps(data)
                # Substituir o corpo da request pelo JSON limpo
                async def receive() -> dict:
                    return {"type": "http.request", "body": text.encode("utf-8"), "more_body": False}
                request._receive = receive  # injeta nova função de leitura do corpo
        # Chama a próxima etapa (rota ou próximo middleware)
        response = await call_next(request)
        return response

app.add_middleware(JSONCleanupMiddleware)

# Modelo Pydantic para o payload da campanha
class CampaignPayload(BaseModel):
    name: str
    objective: constr(strip_whitespace=True, min_length=1)  # será validado pelo código (lista de válidos)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    budget: constr(regex=r'^\$\d+(\.\d{1,2})?$')  # ex: "$32.50"
    cover_photo: Optional[str] = None
    final_url: Optional[str] = None

    # Validar formato das datas (MM/DD/YYYY)
    @validator("start_date", "end_date", pre=True)
    def validate_date(cls, value):
        if value is None:
            return value
        try:
            # Tenta converter a string para date no formato MM/DD/YYYY
            dt = datetime.strptime(value, "%m/%d/%Y")
            return dt.date()  # converte para date
        except Exception:
            raise ValueError("Data deve estar no formato MM/DD/YYYY")
    
    # Validar URLs (com ou sem protocolo)
    @validator("cover_photo", "final_url")
    def validate_url(cls, url):
        if url is None:
            return url
        # Se não começa com http:// ou https://, adiciona http:// temporariamente para validar
        test_url = url
        if not re.match(r'^[a-zA-Z]+://', url):
            test_url = "http://" + url
        try:
            parsed = urllib.parse.urlparse(test_url)
            # Verifica se possui ao menos domínio e esquema (no caso adicionado ou existente)
            if not parsed.netloc or not parsed.scheme:
                raise ValueError
        except Exception:
            raise ValueError("URL inválida")
        return url

# Credenciais fictícias para Google Ads API
google_ads_config = {
    "developer_token": "D4yv61IQ8R0JaE5dxrd1Uw",
    "client_id": "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com",
    "client_secret": "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",
    "refresh_token": "TEST_REFRESH_TOKEN",
    "use_proto_plus": True
}
# Inicializa cliente Google Ads fora da rota para reutilizar
google_ads_client = GoogleAdsClient.load_from_dict(google_ads_config, version="v16")

# Função de criação de campanha (executada em background)
def create_campaign_task(payload: CampaignPayload):
    customer_id = "1234567890"  # ID do cliente Google Ads (fictício)
    try:
        # 1. Criação do orçamento da campanha
        campaign_budget_service = google_ads_client.get_service("CampaignBudgetService")
        budget_op = google_ads_client.get_type("CampaignBudgetOperation")
        campaign_budget = budget_op.create
        campaign_budget.name = f"Budget {payload.name}"
        campaign_budget.delivery_method = google_ads_client.enums.BudgetDeliveryMethodEnum.STANDARD
        # Converte budget string "$X.YZ" para micros (int)
        budget_value = float(payload.budget.replace("$", ""))
        campaign_budget.amount_micros = int(budget_value * 1_000_000)
        # Executa a chamada para criar o budget
        budget_response = campaign_budget_service.mutate_campaign_budgets(
            customer_id=customer_id, operations=[budget_op]
        )
        budget_resource = budget_response.results[0].resource_name

        # 2. Criação da campanha
        campaign_service = google_ads_client.get_service("CampaignService")
        campaign_op = google_ads_client.get_type("CampaignOperation")
        campaign = campaign_op.create
        campaign.name = payload.name
        campaign.advertising_channel_type = google_ads_client.enums.AdvertisingChannelTypeEnum.SEARCH
        campaign.status = google_ads_client.enums.CampaignStatusEnum.PAUSED
        # Associa o orçamento criado
        campaign.campaign_budget = budget_resource  # linka a campanha ao orçamento
        # Define datas de início e término, se fornecidas
        if isinstance(payload.start_date, date):
            # Formata para YYYY-MM-DD conforme exigido pela API
            campaign.start_date = payload.start_date.strftime("%Y-%m-%d")
        if isinstance(payload.end_date, date):
            campaign.end_date = payload.end_date.strftime("%Y-%m-%d")
        # 3. Estratégia de lance automática baseada no objetivo
        objective = payload.objective.lower()  # usar lower-case para comparar
        try:
            if objective in ["vendas", "leads", "promover site/app"]:
                # Maximize Clicks via TargetSpend
                campaign.target_spend = google_ads_client.get_type("TargetSpend")()
                # (Opcional: definir teto de CPC, ex: $5.00)
                # campaign.target_spend.cpc_bid_ceiling_micros = 5 * 1_000_000
            elif objective == "alcance de marca":
                tis = google_ads_client.get_type("TargetImpressionShare")()
                # Alvo: qualquer lugar na página, 100% das impressões (se possível)
                tis.location = google_ads_client.enums.TargetImpressionShareLocationEnum.ANYWHERE_ON_PAGE
                tis.location_fraction_micros = 1_000_000  # 100% das impressões
                # Opcional: definir CPC máximo, ex: $2.00
                # tis.cpc_bid_ceiling_micros = 2 * 1_000_000
                campaign.target_impression_share = tis
            else:
                # Objetivo não reconhecido (fallback de segurança)
                raise ValueError("Objetivo inválido")
        except Exception as e:
            # Se objetivo inválido foi detectado
            print(f"Erro: {e}")
            return  # aborta antes de criar campanha
        # 4. Chama API para criar a campanha
        campaign_response = campaign_service.mutate_campaigns(
            customer_id=customer_id, operations=[campaign_op]
        )
        new_campaign_id = campaign_response.results[0].resource_name
        print(f"Campanha criada: {new_campaign_id}")
    except GoogleAdsException as gae:
        # Lida com erros da API Google Ads (ex.: token inválido, orçamento baixo, etc.)
        error_msgs = [err.message for err in gae.failure.errors]
        for msg in error_msgs:
            if "AuthenticationError" in msg or "AuthorizationError" in msg:
                # Possível token inválido
                print("Erro: refresh_token inválido ou credenciais de API incorretas.")
            elif "budget" in msg and "too low" in msg.lower():
                print("Erro: orçamento muito baixo para criar a campanha.")
            else:
                print(f"Erro na criação da campanha: {msg}")
    except Exception as e:
        # Qualquer outro erro não previsto
        print(f"Erro inesperado: {e}")

@app.post("/create_campaign")
async def create_campaign_endpoint(payload: CampaignPayload, background_tasks: BackgroundTasks):
    # Regras de negócio antes de agendar a tarefa:
    # Verifica orçamento mínimo (exemplo: >= $1.00)
    numeric_budget = float(payload.budget.replace("$", ""))
    if numeric_budget < 1.0:
        # Retorna 422 com mensagem clara
        raise HTTPException(status_code=422, detail="Budget muito baixo. Valor mínimo é $1.00.")
    # (A validação de objetivo inválido já é coberta pelo Pydantic/enum no modelo ou pela lógica no background_task)
    # Agenda a task de criação de campanha em segundo plano
    background_tasks.add_task(create_campaign_task, payload)
    return {"message": "Processamento da criação da campanha iniciado em background."}
