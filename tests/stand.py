"""Стенд бота: диспетчер на настоящей базе и подделка мессенджера."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from doc_registry_bot.bot.context import BotConfig
from doc_registry_bot.bot.dispatcher import Dispatcher
from doc_registry_bot.bot.types import Incoming, Reply
from doc_registry_bot.db import journal
from doc_registry_bot.db.models import AuditEntry, JournalEntry, Member
from doc_registry_bot.registry import markup
from tests.fakes import FakeMessenger
from tests.texts import assert_human_text

OWNER = 1
ADMIN = 2
REGISTRAR = 1001
STRANGER = 9999


class Stand:
    def __init__(self, db: async_sessionmaker[AsyncSession], config: BotConfig) -> None:
        self.db = db
        self.messenger = FakeMessenger()
        self.dispatcher = Dispatcher(db, config, self.messenger)
        self.now = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
        self.message_id = 10_000
        self.replies: list[Reply] = []
        self.messenger.person(OWNER, "Владельцев", "Владимир", "Владимирович")
        self.messenger.person(ADMIN, "Админов", "Андрей", "Андреевич")
        self.messenger.person(
            REGISTRAR, "Иванов", "Иван", "Иванович", "Отдел главного конструктора"
        )
        self.messenger.person(STRANGER, "Чужов", "Степан", "Степанович")

    async def say(
        self, sender: int, text: str, files: list[int] | None = None, message_id: int | None = None
    ) -> str:
        """Сообщение боту; ответ отправителю — разметкой, ответы разделены пустой строкой."""
        if message_id is None:
            self.message_id += 1
            message_id = self.message_id
        self.replies = await self.dispatcher.handle(
            Incoming(message_id, sender, text, files or []), self.now
        )
        for reply in self.replies:
            assert_human_text(markup.plain(reply.html))
        return "\n\n".join(r.html for r in self.replies if r.user_id == sender)

    def later(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)

    async def grant(self, user_id: int, admin: bool = False) -> None:
        card = self.messenger.cards[user_id]
        async with self.db() as session, session.begin():
            session.add(Member(user_id=user_id, fio=card.fio, has_access=True, is_admin=admin))

    async def designations(self) -> list[str]:
        async with self.db() as session:
            rows = await session.scalars(select(JournalEntry.designation).order_by(JournalEntry.id))
            return list(rows)

    async def entries(self) -> list[JournalEntry]:
        async with self.db() as session:
            return await journal.all_entries(session)

    async def add(self, *designations: str) -> None:
        async with self.db() as session, session.begin():
            await journal.add_entries(
                session,
                [
                    journal.NewEntry(value, "Кронштейн", "Иванов Иван Иванович", "Машина")
                    for value in designations
                ],
                self.now,
            )

    async def member(self, user_id: int) -> Member | None:
        async with self.db() as session:
            return await session.get(Member, user_id)

    async def audit(self) -> list[AuditEntry]:
        async with self.db() as session:
            return list(await session.scalars(select(AuditEntry).order_by(AuditEntry.id)))


def registration(
    number: str = "АБВГ.301121.ХХХ", name: str = "Кронштейн", project: str = "Машина"
) -> str:
    return f"Действие: Регистрация\nНомер: {number}\nНаименование: {name}\nПроект: {project}"
