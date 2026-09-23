"""Deterministic figures for the scheduled finance digest.

The digest is written by the workspace's default agent, but every number
it narrates comes from here — the same service functions the reports and
the transactions summary line use — so a small local model cannot invent
figures. `build_figures_pack` returns plain data; `render_figures_pack`
turns it into the markdown block the model reads.
"""
from __future__ import annotations

import calendar
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.transaction import Transaction
from app.services import report_service, transaction_service
from app.services._query_filters import counts_as_pnl, is_split_parent

logger = logging.getLogger(__name__)

DigestKind = Literal["weekly", "monthly"]
TOP_CATEGORY_DELTAS = 5


def period_bounds(kind: DigestKind, today: date) -> tuple[date, date, date, date]:
    """(current_from, current_to, previous_from, previous_to).

    weekly  → the last complete Monday–Sunday week before `today`, and the week before it.
    monthly → the last complete calendar month before `today`, and the month before it.
    """
    if kind == "weekly":
        cur_to = today - timedelta(days=today.weekday() + 1)  # last Sunday
        cur_from = cur_to - timedelta(days=6)
        prev_to = cur_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=6)
        return cur_from, cur_to, prev_from, prev_to
    if kind == "monthly":
        first_this = today.replace(day=1)
        cur_to = first_this - timedelta(days=1)
        cur_from = cur_to.replace(day=1)
        prev_to = cur_from - timedelta(days=1)
        prev_from = prev_to.replace(day=1)
        return cur_from, cur_to, prev_from, prev_to
    raise ValueError(f"unknown digest kind {kind!r}")


def digest_title(kind: DigestKind, today: date) -> str:
    cur_from, cur_to, _, _ = period_bounds(kind, today)
    if kind == "weekly":
        return f"Weekly review — {cur_from.isoformat()} to {cur_to.isoformat()}"
    return f"Monthly review — {calendar.month_name[cur_from.month]} {cur_from.year}"


def is_due(kind: DigestKind, local_now: datetime, hour: int) -> bool:
    """True when the hourly tick falls on the digest's day at or after `hour` (local time)."""
    if local_now.hour < hour:
        return False
    if kind == "weekly":
        return local_now.weekday() == 0  # Monday
    if kind == "monthly":
        return local_now.day == 1
    return False


def _num(value: Any) -> float:
    return float(value or 0)


async def summary_window(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """Income / expense / net / invested / excluded for a window, exactly as the
    Transactions summary line computes them (the summary is built over the whole
    filtered set before pagination, so one row is enough)."""
    _, _, summary = await transaction_service.get_transactions(
        session,
        workspace_id,
        user_id,
        from_date=from_date,
        to_date=to_date,
        page=1,
        limit=1,
        include_summary=True,
    )
    summary = summary or {}
    income = _num(summary.get("income"))
    expense = _num(summary.get("expense"))
    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "income": income,
        "expense": expense,
        "net": _num(summary.get("net")),
        "invested": _num(summary.get("invested")),
        "excluded": _num(summary.get("excluded")),
        "savings_rate": round((income - expense) / income, 4) if income > 0 else None,
    }


async def expense_by_category(
    session: AsyncSession, workspace_id: uuid.UUID, from_date: date, to_date: date
) -> dict[str, float]:
    """P&L debits per category name in the window (cash dating), primary-currency
    amounts when stamped. Uncategorized rows land under "Uncategorized"."""
    amount = func.coalesce(Transaction.amount_primary, Transaction.amount)
    rows = (
        await session.execute(
            select(Category.name, func.sum(amount))
            .select_from(Transaction)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.workspace_id == workspace_id,
                Transaction.type == "debit",
                Transaction.date >= from_date,
                Transaction.date <= to_date,
                Transaction.source != "opening_balance",
                Transaction.status == "posted",
                counts_as_pnl(),
                ~is_split_parent(),
            )
            .group_by(Category.name)
        )
    ).all()
    return {(name or "Uncategorized"): _num(total) for name, total in rows}


async def uncategorized_count(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, from_date: date, to_date: date
) -> int:
    _, total, _ = await transaction_service.get_transactions(
        session, workspace_id, user_id, from_date=from_date, to_date=to_date, page=1, limit=1, uncategorized=True
    )
    return int(total or 0)


async def _net_worth(session: AsyncSession, workspace_id: uuid.UUID, cutoff: date, currency: str) -> Optional[float]:
    try:
        point = await report_service._net_worth_at(session, workspace_id, cutoff, currency)
        return round(float(point.value), 2)
    except Exception:  # noqa: BLE001
        logger.exception("net worth for the digest failed at %s", cutoff)
        return None


def _delta(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None:
        return None
    return round(cur - prev, 2)


async def build_figures_pack(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: DigestKind,
    today: date,
    currency: str = "USD",
) -> dict[str, Any]:
    cur_from, cur_to, prev_from, prev_to = period_bounds(kind, today)
    current = await summary_window(session, workspace_id, user_id, cur_from, cur_to)
    previous = await summary_window(session, workspace_id, user_id, prev_from, prev_to)

    cur_cats = await expense_by_category(session, workspace_id, cur_from, cur_to)
    prev_cats = await expense_by_category(session, workspace_id, prev_from, prev_to)
    moves = [
        {
            "category": name,
            "current": round(cur_cats.get(name, 0.0), 2),
            "previous": round(prev_cats.get(name, 0.0), 2),
            "delta": round(cur_cats.get(name, 0.0) - prev_cats.get(name, 0.0), 2),
        }
        for name in set(cur_cats) | set(prev_cats)
    ]
    moves.sort(key=lambda m: (-abs(m["delta"]), m["category"]))

    nw_end = await _net_worth(session, workspace_id, cur_to, currency)
    nw_start = await _net_worth(session, workspace_id, prev_to, currency)

    return {
        "kind": kind,
        "currency": currency,
        "current": current,
        "previous": previous,
        "deltas": {
            "income": _delta(current["income"], previous["income"]),
            "expense": _delta(current["expense"], previous["expense"]),
            "net": _delta(current["net"], previous["net"]),
            "invested": _delta(current["invested"], previous["invested"]),
            "savings_rate": _delta(current["savings_rate"], previous["savings_rate"]),
        },
        "top_category_deltas": moves[:TOP_CATEGORY_DELTAS],
        "uncategorized_count": await uncategorized_count(session, workspace_id, user_id, cur_from, cur_to),
        "net_worth": {"start": nw_start, "end": nw_end, "delta": _delta(nw_end, nw_start)},
    }


def _money(value: Optional[float], currency: str) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f} {currency}"


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_figures_pack(pack: dict[str, Any]) -> str:
    cur, prev, d = pack["current"], pack["previous"], pack["deltas"]
    ccy = pack["currency"]
    lines = [
        f"## Figures ({pack['kind']} review, computed by Securo)",
        f"Current period: {cur['from_date']} to {cur['to_date']}. "
        f"Previous period: {prev['from_date']} to {prev['to_date']}. Currency: {ccy}.",
        "",
        "| Figure | Current | Previous | Change |",
        "|---|---|---|---|",
        f"| Income | {_money(cur['income'], ccy)} | {_money(prev['income'], ccy)} | {_money(d['income'], ccy)} |",
        f"| Expenses | {_money(cur['expense'], ccy)} | {_money(prev['expense'], ccy)} | {_money(d['expense'], ccy)} |",
        f"| Net | {_money(cur['net'], ccy)} | {_money(prev['net'], ccy)} | {_money(d['net'], ccy)} |",
        f"| Invested | {_money(cur['invested'], ccy)} | {_money(prev['invested'], ccy)} | {_money(d['invested'], ccy)} |",
        f"| Savings rate (net / income) | {_pct(cur['savings_rate'])} | {_pct(prev['savings_rate'])} | "
        f"{_pct(d['savings_rate'])} |",
        "",
        "Largest category moves (expenses, current vs previous):",
    ]
    if pack["top_category_deltas"]:
        for m in pack["top_category_deltas"]:
            lines.append(
                f"- {m['category']}: {_money(m['current'], ccy)} vs {_money(m['previous'], ccy)} "
                f"({'+' if m['delta'] >= 0 else ''}{m['delta']:,.2f})"
            )
    else:
        lines.append("- none")
    nw = pack["net_worth"]
    lines += [
        "",
        f"Uncategorized transactions in the current period: {pack['uncategorized_count']}.",
        f"Net worth: {_money(nw['end'], ccy)} at period end, {_money(nw['start'], ccy)} at the previous period end "
        f"(change {_money(nw['delta'], ccy)}).",
    ]
    return "\n".join(lines)
