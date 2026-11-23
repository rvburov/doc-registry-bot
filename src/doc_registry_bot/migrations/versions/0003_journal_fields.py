"""Подразделение регистрирующего и актуальность документа без изменения старых записей.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("journal")}
    if "division" not in existing:
        op.add_column(
            "journal", sa.Column("division", sa.Text(), nullable=False, server_default="")
        )
    if "status" not in existing:
        op.add_column(
            "journal",
            sa.Column("status", sa.Text(), nullable=False, server_default="Зарегистрирован"),
        )

    constraints = sa.inspect(op.get_bind()).get_check_constraints("journal")
    if not any(item["name"] == "ck_journal_status" for item in constraints):
        op.create_check_constraint(
            "ck_journal_status", "journal", "status IN ('Зарегистрирован', 'Аннулирован')"
        )


def downgrade() -> None:
    """Данные сохраняются и при откате версии приложения."""
