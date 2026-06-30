# Cloud Run container. Firebase Hosting rewrites /api/** (or a custom domain) here.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Cloud Run injects $PORT (default 8080). One worker keeps the in-memory cache
# warm; scale via concurrency. Bump --timeout-keep-alive / Cloud Run request
# timeout if a cold date range takes long to scan.
ENV PORT=8080
CMD exec uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 120
