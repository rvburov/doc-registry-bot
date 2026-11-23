"""Что нужно обработчику одного сообщения: сессия базы, настройки, отправитель, ответы."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.types import Attachment, Messenger, Reply
from doc_registry_bot.db.models import AuditEntry
from doc_registry_bot.registry.folders import Folder
from doc_registry_bot.registry.schedule import Weekly


@dataclass(frozen=True)
class BotConfig:
    """Настройки поведения бота (часть `Settings`, без адресов и паролей)."""

    owner_ids: tuple[int, ...]
    org_codes: tuple[str, ...]
    zone: ZoneInfo
    catchup_hours: int = 24
    # Папки для резервных копий (BACKUP_DIR_1..3 и сетевая) и еженедельное напоминание.
    folders: tuple[Folder, ...] = ()
    reminder: Weekly | None = None


LIFETIME = timedelta(minutes=10)

# Дело после коммита: долгая работа (резервная копия, замена журнала) идет вне транзакции
# сообщения и вне очереди сообщений; итог — ответы, которые служба отправит, когда дело кончится.
Task = Callable[[], Awaitable[list[Reply]]]


def now_utc() -> datetime:
    return datetime.now(UTC)


def server_time(ctime: int) -> datetime:
    """Время сервера мессенджера: миллисекунды или секунды от 1970 года (по величине числа)."""
    seconds = ctime / 1000 if ctime > 10**11 else ctime
    return datetime.fromtimestamp(seconds, UTC)


@dataclass
class Ctx:
    """Обработка одного сообщения. Ответы копятся и уходят после коммита транзакции."""

    session: AsyncSession
    config: BotConfig
    messenger: Messenger
    sender_id: int
    now: datetime
    sender_fio: str = ""
    replies: list[Reply] = field(default_factory=list)
    # Для дел после коммита: своя фабрика сессий и часы (в тестах часы стенда).
    sessions: async_sessionmaker[AsyncSession] | None = None
    clock: Callable[[], datetime] = now_utc
    tasks: list[Task] = field(default_factory=list)
    # Когда человек отправил сообщение (время сервера мессенджера); None — неизвестно.
    sent_at: datetime | None = None
    sender_division: str = ""
    contact_divisions: dict[str, str] | None = None

    def reply(self, html: str, files: list[Attachment] | None = None) -> None:
        self.replies.append(Reply(self.sender_id, html, files or []))

    def send_to(self, user_id: int, html: str, files: list[Attachment] | None = None) -> None:
        self.replies.append(Reply(user_id, html, files or []))

    def later(self, task: Task) -> None:
        """Выполнить после коммита, вне очереди сообщений."""
        self.tasks.append(task)

    def audit(self, action: str, details: str = "") -> AuditEntry:
        entry = AuditEntry(
            at=self.now,
            actor_id=self.sender_id,
            actor_fio=self.sender_fio,
            action=action,
            details=details,
        )
        self.session.add(entry)
        return entry

    def local(self, moment: datetime) -> str:
        """Момент по местному времени: «10.11.2026 14:05»."""
        return moment.astimezone(self.config.zone).strftime("%d.%m.%Y %H:%M")

    @property
    def orgs(self) -> list[str]:
        return list(self.config.org_codes)
