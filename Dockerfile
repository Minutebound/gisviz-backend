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

# Install using the pre-compiled wheels from the builder stage!
RUN pip install --no-cache /app/wheels/*

# NEW: Copy Alembic configuration and migration scripts into the container
COPY alembic.ini .
COPY alembic/ ./alembic/

# Pre-create the uploads directory
RUN mkdir -p /app/uploads

# Run as root in dev so local volume mounts don't trigger permission errors
# Use --reload for instant code updates upon saving
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]

# ==========================================
# STAGE 4: PRODUCTION ENVIRONMENT
# ==========================================
FROM base AS prod

# Create a non-root user for enterprise security compliance
RUN addgroup --system gisvizgroup && adduser --system --group gisvizuser

# Copy and install only the pre-compiled wheels (leaves heavy compilers behind)
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache /wheels/*

# Copy the application code and set ownership
COPY --chown=gisvizuser:gisvizgroup ./app /app/app

# NEW: Copy Alembic files and set ownership for secure production migrations
COPY --chown=gisvizuser:gisvizgroup alembic.ini .
COPY --chown=gisvizuser:gisvizgroup alembic/ ./alembic/

# Pre-create the uploads directory and assign ownership to the secure user
RUN mkdir -p /app/uploads && chown -R gisvizuser:gisvizgroup /app/uploads

# Switch to the secure non-root user
USER gisvizuser

EXPOSE 8001

# Execute via Uvicorn with multiple workers to handle high traffic on the VPS
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "4"]