"""Подключение к базе и обновление схемы при запуске."""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def engine_for(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


log = logging.getLogger(__name__)


def upgrade(url: str) -> None:
    """Довести схему базы до последней миграции. Синхронно: зовется до запуска службы.

    Схема новее этой версии (откат на прежнюю версию) — не ошибка: миграции только добавляют,
    и прежняя версия работает на новой схеме.
    """
    try:
        command.upgrade(alembic_config(url), "head")
    except CommandError as error:
        if "Can't locate revision" not in str(error):
            raise
        log.warning("Схема базы новее этой версии бота, обновление схемы пропущено: %s", error)
