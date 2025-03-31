# Usa uma imagem base com Python 3.12 (versão slim para manter a imagem leve)
FROM python:3.12-slim

# Instala dependências do sistema (necessárias para compilar pacotes Python)
RUN apt-get update && apt-get install -y build-essential

# Define o diretório de trabalho no contêiner
WORKDIR /app

# Copia todos os arquivos do seu projeto para o diretório /app dentro do contêiner
COPY . /app

# Cria o ambiente virtual na pasta /opt/venv
RUN python -m venv --copies /opt/venv

# Ativa o ambiente virtual, atualiza pip, setuptools e wheel, instala o Cython e depois as dependências do requirements.txt
RUN . /opt/venv/bin/activate && \
    pip install --upgrade pip setuptools wheel && \
    pip install Cython && \
    pip install -r requirements.txt

# Comando para iniciar a aplicação (ajuste se necessário)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
