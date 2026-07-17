Monolithic Architecture (GISVIZ BETA V0)

# GisViz — Backend

**Stack:** FastAPI · Python 3.11 · PostgreSQL 15 (×4) · Redis 7 · Docker · Alembic · IONOS VPS

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Repository layout](#2-repository-layout)
3. [Prerequisites](#3-prerequisites)
4. [Environment variables](#4-environment-variables)
5. [Local development](#5-local-development)
6. [Database migrations (Alembic)](#6-database-migrations-alembic)
7. [Production deployment](#7-production-deployment)
8. [CI/CD — GitHub Actions](#8-cicd--github-actions)
9. [Monitoring & access](#9-monitoring--access)
10. [Backups](#10-backups)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Architecture

```
Internet → Cloudflare → IONOS VPS :443 → Caddy → localhost:8001 → gisviz-api
                                                                        │
                                          ┌─────────────────────────────┤
                                          │                             │
                              gisviz-users-db :5433          gisviz-cache :6379
                              gisviz-posts-db :5434
                              gisviz-analytics-db :5435
                              gisviz-admin-db :5436
```

All containers share the `gisviz-network` Docker bridge. Database ports are blocked at both UFW and IONOS firewall level — they are never publicly reachable.

### Container list

| Container | Image | Port | Purpose |
|---|---|---|---|
| `gisviz-api` | python:3.11-slim (custom) | 8001 | FastAPI — 4 uvicorn workers |
| `gisviz-users-db` | postgres:15-alpine | 5433 | Users, auth, roles, follows |
| `gisviz-posts-db` | postgres:15-alpine | 5434 | Posts, likes, bookmarks, comments |
| `gisviz-analytics-db` | postgres:15-alpine | 5435 | Daily snapshots, trend data |
| `gisviz-admin-db` | postgres:15-alpine | 5436 | Audit log, admin config |
| `gisviz-cache` | redis:7-alpine | 6379 | Feed cache, legal content, trending |
| `gisviz-loki` | grafana/loki | 3100 | Log aggregation (SSH tunnel only) |
| `gisviz-promtail` | grafana/promtail | — | Reads Docker logs → Loki |
| `gisviz-redis-insight` | redis/redisinsight | 8002 | Redis GUI (SSH tunnel only) |
| `gisviz-pgadmin` | dpage/pgadmin4 | 5050 | Postgres GUI (SSH tunnel only) |

### Dockerfile stages

| Stage | Base | `APP_ENV` | Purpose |
|---|---|---|---|
| `base` | python:3.11-slim | — | System deps |
| `builder` | base | — | Compile Python wheels |
| `dev` | builder | `development` | Hot-reload, console email, dev OTP in responses |
| `prod` | base | `production` | Non-root user, 4 workers, real SMTP |

`APP_ENV` is baked in at build time — never set it in `.env.backend`.

---

## 2. Repository layout

```
gisviz-backend/
├── app/
│   ├── api/v1/endpoints/     # Route handlers (auth, posts, users, admin, …)
│   ├── core/config.py        # Pydantic settings — reads .env.backend
│   ├── db/
│   │   ├── database.py       # Four SQLAlchemy engines + session factories
│   │   └── models.py         # All ORM models — single source of truth
│   ├── schemas/              # Pydantic request / response schemas
│   └── services/             # Business logic (auth, posts, cache, …)
├── alembic/
│   ├── env.py                # Multi-database migration runner
│   ├── script.py.mako        # Migration template (uses Mako Undefined check)
│   └── versions/             # Generated migration files
├── alembic.ini               # Alembic config — 4 database sections
├── docker-compose.yml        # Production stack
├── docker-compose.dev.yml    # Development stack (hot-reload)
├── Dockerfile                # Multi-stage build
├── requirements.txt
├── .env.backend.example      # Template — copy to .env.backend, never commit
└── .gitignore
```

---

## 3. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker | 24+ | `docker compose` (v2) |
| Python | 3.11 | Only needed to run scripts outside Docker |
| Git | any | |

---

## 4. Environment variables

Copy the example and fill in values. **Never commit `.env.backend`.**

```bash
cp .env.backend.example .env.backend
nano .env.backend
```

### Full `.env.backend` reference

```bash
# ── JWT ──────────────────────────────────────────────────────────────────────
# Generate with: openssl rand -hex 32
JWT_SECRET_KEY=CHANGE_ME
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=60

# ── CORS ─────────────────────────────────────────────────────────────────────
# Comma-separated list of allowed frontend origins
BACKEND_CORS_ORIGINS=https://gisviz.com,https://www.gisviz.com

# ── Users DB ─────────────────────────────────────────────────────────────────
USERS_DB_USER=gisviz_users
USERS_DB_PASSWORD=CHANGE_ME
USERS_DB_NAME=gisviz_users_db
USERS_DATABASE_URL=postgresql://gisviz_users:CHANGE_ME@users-db:5432/gisviz_users_db

# ── Posts DB ─────────────────────────────────────────────────────────────────
POSTS_DB_USER=gisviz_posts
POSTS_DB_PASSWORD=CHANGE_ME
POSTS_DB_NAME=gisviz_posts_db
POSTS_DATABASE_URL=postgresql://gisviz_posts:CHANGE_ME@posts-db:5432/gisviz_posts_db

# ── Analytics DB ─────────────────────────────────────────────────────────────
ANALYTICS_DB_USER=gisviz_analytics
ANALYTICS_DB_PASSWORD=CHANGE_ME
ANALYTICS_DB_NAME=gisviz_analytics_db
ANALYTICS_DATABASE_URL=postgresql://gisviz_analytics:CHANGE_ME@analytics-db:5432/gisviz_analytics_db

# ── Admin DB ─────────────────────────────────────────────────────────────────
ADMIN_DB_USER=gisviz_admin
ADMIN_DB_PASSWORD=CHANGE_ME
ADMIN_DB_NAME=gisviz_admin_db
ADMIN_DATABASE_URL=postgresql://gisviz_admin:CHANGE_ME@admin-db:5432/gisviz_admin_db

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL=redis://cache:6379/0

# ── SMTP (IONOS) ─────────────────────────────────────────────────────────────
# Leave blank in dev — email prints to console instead
SMTP_HOST=smtp.ionos.com
SMTP_PORT=465
SMTP_USER=noreply@gisviz.com
SMTP_PASS=CHANGE_ME

# ── Frontend URL ──────────────────────────────────────────────────────────────
# Used to build password reset links in emails
FRONTEND_URL=https://gisviz.com

# ── pgAdmin (dev only) ───────────────────────────────────────────────────────
PGADMIN_EMAIL=admin@gisviz.com
PGADMIN_PASSWORD=CHANGE_ME
```

---

## 5. Local development

```bash
# 1. Clone and enter
git clone git@github.com:YOUR_ORG/gisviz-backend.git
cd gisviz-backend

# 2. Create dev env file
cp .env.backend.example .env.backend
# Edit .env.backend:
#   BACKEND_CORS_ORIGINS=http://localhost:3000,http://localhost:3001
#   FRONTEND_URL=http://localhost:3001
#   SMTP_* fields can be left blank — dev uses console output

# 3. Start the full dev stack
docker compose -f docker-compose.dev.yml up

# API available at:  http://localhost:8001
# Swagger UI at:     http://localhost:8001/docs
# pgAdmin at:        http://localhost:5050  (SSH tunnel in prod)
# RedisInsight at:   http://localhost:8002  (SSH tunnel in prod)
```

The `dev` Dockerfile stage mounts `./app` as a volume, so code changes reload instantly without rebuilding.

---

## 6. Database migrations (Alembic)

GisViz uses a custom multi-database Alembic setup. Each migration file contains four per-database functions (`upgrade_users`, `upgrade_posts`, `upgrade_analytics`, `upgrade_admin`). Only populate the ones relevant to your change — leave others as `pass`.

### Commands

```bash
# Show current state across all 4 databases
docker compose exec api alembic current

# Show history
docker compose exec api alembic history

# Generate a new migration (autogenerate from model changes)
docker compose exec api alembic revision --autogenerate -m "describe_change"

# Apply all pending migrations
docker compose exec api alembic upgrade head

# Roll back one migration
docker compose exec api alembic downgrade -1
```

### Baseline stamp (first deploy only)

If tables already exist and you're adding Alembic for the first time:

```bash
# Generate a baseline revision with all upgrade_* functions as pass
docker compose exec api alembic revision -m "initial_schema_baseline"
# Edit the file so all upgrade_* / downgrade_* functions are pass

# Stamp the DB without running anything
docker compose exec api alembic stamp 0001_initial

# Verify
docker compose exec api alembic current
```

### Adding `is_active` to posts example

```python
# In the generated migration file:
def upgrade_posts() -> None:
    op.add_column(
        "posts",
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_posts_is_active", "posts", ["is_active"])

def downgrade_posts() -> None:
    op.drop_index("ix_posts_is_active", table_name="posts")
    op.drop_column("posts", "is_active")
```

---

## 7. Production deployment

### First deployment

```bash
# On the VPS as deploy user
ssh deploy@YOUR_VPS_IP

cd /srv/gisviz/gisviz-backend

# Create the external Docker network (once only)
docker network create gisviz-network

# Start databases and cache first, wait for healthy
docker compose up -d users-db posts-db analytics-db admin-db cache
sleep 10 && docker compose ps

# Run migrations
docker compose run --rm api alembic upgrade head

# Start everything
docker compose up -d

# Verify
docker compose ps
curl http://localhost:8001/
# → {"status":"operational","engine":"gisviz-api","version":"..."}
```

### Manual update (without CI/CD)

```bash
cd /srv/gisviz/gisviz-backend
git pull origin main

# If models changed — run migrations first
docker compose run --rm api alembic upgrade head

# Rebuild and restart only the API container
docker compose build api
docker compose up -d api

# Check logs
docker compose logs api --tail 30
```

### Key commands

```bash
# View all container status
docker compose ps

# Tail API logs
docker compose logs -f api

# Restart a single container
docker compose restart api

# Connect to a database
docker compose exec users-db psql -U gisviz_users -d gisviz_users_db

# Flush Redis legal cache (after updating legal.py defaults)
docker compose exec cache redis-cli --scan --pattern 'legal:*' \
  | xargs docker compose exec -T cache redis-cli DEL
```

---

## 8. CI/CD — GitHub Actions

Deployments are triggered by commit message keywords. Push to `main` without a trigger keyword — no deployment runs.

| Commit message contains | Deploys |
|---|---|
| `[deploy]` | Backend + Frontend |
| `[deploy-backend]` | Backend only |
| `[deploy-frontend]` | Frontend only |
| anything else | Nothing |

### Setup (one-time)

**1. Generate a CI/CD SSH key on your local machine:**

```bash
ssh-keygen -t ed25519 -C "gisviz-github-actions" -f ~/.ssh/gisviz_actions -N ""
```

**2. Add the public key to the VPS:**

```bash
ssh gisviz
echo "PASTE_PUBLIC_KEY_HERE" >> ~/.ssh/authorized_keys
```

**3. Add GitHub Secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `VPS_HOST` | VPS IP address |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | Contents of `~/.ssh/gisviz_actions` (private key) |
| `VPS_SSH_PORT` | `22` |

**4. Create a `production` environment** (Settings → Environments).

The workflow file lives at `.github/workflows/deploy.yml`. Every successful deploy creates a timestamped release branch (`release/backend-YYYYMMDD-HHMMSS-SHORTSHA`) for audit trail.

### What the deploy script does on the VPS

1. `git pull origin main` — pulls latest code
2. `docker compose build api` — rebuilds the API image
3. `docker compose run --rm api alembic upgrade head` — applies any pending migrations
4. `docker compose up -d api` — starts the new container
5. Health check — `curl http://localhost:8001/` must return HTTP 200
6. Posts a deploy status comment on the commit

---

## 9. Monitoring & access

### SSH tunnels for private services

All admin GUIs are blocked at the firewall. Access them via SSH tunnel only:

```bash
# pgAdmin — http://localhost:5050
ssh -L 5050:localhost:5050 deploy@YOUR_VPS_IP -N &

# RedisInsight — http://localhost:8002
ssh -L 8002:localhost:8002 deploy@YOUR_VPS_IP -N &

# Loki (for local Grafana) — http://localhost:3100
ssh -L 3100:localhost:3100 deploy@YOUR_VPS_IP -N &
```

### Monitoring tools

| Tool | Access | What it shows |
|---|---|---|
| Portainer | `YOUR_VPS_IP:9000` | Container CPU/RAM/logs, live stats |
| pgAdmin | SSH tunnel → `localhost:5050` | Database tables, slow queries |
| RedisInsight | SSH tunnel → `localhost:8002` | Cache keys, memory usage |
| Grafana (local) | Homebrew → `localhost:3000` | Loki logs from all containers |
| UptimeRobot | uptimerobot.com | API uptime, 5-min ping |
| FastAPI Docs | `api.gisviz.com/docs` | All endpoints, interactive testing |

### Health checks

```bash
# API responding
curl http://localhost:8001/

# All containers healthy
docker compose ps

# Redis connected
docker compose exec cache redis-cli ping   # → PONG

# Postgres users DB ready
docker compose exec users-db pg_isready -U gisviz_users
```

---

## 10. Backups

### Manual backup (run before every migration)

```bash
BACKUP_DIR="/srv/gisviz/backups/$(date +%Y-%m-%d)"
mkdir -p "$BACKUP_DIR"

docker compose exec users-db pg_dump -U gisviz_users gisviz_users_db \
  > "$BACKUP_DIR/users.sql"

docker compose exec posts-db pg_dump -U gisviz_posts gisviz_posts_db \
  > "$BACKUP_DIR/posts.sql"

docker compose exec analytics-db pg_dump -U gisviz_analytics gisviz_analytics_db \
  > "$BACKUP_DIR/analytics.sql"

docker compose exec admin-db pg_dump -U gisviz_admin gisviz_admin_db \
  > "$BACKUP_DIR/admin.sql"
```

### Automated daily backup (cron)

```bash
crontab -e
# Add:
0 3 * * * /srv/gisviz/scripts/backup.sh >> /var/log/gisviz-backup.log 2>&1
```

### Restore from backup

```bash
docker compose exec -T users-db psql -U gisviz_users gisviz_users_db \
  < /srv/gisviz/backups/2026-07-16/users.sql
```

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| API returns 500 | DB connection failed | `docker compose ps` — all DBs healthy? |
| JWT errors on login | Wrong `JWT_SECRET_KEY` | Check `.env.backend`, restart api |
| Emails not sending | SMTP credentials wrong | Check `SMTP_*` in `.env.backend` |
| Trending feed empty | Redis `trending_posts` key missing | ETL scheduler not running; falls back to DB |
| Migration fails | DB not ready | Wait 10s after `docker compose up`, retry |
| CORS errors in browser | Origin not in allowlist | Check `BACKEND_CORS_ORIGINS` in `.env.backend` |
| `alembic revision` template fails | Mako `Undefined` object | Check `alembic/script.py.mako` uses `isinstance(upgrades, Undefined)` |
| Legal pages show old content | Redis cache stale | Flush with `redis-cli DEL legal:*` and restart api |

---

## Security notes

- `.env.backend` is gitignored — never commit it
- DB passwords are unique per database — no shared credentials
- DB ports 5433–5436 blocked in both UFW and IONOS firewall
- API container runs as non-root user `gisvizuser`
- JWT tokens expire in 60 minutes
- CORS restricted to `gisviz.com` origin only
- Passwords hashed with bcrypt — never stored in plain text
