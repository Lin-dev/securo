"""merge the fork branch back into upstream's chain (fork)

qc001 chains off upstream 085, and upstream's 086–096 chain off 085 too, so
the history has two heads. This revision names both as parents and has no
operations; afterwards `alembic upgrade head` resolves to one revision again.
Each upstream sync that brings a new migration needs another qc00N merge.

Revision ID: qc002
Revises: 096, qc001
Create Date: 2026-10-13
"""

from typing import Sequence, Union

revision: str = "qc002"
down_revision: Union[str, Sequence[str], None] = ("096", "qc001")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
