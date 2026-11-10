"""Журнал КД в базе: серии, выдача номеров, поиск записи по номеру."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from doc_registry_bot.db.models import JournalEntry
from doc_registry_bot.registry import designation, text


def designation_key(value: str) -> str:
    """Обозначение к одному виду: без крайних пробелов, заглавные, похожая латиница русскими."""
    return text.org(value)


def series_of(value: str) -> str:
    """Серия обозначения «АБВГ.301121.»; пусто — строка не похожа на обозначение."""
    parts = designation_key(value).split(".")
    return f"{parts[0]}.{parts[1]}." if len(parts) >= 3 else ""


@dataclass(frozen=True)
class NewEntry:
    """Строка для записи в журнал."""

    designation: str
    name: str
    fio: str
    project: str
    accounting: str = ""
    first_use: str = ""
    summary: str = ""
    registrant_id: int | None = None


async def lock_series(session: AsyncSession, series: Sequence[str]) -> None:
    """Блокировка серий до конца транзакции. Порядок одинаковый у всех, чтобы не было взаимной
    блокировки, когда двум заявкам нужны одни и те же серии."""
    for key in sorted(set(series)):
        await session.execute(sql("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


async def series_designations(session: AsyncSession, org: str, eskd: str) -> list[str]:
    """Выданные обозначения серии (приведенные к одному виду), по возрастанию.

    Отбор по столбцу серии дает лишнее только в редких случаях; точная принадлежность серии
    проверяется разбором обозначения.
    """
    prefix = designation_key(designation.prefix(org, eskd))
    rows = await session.scalars(
        select(JournalEntry.designation_key)
        .where(JournalEntry.series == prefix)
        .order_by(JournalEntry.designation_key)
    )
    return [
        value for value in rows if designation.number_in(value, text.org(org), eskd) is not None
    ]


async def add_entries(session: AsyncSession, entries: Sequence[NewEntry], now: datetime) -> None:
    for entry in entries:
        session.add(
            JournalEntry(
                designation=entry.designation,
                designation_key=designation_key(entry.designation),
                series=series_of(entry.designation),
                name=entry.name,
                fio=entry.fio,
                registered_at=now,
                project=entry.project,
                accounting=entry.accounting,
                first_use=entry.first_use,
                summary=entry.summary,
                registrant_id=entry.registrant_id,
                updated_at=now,
            )
        )
    await session.flush()


async def find(session: AsyncSession, exact: str) -> JournalEntry | None:
    """Запись по точному номеру документа или исполнения."""
    return await session.scalar(
        select(JournalEntry).where(JournalEntry.designation_key == designation_key(exact))
    )


async def count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(JournalEntry)) or 0


async def all_entries(session: AsyncSession) -> list[JournalEntry]:
    rows = await session.scalars(select(JournalEntry).order_by(JournalEntry.id))
    return list(rows)
