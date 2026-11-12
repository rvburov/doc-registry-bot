"""Ожидание ответа человека: одно на человека, хранится в базе и переживает перезапуск."""

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from doc_registry_bot.bot.context import LIFETIME, Ctx
from doc_registry_bot.db.models import BotState, Pending

EXPIRED = "expired"


async def current(ctx: Ctx) -> Pending | str | None:
    """Живое ожидание отправителя; `EXPIRED` — было, но срок вышел (уже удалено); None — нет."""
    found = await ctx.session.scalar(
        select(Pending).where(Pending.user_id == ctx.sender_id).with_for_update()
    )
    if found is None:
        return None
    if found.expires_at <= ctx.now:
        await ctx.session.delete(found)
        await ctx.session.flush()
        return EXPIRED
    return found


def asked_key(user_id: int) -> str:
    return f"asked:{user_id}"


async def put(ctx: Ctx, kind: str, payload: dict[str, Any]) -> None:
    await ctx.session.execute(delete(Pending).where(Pending.user_id == ctx.sender_id))
    ctx.session.add(
        Pending(user_id=ctx.sender_id, kind=kind, payload=payload, expires_at=ctx.now + LIFETIME)
    )
    # Когда человеку задали последний вопрос: просроченное ожидание удаляется, а эта отметка
    # остается, и «Да» к давно истекшему вопросу не засчитается ответом на напоминание.
    asked = ctx.now.isoformat()
    await ctx.session.execute(
        pg_insert(BotState)
        .values(key=asked_key(ctx.sender_id), value=asked)
        .on_conflict_do_update(index_elements=[BotState.key], set_={"value": asked})
    )
    await ctx.session.flush()


async def asked_at(session: Any, user_id: int) -> datetime | None:
    """Когда человеку задали последний вопрос с ожиданием ответа; None — не задавали."""
    found = await session.get(BotState, asked_key(user_id))
    return datetime.fromisoformat(found.value) if found is not None else None


async def take(ctx: Ctx, found: Pending) -> None:
    """Забрать ожидание один раз, в том числе если выполнение потом не удастся."""
    await ctx.session.delete(found)
    await ctx.session.flush()


async def purge_expired(session: Any, now: datetime) -> None:
    await session.execute(delete(Pending).where(Pending.expires_at <= now))
