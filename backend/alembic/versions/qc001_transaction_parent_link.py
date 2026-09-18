"""link category split lines to their parent transaction (fork)

Fork migration on its own Alembic branch ("qc"). It chains off upstream 085,
the head of v0.15.1; upstream's later migrations chain off 085 as well, so a
future rebase leaves two heads and is upgraded with `alembic upgrade heads`.
The column name matches upstream issue #425 on purpose.

Revision ID: qc001
Revises: 085
Create Date: 2026-09-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "qc001"
down_revision: Union[str, None] = "085"
branch_labels: Union[str, Sequence[str], None] = ("qc",)
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column(
            "parent_transaction_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_transactions_parent_transaction_id",
        "transactions",
        "transactions",
        ["parent_transaction_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_transactions_parent_transaction_id",
        "transactions",
        ["parent_transaction_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_parent_transaction_id", table_name="transactions")
    op.drop_constraint(
        "fk_transactions_parent_transaction_id", "transactions", type_="foreignkey"
    )
    op.drop_column("transactions", "parent_transaction_id")
