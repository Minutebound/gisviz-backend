"""
alembic/env.py
==============
Change from previous version:
  Before: import app.db.models + import app.db.analytics_models
  After:  import app.db.models   (analytics + admin models now live there)
"""

import logging
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

from app.core.config import settings
from app.db.database import UsersBase, PostsBase, AnalyticsBase, AdminBase

# Single import registers all four bases — no separate analytics_models import needed
import app.db.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

config.set_section_option("users",     "sqlalchemy.url", settings.USERS_DATABASE_URL)
config.set_section_option("posts",     "sqlalchemy.url", settings.POSTS_DATABASE_URL)
config.set_section_option("analytics", "sqlalchemy.url", settings.ANALYTICS_DATABASE_URL)
config.set_section_option("admin",     "sqlalchemy.url", settings.ADMIN_DATABASE_URL)

target_metadata = {
    "users":     UsersBase.metadata,
    "posts":     PostsBase.metadata,
    "analytics": AnalyticsBase.metadata,
    "admin":     AdminBase.metadata,
}


def run_migrations_offline() -> None:
    for name in config.get_main_option("databases").split(","):
        name = name.strip()
        rec  = config.get_section(name)
        context.configure(
            url=rec["sqlalchemy.url"],
            target_metadata=target_metadata.get(name),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations(engine_name=name)


def run_migrations_online() -> None:
    db_names = [n.strip() for n in config.get_main_option("databases").split(",")]
    engines  = {
        name: engine_from_config(
            config.get_section(name, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        for name in db_names
    }
    for name, engine in engines.items():
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata.get(name),
            )
            with context.begin_transaction():
                logger.info("Migrating: %s", name)
                context.run_migrations(engine_name=name)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()