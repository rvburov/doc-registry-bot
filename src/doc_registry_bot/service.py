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

from doc_registry_bot import __version__, backups
from doc_registry_bot.bot import copies, pending
from doc_registry_bot.bot.context import LIFETIME, BotConfig, Task, server_time
from doc_registry_bot.bot.dispatcher import Dispatcher
from doc_registry_bot.bot.types import Incoming, Messenger, MessengerError, Reply
from doc_registry_bot.db.models import (
    AuditEntry,
    BackupJob,
    BotState,
    JournalUpload,
    Member,
    Pending,
    ProcessedMessage,
    Reminder,
)
from doc_registry_bot.messenger.client import file_ids_of
from doc_registry_bot.registry import markup

log = logging.getLogger(__name__)

LAST_CTIME = "last_ctime"
BACKUP_REPORTED = "backup_reported"
# Когда база восстановлена из копии (секунды от 1970 года, пишет update.sh --restore): сообщения,
# отправленные раньше, принадлежат прежней базе и при дочитывании не выполняются заново.
RESTORED_AT = "restored_at"
# С какого момента считаются напоминания: сроки до первого запуска с напоминанием не пропущены.
REMINDER_SINCE = "reminder_since"
# Расписание, с которым считается reminder_since: сменили день или время — отсчет заново.
REMINDER_SCHEDULE = "reminder_schedule"
REMINDER_RETRY = timedelta(minutes=10)
# Сколько ждать после «Да» на напоминание сверх срока подтверждения папки, прежде чем решить,
# что копия по напоминанию не сделана.
REMINDER_GRACE = timedelta(minutes=1)
CLEAN_EVERY = timedelta(hours=1)
UPLOADS_KEEP = timedelta(days=1)
OUTAGE_NOTICE = timedelta(minutes=5)
AUDIT_KEEP = timedelta(days=365)
PROCESSED_KEEP = timedelta(days=60)


def now_utc() -> datetime:
    return datetime.now(UTC)


async def copy_made_since(session: AsyncSession, moment: datetime) -> bool:
    """Есть ли успешная резервная копия (из чата или терминала), начатая не раньше `moment`."""
    found = await session.scalar(
        select(BackupJob.id)
        .where(
            BackupJob.kind == "backup",
            BackupJob.status == "done",
            BackupJob.dump_ok.is_(True),
            BackupJob.csv_ok.is_(True),
            BackupJob.created_at >= moment,
        )
        .limit(1)
    )
    return found is not None


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
        self._dispatcher = Dispatcher(sessions, config, messenger, clock=self._now)
        self._queue: asyncio.Queue[Incoming] = asyncio.Queue()
        # Долгие дела (резервная копия, замена журнала) идут по одному, в фоне: очередь сообщений
        # при этом не стоит.
        self._task_lock = asyncio.Lock()
        self._running: set[asyncio.Task[None]] = set()
        self._backup_status = backup_status
        self._bot_id = bot_id
        self._started_notice = False
        self._lost_at: datetime | None = None
        self.clock = now_utc

    def _now(self) -> datetime:
        return self.clock()

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
        handled = await self._dispatcher.handle(incoming, self.clock())
        await self.deliver(handled.replies)
        for task in handled.tasks:
            self.start(task)
        if incoming.ctime is not None:
            await self._remember_ctime(incoming.ctime)

    def start(self, task: Task) -> None:
        """Запустить дело в фоне; ссылка хранится, пока дело не кончится."""
        running = asyncio.create_task(self._run(task))
        self._running.add(running)
        running.add_done_callback(self._running.discard)

    async def _run(self, task: Task) -> None:
        async with self._task_lock:
            try:
                replies = await task()
            except Exception:
                log.exception("Фоновое дело не выполнено")
                replies = []
        await self.deliver(replies)
        await self.report()

    async def report(self) -> None:
        """Отправить итоги копий и замен журнала, которые еще не дошли до владельца."""
        try:
            items = await copies.claim_reports(
                self._sessions, self._config.zone, self.clock(), self._config.owner_ids
            )
        except Exception:
            log.exception("Итоги не прочитаны")
            return
        for item in items:
            if not await self._send(item.user_id, item.html):
                await copies.unclaim(self._sessions, item)

    async def _send(self, user_id: int, html: str) -> bool:
        for attempt in range(3):
            try:
                await self._messenger.send(user_id, html)
                return True
            except MessengerError as error:
                log.warning("Итог %s не отправлен (%s), попытка %d", user_id, error, attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))
        return False

    async def idle(self) -> None:
        """Дождаться фоновых дел (для тестов и остановки)."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

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
        restored = await self._state(RESTORED_AT)
        cut = (
            datetime.fromtimestamp(int(restored), UTC) if restored and restored.isdigit() else None
        )
        skipped = 0
        newest = after
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
                        if cut is not None and server_time(item.ctime) < cut:
                            # Выполнено прежней базой до восстановления: повтор отменил бы его.
                            skipped += 1
                            newest = max(newest, item.ctime)
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
        if skipped:
            log.info("После восстановления базы не выполняются заново %d сообщений", skipped)
            await self._remember_ctime(newest)
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

    async def maintenance(self, stop: asyncio.Event, period: float = 60.0) -> None:
        """Раз в минуту — напоминание о копии, раз в час — чистка и отчет защитной копии."""
        try:
            await self.recover()
        except Exception:
            log.exception("Разбор дел прошлого запуска не удался")
        cleaned: datetime | None = None
        while not stop.is_set():
            try:
                now = self.clock()
                if cleaned is None or now - cleaned >= CLEAN_EVERY:
                    await self.clean()
                    await self.check_backup()
                    cleaned = now
                await self.remind()
                await self.report()
            except Exception:
                log.exception("Обслуживание не удалось")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=period)

    # ── Еженедельное напоминание ──────────────────────────────────────────────

    async def remind(self) -> None:
        """Напомнить владельцам о резервной копии, если наступил срок и напоминание еще не
        дошло. Только последний срок: за пропущенные недели пачки не будет."""
        weekly = self._config.reminder
        now = self.clock()
        plan = "нет"
        if weekly is not None:
            plan = f"{weekly.weekday}|{weekly.at.strftime('%H:%M')}|{weekly.zone.key}"
        since = await self._state(REMINDER_SINCE)
        if since is None or await self._state(REMINDER_SCHEDULE) != plan:
            # Первый запуск, новое расписание или снова включено: прошедший срок не пропущен.
            await self._set_state(REMINDER_SINCE, now.isoformat())
            await self._set_state(REMINDER_SCHEDULE, plan)
            return
        if weekly is None:
            return
        slot = weekly.slot(now)
        if slot < datetime.fromisoformat(since):
            return
        async with self._sessions() as session:
            if await copy_made_since(session, slot):
                # Копию на этой неделе уже сделали (в чате или в терминале): напоминать незачем.
                return
        await self._reopen_if_abandoned(slot, now)
        for owner in self._config.owner_ids:
            if not await self._reminder_due(slot, owner, now):
                continue
            try:
                await self._messenger.send(owner, markup.bold(copies.REMINDER_TEXT))
            except MessengerError as error:
                log.warning("Напоминание владельцу %s не дошло: %s", owner, error)
                await self._reminder_done(slot, owner, now, error=str(error))
                continue
            await self._reminder_done(slot, owner, now)

    async def _reopen_if_abandoned(self, slot: datetime, now: datetime) -> None:
        """«Да» на напоминание закрывает его только успешной копией. Владелец не подтвердил папку,
        отменил или копия не удалась — напоминание снова приходит всем владельцам."""
        async with self._sessions() as session, session.begin():
            rows = list(
                await session.scalars(
                    select(Reminder).where(Reminder.slot_at == slot).with_for_update()
                )
            )
            taken = next((row for row in rows if row.answer == "да"), None)
            if taken is None or taken.closed_at is None or taken.answered_by is None:
                return
            if now - taken.closed_at <= LIFETIME + REMINDER_GRACE:
                return
            since = taken.closed_at
            working = await session.scalar(
                select(BackupJob.id).where(
                    BackupJob.kind == "backup",
                    BackupJob.status.in_(("requested", "new", "running")),
                    BackupJob.created_at >= since,
                )
            )
            asking = await session.scalar(
                select(Pending.user_id).where(
                    Pending.user_id == taken.answered_by,
                    Pending.kind == copies.BACKUP_KIND,
                    Pending.expires_at > now,
                )
            )
            if working is not None or asking is not None or await copy_made_since(session, since):
                return
            for row in rows:
                row.closed_at = None
                row.answer = ""
                row.answered_by = None
                row.status = "failed"
                row.error = "копия по напоминанию не сделана, напоминание повторяется"
                row.tried_at = None
                row.sent_at = None
                row.first_sent_at = None
            log.info("Копия по напоминанию не сделана: напоминание повторяется")

    async def _reminder_due(self, slot: datetime, owner: int, now: datetime) -> bool:
        """Пора ли отправлять; если да — строка «отправляется» уже записана."""
        async with self._sessions() as session, session.begin():
            rows = list(
                await session.scalars(
                    select(Reminder).where(Reminder.slot_at == slot).with_for_update()
                )
            )
            if any(row.closed_at is not None for row in rows):
                return False
            mine = next((row for row in rows if row.owner_id == owner), None)
            first = None if mine is None else mine.first_sent_at or mine.sent_at
            if first is not None and now - first >= copies.REMINDER_WINDOW:
                # Дошло больше суток назад: ответ на него уже не принимается, повторять незачем.
                return False
            if mine is not None and mine.status == "sent":
                # Дошло. Повторить, только если после него владельцу задали другой вопрос: его
                # «Да» тогда относилось бы к тому вопросу, а не к напоминанию.
                asked = await pending.asked_at(session, owner)
                if asked is None or mine.sent_at is None or asked <= mine.sent_at:
                    return False
            if (
                mine is not None
                and mine.status == "failed"
                and mine.tried_at is not None
                and now - mine.tried_at < REMINDER_RETRY
            ):
                return False  # неудачная отправка повторяется не чаще раза в 10 минут
            # Владелец посреди другой операции: его «Да» относилось бы к ней. Подождем.
            busy = await session.scalar(
                select(Pending.user_id).where(Pending.user_id == owner, Pending.expires_at > now)
            )
            if busy is not None:
                return False
            if mine is None:
                mine = Reminder(slot_at=slot, owner_id=owner, attempts=0)
                session.add(mine)
            mine.status = "sending"
            mine.attempts = (mine.attempts or 0) + 1
            mine.tried_at = now
            return True

    async def _reminder_done(
        self, slot: datetime, owner: int, now: datetime, error: str = ""
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.scalar(
                select(Reminder)
                .where(Reminder.slot_at == slot, Reminder.owner_id == owner)
                .with_for_update()
            )
            if row is None:
                return
            if error:
                row.status = "failed"
                row.error = error[:300]
            else:
                row.status = "sent"
                row.sent_at = now
                row.first_sent_at = row.first_sent_at or now
                row.error = ""

    # ── После перезапуска ─────────────────────────────────────────────────────

    async def recover(self) -> None:
        """Дела, оборванные прошлым запуском бота. Итог владелец получит через `report`."""
        now = self.clock()
        async with self._sessions() as session, session.begin():
            # Снимок базы жил в транзакции прошлого запуска: эту копию уже не сделать.
            # Заявки из терминала не трогаются: ./backup.sh может еще ждать их сам.
            jobs = await session.scalars(
                select(BackupJob)
                .where(BackupJob.status.in_(("requested", "new")), BackupJob.source != "terminal")
                .with_for_update()
            )
            for job in jobs:
                job.status = "canceled"
                job.message = "бот перезапустился до начала копии, повторите: Резервная копия"
                job.finished_at = now
                job.csv_data = None
                job.snapshot = None
            # Замена не закоммичена: иначе статус был бы done в той же транзакции.
            uploads = await session.scalars(
                select(JournalUpload).where(JournalUpload.status == "confirmed").with_for_update()
            )
            for upload in uploads:
                upload.status = "refused"
                upload.message = "бот перезапустился до замены, журнал не менялся"
                upload.finished_at = now
        async with self._sessions() as session:
            running = list(
                await session.scalars(
                    select(BackupJob.id).where(
                        BackupJob.kind == "backup",
                        BackupJob.source != "terminal",
                        BackupJob.status == "running",
                    )
                )
            )
        # Контейнер мог успеть начать по старому снимку: итог придет, когда он закончит.
        for job_id in running:
            self.start(self._watch(job_id))
        await self.report()

    def _watch(self, job_id: int) -> Task:
        async def run() -> list[Reply]:
            await backups.wait(self._sessions, job_id)
            return []

        return run

    async def clean(self) -> None:
        """Журнал действий — 12 месяцев, отметки обработанных сообщений — 60 дней, файлы для
        замены журнала — сутки. Записи о копиях не удаляются."""
        now = self.clock()
        async with self._sessions() as session, session.begin():
            await session.execute(delete(AuditEntry).where(AuditEntry.at < now - AUDIT_KEEP))
            await session.execute(
                delete(ProcessedMessage).where(ProcessedMessage.processed_at < now - PROCESSED_KEEP)
            )
            await session.execute(delete(Pending).where(Pending.expires_at <= now))
            await session.execute(
                delete(JournalUpload).where(
                    JournalUpload.created_at < now - UPLOADS_KEEP,
                    JournalUpload.status != "confirmed",
                )
            )

    async def check_backup(self) -> None:
        """Отчет защитной копии перед обновлением (update.sh): сбой или копия не попала в
        заданную сетевую папку — сообщение владельцу."""
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
            await self.notify_owners(f"Защитная копия базы перед обновлением не сделана: {problem}")
        elif not status.get("share_ok") and "не задана" not in problem:
            await self.notify_owners(
                f"Защитная копия базы сделана, но не попала в сетевую папку: {problem}"
            )
