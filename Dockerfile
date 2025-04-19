# Usa imagem base Python 3.12 slim
FROM python:3.12-slim

# Instala dependências do sistema e o ffmpeg
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      libyaml-dev \
      ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Cria e ativa um virtualenv em /opt/venv
ENV VENV_PATH=/opt/venv
RUN python -m venv --copies $VENV_PATH
ENV PATH="$VENV_PATH/bin:$PATH"

# Define diretório de trabalho
WORKDIR /app

# Copia só o requirements.txt primeiro para aproveitar cache do Docker
COPY requirements.txt /app/

# Atualiza pip e instala dependências Python (inclui Cython se necessário)
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir Cython \
 && pip install --no-cache-dir -r requirements.txt

# Copia o restante do código da aplicação
COPY . /app

# Expõe a porta que o Uvicorn vai usar
EXPOSE 8000

# Comando padrão para iniciar a aplicação
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
