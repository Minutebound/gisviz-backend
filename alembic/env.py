"""
alembic/env.py
==============
One migration history for six databases: users, posts, analytics, admin, datasets, misc.
datasets and misc are optional in app.core.config; when their URL is not set they are skipped.
"""

import logging
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

from app.core.config import settings
from app.db.database import (
    UsersBase, PostsBase, AnalyticsBase, AdminBase, DatasetsBase, MiscBase,
)

import app.db.models  # noqa: F401  registers the models on the bases above

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

URLS = {
    "users":     settings.USERS_DATABASE_URL,
    "posts":     settings.POSTS_DATABASE_URL,
    "analytics": settings.ANALYTICS_DATABASE_URL,
    "admin":     settings.ADMIN_DATABASE_URL,
    "datasets":  settings.DATASETS_DATABASE_URL,
    "misc":      settings.MISC_DATABASE_URL,
}

target_metadata = {
    "users":     UsersBase.metadata,
    "posts":     PostsBase.metadata,
    "analytics": AnalyticsBase.metadata,
    "admin":     AdminBase.metadata,
    "datasets":  DatasetsBase.metadata,
    "misc":      MiscBase.metadata,
}

for _name, _url in URLS.items():
    if _url:
        # '%' is special in ini values; passwords with % must be doubled
        config.set_section_option(_name, "sqlalchemy.url", _url.replace("%", "%%"))


def _db_names() -> list[str]:
    names = [n.strip() for n in config.get_main_option("databases").split(",")]
    return [n for n in names if URLS.get(n)]


def run_migrations_offline() -> None:
    for name in _db_names():
        rec = config.get_section(name)
        context.configure(
            url=rec["sqlalchemy.url"],
            target_metadata=target_metadata.get(name),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations(engine_name=name)


def run_migrations_online() -> None:
    engines = {
        name: engine_from_config(
            config.get_section(name, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        for name in _db_names()
    }
    for name, engine in engines.items():
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata.get(name),
                compare_type=True,
            )
            with context.begin_transaction():
                logger.info("Migrating: %s", name)
                context.run_migrations(engine_name=name)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()