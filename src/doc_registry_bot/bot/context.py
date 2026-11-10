"""Что нужно обработчику одного сообщения: сессия базы, настройки, отправитель, ответы."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from doc_registry_bot.bot.types import Attachment, Messenger, Reply
from doc_registry_bot.db.models import AuditEntry


@dataclass(frozen=True)
class BotConfig:
    """Настройки поведения бота (часть `Settings`, без адресов и паролей)."""

    owner_ids: tuple[int, ...]
    org_codes: tuple[str, ...]
    zone: ZoneInfo
    catchup_hours: int = 24


LIFETIME = timedelta(minutes=10)


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

    def reply(self, html: str, files: list[Attachment] | None = None) -> None:
        self.replies.append(Reply(self.sender_id, html, files or []))

    def send_to(self, user_id: int, html: str, files: list[Attachment] | None = None) -> None:
        self.replies.append(Reply(user_id, html, files or []))

    def audit(self, action: str, details: str = "") -> None:
        self.session.add(
            AuditEntry(
                at=self.now,
                actor_id=self.sender_id,
                actor_fio=self.sender_fio,
                action=action,
                details=details,
            )
        )

    def local(self, moment: datetime) -> str:
        """Момент по местному времени: «10.11.2026 14:05»."""
        return moment.astimezone(self.config.zone).strftime("%d.%m.%Y %H:%M")

    @property
    def orgs(self) -> list[str]:
        return list(self.config.org_codes)
