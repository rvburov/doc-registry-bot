"""Таблицы базы. Схему меняют только миграции Alembic (`migrations/`), и только добавлением."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JournalEntry(Base):
    """Строка журнала КД: восемь столбцов журнала и служебные поля.

    `designation_key` — обозначение, приведенное к одному виду (заглавные, похожая латиница
    русскими буквами). На нем уникальный индекс: один номер не может быть выдан дважды.
    `series` — «АБВГ.301121.», по нему номера серии выбираются одним запросом.
    `version` растет при каждом изменении: подтверждение «Да» сверяет его со снимком.
    """

    __tablename__ = "journal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    designation: Mapped[str] = mapped_column(Text)
    designation_key: Mapped[str] = mapped_column(Text, unique=True)
    series: Mapped[str] = mapped_column(Text, index=True)
    name: Mapped[str] = mapped_column(Text)
    fio: Mapped[str] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    project: Mapped[str] = mapped_column(Text)
    accounting: Mapped[str] = mapped_column(Text, default="", server_default="")
    first_use: Mapped[str] = mapped_column(Text, default="", server_default="")
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    registrant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Member(Base):
    """Человек, известный боту: доступ к регистрации и роль администратора.

    Владельцы задаются в `.env` и здесь не хранятся: командой бота их не снять.
    """

    __tablename__ = "members"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    fio: Mapped[str] = mapped_column(Text, default="", server_default="")
    has_access: Mapped[bool] = mapped_column(default=False, server_default="false")
    is_admin: Mapped[bool] = mapped_column(default=False, server_default="false")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Pending(Base):
    """Операция, ожидающая ответа человека («Да/Нет», выбор номера из списка). Одна на человека."""

    __tablename__ = "pending"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedMessage(Base):
    """Обработанное сообщение. Пишется в той же транзакции, что и его результат: повторная
    доставка или дочитывание истории не выполнит его второй раз."""

    __tablename__ = "processed_messages"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    sender_id: Mapped[int] = mapped_column(BigInteger)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuditEntry(Base):
    """Журнал действий: кто, когда, что сделал. Хранится 12 месяцев."""

    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_fio: Mapped[str] = mapped_column(Text, default="", server_default="")
    action: Mapped[str] = mapped_column(Text)
    details: Mapped[str] = mapped_column(Text, default="", server_default="")

    __table_args__ = (Index("ix_audit_at", "at"),)


class BotState(Base):
    """Служебные значения: до какого места дочитана история, последний отчет о копии."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
