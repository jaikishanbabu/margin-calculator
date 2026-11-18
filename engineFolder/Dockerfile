# Engine image for Railway (Render builds natively from render.yaml and doesn't use this file).
# Build context = repository root (railway.json -> dockerfilePath).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY engineFolder/requirements.txt engineFolder/requirements.txt
RUN pip install -r engineFolder/requirements.txt
COPY engineFolder engineFolder
COPY adminFolder adminFolder
COPY marginCalc marginCalc
RUN useradd --create-home --uid 10001 engine && chown -R engine /app
USER engine
WORKDIR /app/engineFolder
ENV HOST_NAME=railway
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
