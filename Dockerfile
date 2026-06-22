# ==========================================
# STAGE 1: Dependency Builder
# ==========================================
FROM python:3.11-slim AS compiler-stage

WORKDIR /usr/src/app

# Prevent Python from writing pyc files and keep stdout unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies required for building psycopg2 and GeoAlchemy
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Compile dependencies into wheels for faster, cleaner installation
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /usr/src/app/wheels -r requirements.txt

# ==========================================
# STAGE 2: Production Runtime
# ==========================================
FROM python:3.11-slim AS runtime-stage

WORKDIR /app

# Create a non-root user for security compliance
RUN addgroup --system gisvizgroup && adduser --system --group gisvizuser

# Install ONLY the runtime libraries for PostgreSQL (libpq5) - drops the heavy gcc compilers
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled wheels from the builder stage
COPY --from=compiler-stage /usr/src/app/wheels /wheels
COPY --from=compiler-stage /usr/src/app/requirements.txt .

# Install the pre-compiled wheels
RUN pip install --no-cache /wheels/*

# Copy the application code and set ownership
COPY --chown=gisvizuser:gisvizgroup ./app /app/app

# Switch to the secure non-root user
USER gisvizuser

EXPOSE 8001

# Execute the application via Uvicorn ASGI server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload", "--workers", "4"]