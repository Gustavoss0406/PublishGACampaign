# Usa uma imagem base com Python 3.12 (versão slim para manter a imagem leve)
FROM python:3.12-slim

# Instala dependências do sistema necessárias para compilar pacotes
RUN apt-get update && apt-get install -y build-essential

# Define o diretório de trabalho dentro do contêiner
WORKDIR /app

# Copia os arquivos do seu projeto para o contêiner
COPY . /app

# Cria o ambiente virtual na pasta /opt/venv
RUN python -m venv --copies /opt/venv

# (Opcional) Define a variável de ambiente para usar o distutils da biblioteca padrão
ENV SETUPTOOLS_USE_DISTUTILS=stdlib

# Ativa o ambiente virtual, atualiza pip, setuptools e wheel, instala Cython e as dependências
RUN . /opt/venv/bin/activate && \
    pip install --upgrade pip setuptools wheel && \
    pip install Cython && \
    pip install -r requirements.txt

# Comando para iniciar a aplicação (ajuste "main:app" conforme o seu código)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
