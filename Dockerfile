FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Runtime (not build-time) environment variables - supplied by `docker run -e`
# or Cloud Run's --set-env-vars, deliberately NOT baked in, so one image can be
# promoted between environments:
#   GCS_BUCKET          bucket holding the parquet dataset (e.g. collab-nprod-data)
#   CHANGE_LOG_PROJECT  GCP project of the BigQuery change log (e.g. collab-infra-nprod)
#   IAP_AUDIENCE        expected audience of the IAP-signed identity token
# Left unset, GCS_BUCKET/CHANGE_LOG_PROJECT fall back to the values in
# config.yaml. See README.md's "Secrets / environment variables".

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8501

ENTRYPOINT ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
