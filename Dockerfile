# Usa uma imagem base com Python 3.12 (versão slim para manter a imagem leve)
FROM python:3.12-slim

# Instala dependências do sistema necessárias para compilar pacotes (como build-essential)
RUN apt-get update && apt-get install -y build-essential

# Define o diretório de trabalho dentro do contêiner
WORKDIR /app

# Copia todos os arquivos do projeto para o contêiner
COPY . /app

# Cria o ambiente virtual na pasta /opt/venv
RUN python -m venv --copies /opt/venv

# Ativa o ambiente virtual, força a versão do setuptools e instala as dependências
RUN . /opt/venv/bin/activate && \
    pip install --upgrade pip && \
    pip install setuptools==66.1.1 wheel && \
    pip install Cython && \
    pip install -r requirements.txt

# Comando para iniciar a aplicação (ajuste "main:app" conforme o nome do seu arquivo e objeto FastAPI)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
