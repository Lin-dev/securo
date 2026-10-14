"""The span of complete data that yearly figures can be read from."""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction
from app.services._query_filters import counts_as_pnl

# Annualize only from this much complete history; below it a ratio of a few days is noise.
MIN_COVERAGE_DAYS = 30


async def coverage_window(session: AsyncSession, ws_id: uuid.UUID, today: date) -> dict[str, Any]:
    """The span of complete data that yearly figures can be read from.

    "The trailing 365 days" assumes a year of history. When the accounts were
    connected recently, most of that year is empty and a sum over it reads like
    a fraction of the real spending. The window therefore starts when the last of
    the currently active spending accounts (open, with a posted P&L debit in the
    last 90 days) has its first posted row, never earlier than today - 364, and
    figures over it are annualized by `scale` (365 / days). With less than
    MIN_COVERAGE_DAYS of history the plain trailing year is kept and flagged.
    """
    floor = today - timedelta(days=364)
    active = (
        select(Transaction.account_id)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.workspace_id == ws_id,
            Account.is_closed.is_(False),
            Transaction.type == "debit",
            Transaction.status == "posted",
            Transaction.date >= today - timedelta(days=90),
            Transaction.date <= today,
            Transaction.source != "opening_balance",
            counts_as_pnl(),
        )
        .distinct()
    )
    firsts = (
        await session.execute(
            select(func.min(Transaction.date))
            .where(
                Transaction.account_id.in_(active),
                Transaction.status == "posted",
                Transaction.source != "opening_balance",
            )
            .group_by(Transaction.account_id)
        )
    ).scalars().all()
    start = max([floor, *(d for d in firsts if d is not None)])
    days = (today - start).days + 1
    if days < MIN_COVERAGE_DAYS:
        return {"from_date": floor, "to_date": today, "days": 365, "scale": 1.0,
                "history_days": days, "annualized": False, "insufficient_history": True}
    return {"from_date": start, "to_date": today, "days": days, "scale": 365.0 / days,
            "history_days": days, "annualized": days < 365, "insufficient_history": False}
