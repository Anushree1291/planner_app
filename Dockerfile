# Optional: run the app as a container (local testing, or Azure Container Apps later).
# The default Azure path in docs/DEPLOY_AZURE.md deploys the code directly to App Service.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --create-home appuser && chown -R appuser /code
USER appuser

EXPOSE 8000
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-w", "1", "--bind", "0.0.0.0:8000", "--timeout", "120", "--forwarded-allow-ips", "*"]
