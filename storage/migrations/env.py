import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

logger = logging.getLogger("alembic.env")

# Make the repo root importable so `shared.config.settings` can be loaded regardless of
# the working directory this is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = None

# No SQLAlchemy ORM/schema layer here: migrations are raw SQL (`op.execute(...)`) per
# spec 01's raw-SQL style. SQLAlchemy is only used as alembic's connection/engine machinery.
#
# DATABASE_URL always comes from the environment / .env via pydantic-settings `Settings`
# (shared/config/settings.py) — never hardcoded in alembic.ini. Fall back to a bare env
# read so `alembic` can run even without the other Settings fields (gmail_oauth_token_path)
# populated, e.g. in CI contexts that only run migrations. Only a `Settings` *validation*
# failure (a genuinely missing/invalid field) triggers the fallback — anything else (e.g. a
# real bug inside `shared.config.settings`) is left to raise and fail loudly.
try:
    from shared.config.settings import Settings

    database_url = Settings(gmail_oauth_token_path=os.environ.get("GMAIL_OAUTH_TOKEN_PATH", "")).database_url
except ValidationError as exc:
    logger.warning(
        "Settings validation failed (%s); falling back to DATABASE_URL env var directly.", exc
    )
    database_url = os.environ["DATABASE_URL"]

config.set_main_option("sqlalchemy.url", database_url)

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
