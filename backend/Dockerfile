# v1 backend image. Built authoritatively in CodeBuild (ADR-004); this Dockerfile
# is the same artifact used locally via docker-compose (§5).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Stateless task behind the ALB (§4.3). Migrations run as a pre-traffic
# lifecycle hook in prod, not at container start.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
