"""Таблицы базы. Схему меняют только миграции Alembic (`migrations/`), и только добавлением."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JournalEntry(Base):
    """Строка журнала КД: десять столбцов журнала и служебные поля.

    `designation_key` — обозначение, приведенное к одному виду (заглавные, похожая латиница
    русскими буквами). На нем уникальный индекс: один номер не может быть выдан дважды.
    `series` — «АБВГ.301121.», по нему номера серии выбираются одним запросом.
    `version` растет при каждом изменении: подтверждение «Да» сверяет его со снимком.
    """

    __tablename__ = "journal"
    __table_args__ = (
        CheckConstraint("status IN ('Зарегистрирован', 'Аннулирован')", name="ck_journal_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    designation: Mapped[str] = mapped_column(Text)
    designation_key: Mapped[str] = mapped_column(Text, unique=True)
    series: Mapped[str] = mapped_column(Text, index=True)
    name: Mapped[str] = mapped_column(Text)
    fio: Mapped[str] = mapped_column(Text)
    division: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(
        Text, default="Зарегистрирован", server_default="Зарегистрирован"
    )
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


class BackupJob(Base):
    """Резервная копия: заявка контейнеру копий и ее итог (`kind="backup"`) или проверка файлов
    уже сделанной копии перед заменой журнала (`kind="verify"`, `target_id` — копия).

    Заявку пишет бот, выполняет контейнер копий: забирает строку `status="new"` и пишет итог в
    нее же. Копия успешна, только если `status="done"` и оба файла на месте (`dump_ok`, `csv_ok`).
    `fingerprint` — SHA-256 файла CSV журнала: замена журнала сверяет с ним журнал в базе.
    Статусы: requested (бот готовит), new (ждет контейнер), running, done, failed, canceled.
    """

    __tablename__ = "backup_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, default="backup", server_default="backup")
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(Text, default="chat", server_default="chat")
    requested_by: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    requested_fio: Mapped[str] = mapped_column(Text, default="", server_default="")
    folder_key: Mapped[str] = mapped_column(Text, default="", server_default="")
    folder_label: Mapped[str] = mapped_column(Text, default="", server_default="")
    subdir: Mapped[str] = mapped_column(Text, default="", server_default="")
    dump_name: Mapped[str] = mapped_column(Text, default="", server_default="")
    csv_name: Mapped[str] = mapped_column(Text, default="", server_default="")
    csv_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(Text, default="", server_default="")
    rows: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    owner_uid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_gid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="requested", server_default="requested")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dump_ok: Mapped[bool] = mapped_column(default=False, server_default="false")
    csv_ok: Mapped[bool] = mapped_column(default=False, server_default="false")
    dump_size: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    csv_size: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Контрольная сумма DUMP (проверка перед заменой журнала) и путь папки на компьютере, куда
    # контейнер копий на самом деле записал файлы (из его настроек, для отчета).
    dump_sha256: Mapped[str] = mapped_column(Text, default="", server_default="")
    host_path: Mapped[str] = mapped_column(Text, default="", server_default="")

    __table_args__ = (Index("ix_backup_jobs_status", "status", "id"),)

    @property
    def successful(self) -> bool:
        return self.kind == "backup" and self.status == "done" and self.dump_ok and self.csv_ok


class JournalUpload(Base):
    """Файл CSV для замены журнала через чат: ждет подтверждения владельца.

    Статусы: waiting (ждет «Да»), confirmed (замена выполняется), done, refused, canceled.
    """

    __tablename__ = "journal_uploads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_fio: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    name: Mapped[str] = mapped_column(Text)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(Text)
    rows: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    backup_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="waiting", server_default="waiting")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Reminder(Base):
    """Еженедельное напоминание владельцу о резервной копии: срок, кому и доставлено ли.

    Одна строка на срок и владельца (уникально): перезапуск бота не отправит его второй раз.
    Статусы доставки: sending (отправляется), sent, failed (не дошло, будет повтор).
    Ответ одного владельца закрывает напоминание у всех (`closed_at`, `answer`, `answered_by`).
    """

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    owner_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, default="sending", server_default="sending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str] = mapped_column(Text, default="", server_default="")
    tried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Последняя доставка (от нее сутки на ответ; напоминание повторяют, если после него владельцу
    # задали другой вопрос) и первая: повторы идут только в первые сутки после нее.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answer: Mapped[str] = mapped_column(Text, default="", server_default="")
    answered_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (UniqueConstraint("slot_at", "owner_id", name="uq_reminders_slot_owner"),)
