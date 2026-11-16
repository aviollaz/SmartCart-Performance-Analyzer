# Imagen de producción del analyzer (Cloud Run).
#
# Liviana a propósito: sin torch ni modelos, así que el arranque en frío es de
# un par de segundos. Importa, porque SmartCart corta el envío del evento por
# timeout (ANALYTICS_TIMEOUT_SECONDS) y un arranque lento se traduce en eventos
# perdidos.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# Forma shell a propósito: Cloud Run inyecta $PORT y la forma exec no lo
# expandiría. 8001 es el default para correr la imagen en local.
CMD uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8001}
