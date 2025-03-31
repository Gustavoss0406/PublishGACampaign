# Usa uma imagem base com Python 3.12 (versão slim para manter a imagem leve)
FROM python:3.12-slim

# Instala dependências do sistema necessárias para compilar pacotes
RUN apt-get update && apt-get install -y build-essential libyaml-dev

# Define o diretório de trabalho dentro do contêiner
WORKDIR /app

# Copia os arquivos do projeto para o contêiner
COPY . /app

# Cria o ambiente virtual em /opt/venv
RUN python -m venv --copies /opt/venv

# Atualiza as ferramentas do Python, instala Cython e as dependências do requirements.txt
RUN . /opt/venv/bin/activate && \
    pip install --upgrade pip setuptools wheel && \
    pip install Cython && \
    pip install -r requirements.txt

# Adiciona o diretório do ambiente virtual ao PATH
ENV PATH="/opt/venv/bin:${PATH}"

# Comando para iniciar a aplicação
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
