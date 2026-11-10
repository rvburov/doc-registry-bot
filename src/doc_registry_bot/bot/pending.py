"""Ожидание ответа человека: одно на человека, хранится в базе и переживает перезапуск."""

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from doc_registry_bot.bot.context import LIFETIME, Ctx
from doc_registry_bot.db.models import Pending

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


async def put(ctx: Ctx, kind: str, payload: dict[str, Any]) -> None:
    await ctx.session.execute(delete(Pending).where(Pending.user_id == ctx.sender_id))
    ctx.session.add(
        Pending(user_id=ctx.sender_id, kind=kind, payload=payload, expires_at=ctx.now + LIFETIME)
    )
    await ctx.session.flush()


async def take(ctx: Ctx, found: Pending) -> None:
    """Забрать ожидание один раз, в том числе если выполнение потом не удастся."""
    await ctx.session.delete(found)
    await ctx.session.flush()


async def purge_expired(session: Any, now: datetime) -> None:
    await session.execute(delete(Pending).where(Pending.expires_at <= now))
