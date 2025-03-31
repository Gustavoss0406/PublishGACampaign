# Usa uma imagem base com Python 3.12 (versão slim para manter a imagem leve)
FROM python:3.12-slim

# Instala dependências do sistema necessárias para compilar pacotes (build-essential e libyaml-dev)
RUN apt-get update && apt-get install -y build-essential libyaml-dev

# Define o diretório de trabalho dentro do contêiner
WORKDIR /app

# Copia os arquivos do seu projeto para o contêiner
COPY . /app

# Cria o ambiente virtual na pasta /opt/venv
RUN python -m venv --copies /opt/venv

# (Opcional) Se você tiver definido essa variável, remova-a ou comente-a
# ENV SETUPTOOLS_USE_DISTUTILS=stdlib

# Ativa o ambiente virtual, atualiza pip, setuptools e wheel, instala Cython e as dependências
RUN . /opt/venv/bin/activate && \
    pip install --upgrade pip setuptools wheel && \
    pip install Cython && \
    pip install -r requirements.txt

# Comando para iniciar a aplicação (ajuste "main:app" conforme o seu código)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
