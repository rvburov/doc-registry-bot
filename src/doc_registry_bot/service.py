"""Служба бота: поток событий → очередь → обработка по одному → ответы.

Сообщения обрабатываются строго по одному: номера выдаются без гонок, а прием событий при
этом не останавливается (поток кладет их в очередь). После каждого подключения к потоку
дочитывается история: сообщения, пришедшие за время простоя, не теряются. Старше окна
(CATCHUP_HOURS) не выполняются: человек получает просьбу отправить их заново.
"""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot import __version__
from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.dispatcher import Dispatcher
from doc_registry_bot.bot.types import Incoming, Messenger, MessengerError, Reply
from doc_registry_bot.db.models import AuditEntry, BotState, Member, Pending, ProcessedMessage
from doc_registry_bot.messenger.client import file_ids_of
from doc_registry_bot.registry import markup

log = logging.getLogger(__name__)

LAST_CTIME = "last_ctime"
BACKUP_REPORTED = "backup_reported"
OUTAGE_NOTICE = timedelta(minutes=5)
AUDIT_KEEP = timedelta(days=365)
PROCESSED_KEEP = timedelta(days=60)


def now_utc() -> datetime:
    return datetime.now(UTC)


def server_time(ctime: int) -> datetime:
    """Время сервера мессенджера: миллисекунды или секунды от 1970 года (по величине числа)."""
    seconds = ctime / 1000 if ctime > 10**11 else ctime
    return datetime.fromtimestamp(seconds, UTC)


def incoming_of(kind: str, row: dict[str, Any]) -> Incoming | None:
    """Событие потока → входящее сообщение. Берутся только INCOMING_MESSAGE."""
    if kind != "INCOMING_MESSAGE":
        return None

    def number(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
        return None

    message_id = number(row.get("serverMessageId")) or number(row.get("id"))
    sender = number(row.get("ownerId"))
    if message_id is None or sender is None:
        return None
    files = file_ids_of(row.get("serverFileIds")) if row.get("hasFile") else []
    return Incoming(message_id, sender, str(row.get("text") or ""), files, number(row.get("ctime")))


class BotService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        config: BotConfig,
        messenger: Messenger,
        *,
        backup_status: Path | None = None,
        bot_id: int | None = None,
    ) -> None:
        self._sessions = sessions
        self._config = config
        self._messenger = messenger
        self._dispatcher = Dispatcher(sessions, config, messenger)
        self._queue: asyncio.Queue[Incoming] = asyncio.Queue()
        self._backup_status = backup_status
        self._bot_id = bot_id
        self._started_notice = False
        self._lost_at: datetime | None = None
        self.clock = now_utc

    # ── События потока ────────────────────────────────────────────────────────

    async def on_event(self, kind: str, row: dict[str, Any]) -> None:
        incoming = incoming_of(kind, row)
        if incoming is not None and incoming.sender_id != self._bot_id:
            await self._queue.put(incoming)

    async def on_state(self, connected: bool) -> None:
        now = self.clock()
        if not connected:
            self._lost_at = self._lost_at or now
            return
        if not self._started_notice:
            self._started_notice = True
            await self.notify_owners(f"Бот регистрации КД запущен, версия {__version__}.")
        elif self._lost_at is not None and now - self._lost_at >= OUTAGE_NOTICE:
            minutes = int((now - self._lost_at).total_seconds() // 60)
            await self.notify_owners(
                f"Связь со службой мессенджера восстановлена. Перерыв: {minutes} мин."
            )
        self._lost_at = None
        await self.catch_up()

    # ── Обработка ─────────────────────────────────────────────────────────────

    async def worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                incoming = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            await self.process(incoming)

    async def process(self, incoming: Incoming) -> None:
        replies = await self._dispatcher.handle(incoming, self.clock())
        await self.deliver(replies)
        if incoming.ctime is not None:
            await self._remember_ctime(incoming.ctime)

    async def deliver(self, replies: list[Reply]) -> None:
        for reply in replies:
            for attempt in range(3):
                try:
                    await self._messenger.send(reply.user_id, reply.html, reply.files)
                    break
                except MessengerError as error:
                    log.warning(
                        "Ответ %s не отправлен (%s), попытка %d", reply.user_id, error, attempt + 1
                    )
                    await asyncio.sleep(2 * (attempt + 1))

    async def notify_owners(self, note: str) -> None:
        await self.deliver([Reply(owner, markup.bold(note)) for owner in self._config.owner_ids])

    # ── Дочитывание пропущенного ──────────────────────────────────────────────

    async def _state(self, key: str) -> str | None:
        async with self._sessions() as session:
            found = await session.get(BotState, key)
            return found.value if found else None

    async def _set_state(self, key: str, value: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(BotState)
                .values(key=key, value=value)
                .on_conflict_do_update(index_elements=[BotState.key], set_={"value": value})
            )

    async def _remember_ctime(self, ctime: int) -> None:
        current = await self._state(LAST_CTIME)
        if current is None or int(current) < ctime:
            await self._set_state(LAST_CTIME, str(ctime))

    async def catch_up(self) -> None:
        """Дочитать историю после последнего обработанного сообщения.

        При первом запуске отметки нет: история до первого запуска бота не дочитывается.
        """
        last = await self._state(LAST_CTIME)
        if last is None:
            return
        after = int(last)
        border = self.clock() - timedelta(hours=self._config.catchup_hours)
        try:
            people = await self._people()
            fresh: list[Incoming] = []
            stale: dict[int, list[int]] = {}
            for person in people:
                cursor = after
                while True:
                    page = await self._messenger.history(person, cursor, 100)
                    for item in page:
                        if item.owner_id != person:
                            continue
                        if server_time(item.ctime) < border:
                            stale.setdefault(person, []).append(item.message_id)
                        else:
                            fresh.append(
                                Incoming(
                                    item.message_id, person, item.text, item.file_ids, item.ctime
                                )
                            )
                    if len(page) < 100:
                        break
                    cursor = page[-1].ctime
        except MessengerError as error:
            log.warning("Дочитывание истории не удалось: %s", error)
            return
        for person, ids in stale.items():
            await self.deliver(await self._dispatcher.handle_stale(person, ids, self.clock()))
        # Через ту же очередь: сообщения обрабатываются строго по одному.
        for incoming in sorted(fresh, key=lambda m: m.ctime or 0):
            await self._queue.put(incoming)
        if fresh or stale:
            log.info(
                "Дочитано: %d сообщений, устарело: %d",
                len(fresh),
                sum(len(ids) for ids in stale.values()),
            )

    async def _people(self) -> list[int]:
        contacts = {card.user_id for card in await self._messenger.contacts()}
        async with self._sessions() as session:
            members = set(await session.scalars(select(Member.user_id)))
        people = contacts | members | set(self._config.owner_ids)
        people.discard(self._bot_id or -1)
        return sorted(people)

    # ── Обслуживание ──────────────────────────────────────────────────────────

    async def maintenance(self, stop: asyncio.Event, period: float = 3600.0) -> None:
        while not stop.is_set():
            try:
                await self.clean()
                await self.check_backup()
            except Exception:
                log.exception("Обслуживание не удалось")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=period)

    async def clean(self) -> None:
        """Журнал действий — 12 месяцев, отметки обработанных сообщений — 60 дней."""
        now = self.clock()
        async with self._sessions() as session, session.begin():
            await session.execute(delete(AuditEntry).where(AuditEntry.at < now - AUDIT_KEEP))
            await session.execute(
                delete(ProcessedMessage).where(ProcessedMessage.processed_at < now - PROCESSED_KEEP)
            )
            await session.execute(delete(Pending).where(Pending.expires_at <= now))

    async def check_backup(self) -> None:
        """Отчет ночной копии: сбой или копия не попала в сетевую папку — сообщение владельцу."""
        if self._backup_status is None or not self._backup_status.exists():
            return
        try:
            status = json.loads(self._backup_status.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        stamp = str(status.get("time", ""))
        if not stamp or stamp == await self._state(BACKUP_REPORTED):
            return
        await self._set_state(BACKUP_REPORTED, stamp)
        problem = str(status.get("message") or "").strip()
        if not status.get("ok"):
            await self.notify_owners(f"Резервная копия базы не сделана: {problem}")
        elif not status.get("share_ok"):
            await self.notify_owners(
                f"Резервная копия сделана, но не попала в сетевую папку: {problem}"
            )
