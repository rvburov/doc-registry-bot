"""Резервные копии по команде владельца, замена журнала через чат, еженедельное напоминание.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _missing(table: str) -> bool:
    """Таблицы может уже быть: базу восстановили из копии, сделанной до этой миграции, поверх
    базы, где она уже прошла. Тогда миграция ничего не создает заново."""
    return not sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _missing("backup_jobs"):
        _backup_jobs()
    if _missing("journal_uploads"):
        _journal_uploads()
    if _missing("reminders"):
        _reminders()


def _backup_jobs() -> None:
    op.create_table(
        "backup_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False, server_default="backup"),
        sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="chat"),
        sa.Column("requested_by", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("requested_fio", sa.Text(), nullable=False, server_default=""),
        sa.Column("folder_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("folder_label", sa.Text(), nullable=False, server_default=""),
        sa.Column("subdir", sa.Text(), nullable=False, server_default=""),
        sa.Column("dump_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("csv_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("csv_data", sa.LargeBinary(), nullable=True),
        sa.Column("snapshot", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=False, server_default=""),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("owner_uid", sa.Integer(), nullable=True),
        sa.Column("owner_gid", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dump_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("csv_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dump_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("csv_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dump_sha256", sa.Text(), nullable=False, server_default=""),
        sa.Column("host_path", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_backup_jobs_status", "backup_jobs", ["status", "id"])


def _journal_uploads() -> None:
    op.create_table(
        "journal_uploads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_fio", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("backup_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="waiting"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
    )


def _reminders() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("slot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="sending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("tried_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer", sa.Text(), nullable=False, server_default=""),
        sa.Column("answered_by", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("slot_at", "owner_id", name="uq_reminders_slot_owner"),
    )


def downgrade() -> None:
    raise NotImplementedError("Миграции только добавляющие, отката схемы нет")
