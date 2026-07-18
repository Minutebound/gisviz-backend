# ==========================================
# STAGE 1: Base Runtime Environment
# ==========================================
FROM python:3.11-slim AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*


# ==========================================
# STAGE 2: Dependency Builder
# ==========================================
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt


# ==========================================
# STAGE 3: DEVELOPMENT
#
# APP_ENV=development is baked in here.
# This means:
#   - _email()           → simulate_send_email() (prints OTP to console)
#   - _expose_dev_field() → returns the otp/token value
#   - All API responses include dev_otp / dev_token
#   - The UI shows the DEV notice banner
#
# --reload watches the volume-mounted ./app from your host disk.
# Code changes are reflected instantly without rebuilding.
# ==========================================
FROM builder AS dev

ENV APP_ENV=development

RUN pip install --no-cache /app/wheels/*

COPY alembic.ini .
COPY alembic/ ./alembic/

RUN mkdir -p /app/uploads

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]


# ==========================================
# STAGE 4: PRODUCTION
#
# APP_ENV=production is baked in here.
# This means:
#   - _email()            → send_email() (real SMTP — SMTP_* must be in .env.backend)
#   - _expose_dev_field() → always returns None
#   - dev_otp / dev_token are NEVER included in any API response
#   - The UI dev notice banner never renders
#
# App code is copied INTO the image at build time (not volume-mounted).
# Non-root user for security. 4 workers for production throughput.
# ==========================================
FROM base AS prod

ENV APP_ENV=production

RUN addgroup --system gisvizgroup && adduser --system --group gisvizuser

COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache /wheels/*

COPY --chown=gisvizuser:gisvizgroup ./app /app/app
COPY --chown=gisvizuser:gisvizgroup alembic.ini .
COPY --chown=gisvizuser:gisvizgroup alembic/ ./alembic/

RUN mkdir -p /app/uploads && chown -R gisvizuser:gisvizgroup /app/uploads

USER gisvizuser

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "4","--proxy-headers","--forwarded-allow-ips='*'"]