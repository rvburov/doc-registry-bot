"""Окружение Alembic: миграции идут синхронно, тем же драйвером psycopg, что и бот."""

from alembic import context
from sqlalchemy import create_engine, pool

from doc_registry_bot.db.models import Base

config = context.config
target_metadata = Base.metadata


def run() -> None:
    url = config.get_main_option("sqlalchemy.url")
    assert url, "не задан адрес базы"
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run()
