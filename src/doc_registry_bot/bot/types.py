"""Общие типы бота и то, что бот ждет от мессенджера (протокол `Messenger`).

Бот не знает, как устроен мессенджер: в тестах вместо него подставляется подделка с тем же
протоколом, а настоящая связь живет в `doc_registry_bot.messenger`.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Incoming:
    """Входящее сообщение боту."""

    message_id: int
    sender_id: int
    text: str
    file_ids: list[int] = field(default_factory=list)
    ctime: int | None = None


@dataclass(frozen=True)
class UserCard:
    """Карточка пользователя мессенджера."""

    user_id: int
    family: str = ""
    first_name: str = ""
    patronymic: str = ""
    division: str = ""
    post: str = ""

    @property
    def fio(self) -> str:
        return " ".join(part for part in (self.family, self.first_name, self.patronymic) if part)


@dataclass(frozen=True)
class Attachment:
    """Файл для отправки: имя и содержимое."""

    name: str
    data: bytes


@dataclass(frozen=True)
class Reply:
    """Ответ бота: текст разметкой HTML (строки через «\\n») и, если нужно, файлы."""

    user_id: int
    html: str
    files: list[Attachment] = field(default_factory=list)


@dataclass(frozen=True)
class HistoryMessage:
    """Сообщение из истории переписки; `ctime` — время сервера в его единицах."""

    message_id: int
    owner_id: int
    text: str
    ctime: int
    file_ids: list[int] = field(default_factory=list)


class MessengerError(Exception):
    """Служба мессенджера не ответила или ответила ошибкой; текст годится для журнала."""


class Messenger(Protocol):
    async def send(self, user_id: int, html: str, files: list[Attachment] | None = None) -> None:
        """Отправить сообщение человеку; файлы загружаются блоками после создания сообщения."""

    async def download(self, message_id: int, file_id: int) -> Attachment:
        """Скачать вложение сообщения (ждет готовности файла на сервере)."""

    async def user_card(self, user_id: int) -> UserCard | None:
        """Карточка пользователя; None — такого пользователя нет."""

    async def contacts(self) -> list[UserCard]:
        """Список контактов бота."""

    async def history(self, user_id: int, after: int, limit: int) -> list[HistoryMessage]:
        """Сообщения переписки с человеком после времени сервера `after`, по возрастанию."""
