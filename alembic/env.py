import logging
from logging.config import fileConfig
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context

# 1. Import your settings and database definitions
from app.core.config import settings
from app.db.database import UsersBase, PostsBase
import app.db.models  # Ensures all models are registered with their Base

# Load Alembic Config
config = context.config

# Setup standard Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Initialize logger (Fixes the context.logger attribute error)
logger = logging.getLogger("alembic.env")

# 2. Inject Database URLs dynamically from your environment/config
config.set_section_option("users", "sqlalchemy.url", settings.USERS_DATABASE_URL)
config.set_section_option("posts", "sqlalchemy.url", settings.POSTS_DATABASE_URL)

# 3. Map databases to their respective Base metadata
target_metadata = {
    "users": UsersBase.metadata,
    "posts": PostsBase.metadata,
}

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    engines = {}
    for name in config.get_main_option("databases").split(","):
        engines[name.strip()] = config.get_section(name.strip())

    for name, rec in engines.items():
        context.configure(
            url=rec["sqlalchemy.url"],
            target_metadata=target_metadata.get(name),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations(engine_name=name)


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    # Create engines for both databases
    db_names = [name.strip() for name in config.get_main_option("databases").split(",")]
    engines = {
        name: engine_from_config(
            config.get_section(name, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        for name in db_names
    }

    # Run migrations for each engine
    for name, engine in engines.items():
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata.get(name),
            )
            with context.begin_transaction():
                logger.info(f"Migrating database: {name}")
                context.run_migrations(engine_name=name)

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()