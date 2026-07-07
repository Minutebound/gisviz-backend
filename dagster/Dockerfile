FROM python:3.11-slim

WORKDIR /opt/dagster/app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DAGSTER_HOME=/opt/dagster/dagster_home

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev libpq-dev libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip + setuptools so build_meta is available
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Install Dagster package (non-editable)
COPY dagster/pyproject.toml ./pyproject.toml
COPY dagster/gisviz_dagster/ ./gisviz_dagster/
RUN pip install --no-cache-dir .

# Copy gisviz app so Dagster can import app.analytics.snapshot
COPY app/ ./app/

# Seed DAGSTER_HOME with instance config + workspace
RUN mkdir -p /opt/dagster/dagster_home
COPY dagster/dagster.yaml   /opt/dagster/dagster_home/dagster.yaml
COPY dagster/workspace.yaml /opt/dagster/dagster_home/workspace.yaml

EXPOSE 3000

# webserver — daemon overrides this CMD in docker-compose.yml
CMD ["dagster-webserver", "-h", "0.0.0.0", "-p", "3000"]