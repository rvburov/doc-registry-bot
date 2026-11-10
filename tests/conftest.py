"""База для тестов: настоящий PostgreSQL. Нет сервера — тесты базы пропускаются (skipped).

Адрес — переменная TEST_DATABASE_URL; по умолчанию стенд из tests/compose.yaml на порту 5457.
"""

import os
from collections.abc import AsyncIterator, Iterator
from zoneinfo import ZoneInfo

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.db.session import engine_for, sessions, upgrade

DEFAULT_URL = "postgresql+psycopg://postgres:postgres@localhost:5457/drb_test"
TABLES = ("journal", "members", "pending", "processed_messages", "audit", "bot_state")


def database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)


def _reachable(url: str) -> bool:
    try:
        with psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://"), connect_timeout=3
        ):
            return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="session")
def migrated() -> Iterator[str]:
    url = database_url()
    if not _reachable(url):
        pytest.skip(f"PostgreSQL для тестов недоступен: {url}")
    with psycopg.connect(
        url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    ) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
    upgrade(url)
    yield url


@pytest_asyncio.fixture
async def db(migrated: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = engine_for(migrated)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY"))
    yield sessions(engine)
    await engine.dispose()


@pytest.fixture
def config() -> BotConfig:
    return BotConfig(owner_ids=(1,), org_codes=("АБВГ", "ГМБВ"), zone=ZoneInfo("Europe/Moscow"))
