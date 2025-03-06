import logging
import requests
from flask import Flask, request, jsonify

# Configuração de logs detalhados
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# Credenciais e constantes para o Google Ads
DEVELOPER_TOKEN = "D4yv61IQ8R0JaE5dxrd1Uw"
CLIENT_ID = "167266694231-g7hvta57r99etbp3sos3jfi7q7h4ef44.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI = "https://app.adstock.ai/dashboard"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CUSTOMERS_LIST_URL = "https://googleads.googleapis.com/v9/customers:listAccessibleCustomers"

app = Flask(__name__)

def get_access_token(refresh_token: str) -> str:
    """
    Obtém o token de acesso utilizando o refresh token.
    """
    logging.debug("Iniciando get_access_token com refresh_token: %s", refresh_token)
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    logging.debug("Enviando requisição POST para %s com payload: %s", OAUTH_TOKEN_URL, payload)
    response = requests.post(OAUTH_TOKEN_URL, data=payload)
    logging.debug("Resposta do token: %s", response.text)
    if response.status_code != 200:
        logging.error("Erro ao obter token de acesso: %s", response.text)
        raise Exception("Erro ao obter token de acesso")
    access_token = response.json().get("access_token")
    if not access_token:
        logging.error("Token de acesso não encontrado na resposta: %s", response.text)
        raise Exception("Token de acesso não encontrado")
    logging.debug("Token de acesso obtido: %s", access_token)
    return access_token

def get_customer_id(access_token: str) -> str:
    """
    Obtém a lista de contas (customer IDs) acessíveis e retorna o primeiro encontrado.
    """
    logging.debug("Iniciando get_customer_id com access_token: %s", access_token)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": DEVELOPER_TOKEN,
        "Content-Type": "application/json"
    }
    logging.debug("Enviando requisição GET para %s com headers: %s", CUSTOMERS_LIST_URL, headers)
    response = requests.get(CUSTOMERS_LIST_URL, headers=headers)
    logging.debug("Resposta da listagem de customers: %s", response.text)
    if response.status_code != 200:
        logging.error("Erro ao obter lista de customers: %s", response.text)
        raise Exception("Erro ao obter lista de customers")
    resource_names = response.json().get("resourceNames", [])
    if not resource_names:
        logging.error("Nenhuma conta de cliente encontrada.")
        raise Exception("Nenhuma conta de cliente encontrada")
    # O formato é "customers/{customer_id}" - extraímos o customer_id
    first_customer = resource_names[0]
    customer_id = first_customer.split("/")[-1]
    logging.debug("Customer ID selecionado: %s", customer_id)
    return customer_id

def create_google_ads_campaign(customer_id: str, campaign_data: dict, access_token: str) -> dict:
    """
    Cria uma campanha no Google Ads com os dados fornecidos.
    Essa função simula a criação de campanha via Google Ads API.
    """
    logging.debug("Iniciando create_google_ads_campaign para customer_id: %s com campaign_data: %s", customer_id, campaign_data)
    campaign_endpoint = f"https://googleads.googleapis.com/v9/customers/{customer_id}/campaigns:mutate"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": DEVELOPER_TOKEN,
        "Content-Type": "application/json"
    }
    # Montagem do payload de criação da campanha (exemplo simplificado)
    campaign_operation = {
        "operations": [
            {
                "create": {
                    "name": f"Campanha - {campaign_data.get('keyword2', 'SemNome')}",
                    "status": "ENABLED",  # A campanha será criada ativa
                    "campaignBudget": f"customers/{customer_id}/campaignBudgets/INSERT_BUDGET_ID",  # ID de orçamento deve ser criado previamente
                    "advertisingChannelType": campaign_data.get("campaign_type", "SEARCH"),
                    "startDate": campaign_data.get("start_date"),
                    "endDate": campaign_data.get("end_date"),
                    "manualCpc": {},  # Exemplo de estratégia de lances
                    "networkSettings": {
                        "targetGoogleSearch": True,
                        "targetSearchNetwork": True,
                        "targetContentNetwork": True,
                        "targetPartnerSearchNetwork": False
                    }
                    # Outras configurações podem ser adicionadas conforme necessário.
                }
            }
        ]
    }
    logging.debug("Enviando requisição POST para criação de campanha em %s com payload: %s", campaign_endpoint, campaign_operation)
    response = requests.post(campaign_endpoint, headers=headers, json=campaign_operation)
    logging.debug("Resposta da criação da campanha: %s", response.text)
    if response.status_code not in (200, 201):
        logging.error("Erro ao criar campanha: %s", response.text)
        raise Exception("Erro ao criar campanha")
    return response.json()

@app.route('/create_campaign', methods=['POST'])
def create_campaign():
    logging.debug("Requisição recebida em /create_campaign")
    try:
        data = request.get_json()
        logging.debug("Dados recebidos no body: %s", data)
        
        # Validação dos campos obrigatórios
        campos_obrigatorios = [
            "keyword2", "keyword3", "budget", "start_date", "end_date",
            "price_model", "campaign_type", "audience_gender",
            "audience_min_age", "audience_max_age", "devices", "refresh_token"
        ]
        campos_faltantes = [campo for campo in campos_obrigatorios if campo not in data]
        if campos_faltantes:
            logging.error("Campos obrigatórios ausentes: %s", campos_faltantes)
            return jsonify({"error": f"Campos obrigatórios ausentes: {campos_faltantes}"}), 400
        
        # Extraindo o refresh_token do body
        refresh_token = data.get("refresh_token")
        logging.debug("Refresh token extraído: %s", refresh_token)
        
        # Obtendo o token de acesso usando o refresh token
        access_token = get_access_token(refresh_token)
        
        # Obtendo o customer ID a partir do token de acesso
        customer_id = get_customer_id(access_token)
        
        # Preparando os dados da campanha
        campaign_data = {
            "keyword2": data.get("keyword2"),
            "keyword3": data.get("keyword3"),
            "budget": data.get("budget"),
            "start_date": data.get("start_date"),
            "end_date": data.get("end_date"),
            "price_model": data.get("price_model"),
            "campaign_type": data.get("campaign_type"),
            "audience_gender": data.get("audience_gender"),
            "audience_min_age": data.get("audience_min_age"),
            "audience_max_age": data.get("audience_max_age"),
            "devices": data.get("devices")
        }
        logging.debug("Dados da campanha preparados: %s", campaign_data)
        
        # Criando a campanha no Google Ads
        campaign_response = create_google_ads_campaign(customer_id, campaign_data, access_token)
        logging.debug("Campanha criada com sucesso: %s", campaign_response)
        
        return jsonify({
            "message": "Campanha criada com sucesso",
            "campaign_response": campaign_response
        }), 201
        
    except Exception as e:
        logging.exception("Erro durante o processo de criação da campanha")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    logging.debug("Iniciando a API Flask")
    app.run(host='0.0.0.0', port=5000, debug=True)
