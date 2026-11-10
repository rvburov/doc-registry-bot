"""Подделка мессенджера в памяти: тот же протокол, что у настоящей службы."""

from dataclasses import dataclass, field

from doc_registry_bot.bot.types import Attachment, HistoryMessage, MessengerError, UserCard


@dataclass
class Sent:
    user_id: int
    html: str
    files: list[Attachment]


@dataclass
class FakeMessenger:
    cards: dict[int, UserCard] = field(default_factory=dict)
    files: dict[tuple[int, int], Attachment] = field(default_factory=dict)
    sent: list[Sent] = field(default_factory=list)
    histories: dict[int, list[HistoryMessage]] = field(default_factory=dict)
    broken: set[int] = field(default_factory=set)
    offline: bool = False

    def person(
        self, user_id: int, family: str, name: str, patronymic: str, division: str = ""
    ) -> UserCard:
        card = UserCard(user_id, family, name, patronymic, division)
        self.cards[user_id] = card
        return card

    async def send(self, user_id: int, html: str, files: list[Attachment] | None = None) -> None:
        if self.offline or user_id in self.broken:
            raise MessengerError("служба мессенджера не ответила")
        self.sent.append(Sent(user_id, html, list(files or [])))

    async def download(self, message_id: int, file_id: int) -> Attachment:
        if self.offline or (message_id, file_id) not in self.files:
            raise MessengerError("файл не найден")
        return self.files[(message_id, file_id)]

    async def user_card(self, user_id: int) -> UserCard | None:
        if self.offline:
            raise MessengerError("служба мессенджера не ответила")
        return self.cards.get(user_id)

    async def contacts(self) -> list[UserCard]:
        if self.offline:
            raise MessengerError("служба мессенджера не ответила")
        return list(self.cards.values())

    async def history(self, user_id: int, after: int, limit: int) -> list[HistoryMessage]:
        if self.offline:
            raise MessengerError("служба мессенджера не ответила")
        items = [m for m in self.histories.get(user_id, []) if m.ctime > after]
        return sorted(items, key=lambda m: m.ctime)[:limit]
