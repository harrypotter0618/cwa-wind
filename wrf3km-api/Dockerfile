FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      libeccodes0 \
      libeccodes-data \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8000
ENV CACHE_DIR=/tmp/wrf3km-cache
ENV CACHE_TTL_SECONDS=1200
ENV MAX_CACHE_MB=450

EXPOSE 8000

CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
