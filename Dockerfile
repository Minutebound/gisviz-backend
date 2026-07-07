# ==========================================
# STAGE 1: Base Runtime Environment
# ==========================================
FROM python:3.11-slim AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install runtime libraries needed for PostgreSQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*


# ==========================================
# STAGE 2: Dependency Builder
# ==========================================
FROM base AS builder

# Install heavy compilers needed to build psycopg2, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt


# ==========================================
# STAGE 3: DEVELOPMENT ENVIRONMENT
# ==========================================
FROM builder AS dev

RUN pip install --no-cache /app/wheels/*

COPY alembic.ini .
COPY alembic/ ./alembic/

RUN mkdir -p /app/uploads

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]


# ==========================================
# STAGE 4: PRODUCTION ENVIRONMENT
# ==========================================
FROM base AS prod

RUN addgroup --system gisvizgroup && adduser --system --group gisvizuser

COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache /wheels/*

# Application code
COPY --chown=gisvizuser:gisvizgroup ./app /app/app

# Alembic migrations
COPY --chown=gisvizuser:gisvizgroup alembic.ini .
COPY --chown=gisvizuser:gisvizgroup alembic/ ./alembic/

RUN mkdir -p /app/uploads && chown -R gisvizuser:gisvizgroup /app/uploads

USER gisvizuser

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "4"]