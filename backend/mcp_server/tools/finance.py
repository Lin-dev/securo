"""Finance-analysis tools (fork addition, qc7).

Thin wrappers over the services the UI already uses, so the agent quotes the
same numbers as the Transactions summary line, the Money Map, the Rules page
and the investment accounts. Nothing here writes.
"""
from __future__ import annotations

import calendar
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User
from app.services import (
    account_service,
    asset_service,
    fire_service,
    report_service,
    rule_service,
    transaction_service,
)
from app.services._query_filters import is_split_parent
from app.services.fire_service import FireInputs
from app.services.report_service import _report_start_date
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import (
    merchant_key,
    num,
    parse_date,
    parse_uuid,
    parse_uuid_list,
    resolve_workspace_id,
    suggest_pattern,
)

# Lanes of the Money Map, left column then right column (see
# frontend/src/lib/money-map-utils.ts `buildMoneyMap`).
MONEY_MAP_LANES = (
    "income",
    "direct_contributions",
    "transfers_in",
    "expenses",
    "investments",
    "transfers_out",
)

# Hard caps regardless of LLM input — payloads must stay small for local models.
_MAX_MONTH_WINDOWS = 24
_MAX_LANE_ITEMS = 10
_MAX_MERCHANT_ROWS = 5000
_MAX_MERCHANTS = 100
_MAX_RULES = 300

_SUMMARY_NOTES = (
    "income/expense/net are the P&L figures: paired transfers and transfer-style "
    "categories (Internal transfer, Card payment, Investment contribution, Brokerage "
    "activity, Retirement plan activity) are already left out. invested is money moved "
    "into investments in the window, counted once per movement; it is not part of "
    "expense, so net already contains it. savings_rate = net / income."
)


async def _primary_currency(session: AsyncSession, ctx: CallContext) -> str:
    user = await session.get(User, ctx.user_id)
    return user.primary_currency if user is not None else "USD"


def _round(v: float) -> float:
    return round(float(v), 2)


# --- shared window summary ----------------------------------------------------

async def _summary_window(
    session: AsyncSession,
    ws_id: uuid.UUID,
    user_id: uuid.UUID,
    fd: date,
    td: date,
    *,
    account_ids: Optional[list[uuid.UUID]] = None,
    accounting_mode: Optional[str] = None,
) -> dict[str, Any]:
    """The Transactions summary line for one date window.

    `get_transactions` computes the summary over every matching row before it
    paginates, so `limit=1` is enough and no rows are materialised. Reused by
    `fire_projection` and the digest so all three quote one definition.
    """
    _, _, summary = await transaction_service.get_transactions(
        session,
        ws_id,
        user_id,
        from_date=fd,
        to_date=td,
        page=1,
        limit=1,
        include_summary=True,
        account_ids=account_ids,
        accounting_mode=accounting_mode,
    )
    summary = summary or {}
    income = num(summary.get("income")) or 0.0
    expense = num(summary.get("expense")) or 0.0
    net = income - expense
    return {
        "from_date": fd.isoformat(),
        "to_date": td.isoformat(),
        "income": _round(income),
        "expense": _round(expense),
        "net": _round(net),
        "invested": _round(num(summary.get("invested")) or 0.0),
        "excluded": _round(num(summary.get("excluded")) or 0.0),
        "savings_rate": round(net / income, 4) if income > 0 else None,
    }


def _month_windows(fd: date, td: date) -> list[tuple[date, date]]:
    """Calendar months touching [fd, td], each clipped to the range."""
    out: list[tuple[date, date]] = []
    cur = date(fd.year, fd.month, 1)
    while cur <= td:
        last = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        out.append((max(cur, fd), min(last, td)))
        cur = last + timedelta(days=1)
    return out


@tool(
    name="get_transactions_summary",
    description=(
        "Income, expense, net, invested and excluded for a date range — the same "
        "figures as the Transactions summary line. Use this (not list_transactions "
        "or aggregate) for 'how much did I earn/spend/save/invest' in a period, and "
        "group_by='month' for month-by-month comparisons. savings_rate is net/income."
    ),
    parameters={
        "type": "object",
        "properties": {
            "from_date": {"type": "string", "format": "date", "description": "Inclusive (YYYY-MM-DD)"},
            "to_date": {"type": "string", "format": "date", "description": "Inclusive (YYYY-MM-DD)"},
            "group_by": {
                "type": "string",
                "enum": ["none", "month"],
                "default": "none",
                "description": "'month' adds one row per calendar month in the range (max 24)",
            },
            "account_ids": {
                "type": "array",
                "items": {"type": "string", "format": "uuid"},
                "description": "Restrict to these accounts",
            },
            "accounting_mode": {
                "type": "string",
                "enum": ["cash", "accrual"],
                "description": "Credit-card bucketing: cash = purchase date (default), accrual = bill date",
            },
        },
        "required": ["from_date", "to_date"],
        "additionalProperties": False,
    },
    tags=["read", "finance"],
)
async def get_transactions_summary(
    *,
    session: AsyncSession,
    ctx: CallContext,
    from_date: str,
    to_date: str,
    group_by: str = "none",
    account_ids: list[str] | None = None,
    accounting_mode: str | None = None,
) -> dict[str, Any]:
    fd = parse_date(from_date)
    td = parse_date(to_date)
    if fd is None or td is None:
        return {"error": "from_date and to_date are required (YYYY-MM-DD)"}
    if fd > td:
        return {"error": "from_date must not be after to_date"}
    if accounting_mode not in (None, "cash", "accrual"):
        return {"error": f"unknown accounting_mode: {accounting_mode}"}
    if group_by not in ("none", "month"):
        return {"error": f"unknown group_by: {group_by}"}

    ws_id = await resolve_workspace_id(session, ctx)
    accs = parse_uuid_list(account_ids)
    windows = _month_windows(fd, td) if group_by == "month" else []
    if len(windows) > _MAX_MONTH_WINDOWS:
        return {"error": f"range too long for group_by=month (max {_MAX_MONTH_WINDOWS} months)"}

    total = await _summary_window(
        session, ws_id, ctx.user_id, fd, td, account_ids=accs, accounting_mode=accounting_mode
    )
    out: dict[str, Any] = {"currency": await _primary_currency(session, ctx), **total}
    if group_by == "month":
        items = []
        for wf, wt in windows:
            row = await _summary_window(
                session, ws_id, ctx.user_id, wf, wt, account_ids=accs, accounting_mode=accounting_mode
            )
            items.append({"month": wf.strftime("%Y-%m"), **row})
        out["items"] = items
    out["notes"] = _SUMMARY_NOTES
    return out


# --- money map -------------------------------------------------------------------

def _fold_lane(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Largest first; everything past the cap folded into one 'Other'."""
    items = sorted(items, key=lambda c: c["value"], reverse=True)
    if len(items) <= _MAX_LANE_ITEMS:
        return items
    top, rest = items[:_MAX_LANE_ITEMS], items[_MAX_LANE_ITEMS:]
    return top + [{"key": "other", "label": "Other", "value": _round(sum(c["value"] for c in rest))}]


@tool(
    name="get_money_map",
    description=(
        "Where the money went over a rolling window ending today — the Reports → "
        "Money Map lanes: income, direct_contributions (payroll 401(k)/employer match "
        "that never passed through cash), transfers_in, expenses, investments (money "
        "moved from cash into investments plus the direct contributions), "
        "transfers_out (money that left to accounts Securo cannot see). Use for "
        "'where did my money go' and for the contribution-aware savings rate. Pass "
        "either days (7-730) or months (1-24)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {"type": "integer", "minimum": 7, "maximum": 730, "description": "Rolling window ending today"},
            "months": {"type": "integer", "minimum": 1, "maximum": 24, "description": "Month-aligned window ending today"},
        },
        "additionalProperties": False,
    },
    tags=["read", "finance", "reports"],
)
async def get_money_map(
    *,
    session: AsyncSession,
    ctx: CallContext,
    days: int | None = None,
    months: int | None = None,
) -> dict[str, Any]:
    if days is not None and months is not None:
        return {"error": "pass either days or months, not both"}
    if days is None and months is None:
        days = 30
    if days is not None:
        days = max(7, min(int(days), 730))
        months_arg = 1
    else:
        months_arg = max(1, min(int(months or 1), 24))

    ws_id = await resolve_workspace_id(session, ctx)
    currency = await _primary_currency(session, ctx)
    rep = await report_service.get_income_expenses_report(
        session, ws_id, ctx.user_id, months=months_arg, interval="monthly", currency=currency, days=days
    )

    raw: dict[str, list[dict[str, Any]]] = {lane: [] for lane in MONEY_MAP_LANES}
    for c in rep.composition:
        if c.group in raw and c.value > 0:
            raw[c.group].append({"key": c.key, "label": c.label, "value": _round(c.value)})

    lanes = {
        lane: {"items": _fold_lane(items), "total": _round(sum(c["value"] for c in items))}
        for lane, items in raw.items()
    }
    income = lanes["income"]["total"]
    contributions = lanes["direct_contributions"]["total"]
    transfers_in = lanes["transfers_in"]["total"]
    expenses = lanes["expenses"]["total"]
    investments = lanes["investments"]["total"]
    transfers_out = lanes["transfers_out"]["total"]
    # Same arithmetic as buildMoneyMap: contributions sit in both inflow and
    # investments, so they cancel and never move the surplus.
    net = _round(income + contributions + transfers_in - expenses - investments - transfers_out)
    cash_rate = round((income - expenses) / income, 4) if income > 0 else None
    contribution_aware = (
        round((income - expenses + contributions) / (income + contributions), 4)
        if income + contributions > 0
        else None
    )

    today = date.today()
    start = _report_start_date(today, months_arg, None, days)
    return {
        "window": {
            "from_date": start.isoformat(),
            "to_date": today.isoformat(),
            **({"days": days} if days is not None else {"months": months_arg}),
        },
        "currency": rep.meta.currency,
        "lanes": lanes,
        "totals": {
            "income": income,
            "direct_contributions": contributions,
            "transfers_in": transfers_in,
            "expenses": expenses,
            "investments": investments,
            "transfers_out": transfers_out,
            "net": net,
            "cash_savings_rate": cash_rate,
            "contribution_aware_savings_rate": contribution_aware,
        },
        "notes": (
            "net = income + direct_contributions + transfers_in - expenses - investments - "
            "transfers_out (a negative net is the deficit). cash_savings_rate = (income - "
            "expenses) / income. contribution_aware_savings_rate = (income - expenses + "
            "direct_contributions) / (income + direct_contributions) and is the one to quote "
            "when payroll contributions exist. investments already includes the direct "
            "contributions once."
        ),
    }


# --- uncategorized merchants -----------------------------------------------------

@tool(
    name="list_uncategorized_merchants",
    description=(
        "Uncategorized transactions grouped by merchant (noise such as card "
        "references, dates and ids stripped), largest total first, with a "
        "pattern_suggestion that a description-contains rule can use as-is. Use "
        "this to plan categorization: pair it with list_categories and list_rules, "
        "then propose one propose_create_payee_rule per merchant you are sure about."
    ),
    parameters={
        "type": "object",
        "properties": {
            "from_date": {"type": "string", "format": "date"},
            "to_date": {"type": "string", "format": "date"},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_MERCHANTS, "default": 50},
            "min_count": {"type": "integer", "minimum": 1, "default": 1, "description": "Only merchants seen at least this many times"},
            "include_pending": {"type": "boolean", "default": False, "description": "Also count pending (not yet posted) rows"},
        },
        "additionalProperties": False,
    },
    tags=["read", "finance", "categorization"],
)
async def list_uncategorized_merchants(
    *,
    session: AsyncSession,
    ctx: CallContext,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
    min_count: int = 1,
    include_pending: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), _MAX_MERCHANTS))
    min_count = max(1, int(min_count))
    ws_id = await resolve_workspace_id(session, ctx)
    fd = parse_date(from_date)
    td = parse_date(to_date)

    amount_col = func.coalesce(Transaction.amount_primary, Transaction.amount)
    q = (
        select(
            Transaction.id,
            Transaction.description,
            Transaction.date,
            Transaction.type,
            amount_col.label("amount"),
            Account.name.label("account_name"),
        )
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.workspace_id == ws_id,
            Transaction.category_id.is_(None),
            Transaction.is_ignored.is_(False),
            Transaction.transfer_pair_id.is_(None),
            Transaction.source != "opening_balance",
            # Split lines carry the money; their parent has no category by design.
            Transaction.parent_transaction_id.is_(None),
            ~is_split_parent(),
        )
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        .limit(_MAX_MERCHANT_ROWS + 1)
    )
    if not include_pending:
        q = q.where(Transaction.status == "posted")
    if fd:
        q = q.where(Transaction.date >= fd)
    if td:
        q = q.where(Transaction.date <= td)

    rows = (await session.execute(q)).mappings().all()
    truncated = len(rows) > _MAX_MERCHANT_ROWS
    rows = rows[:_MAX_MERCHANT_ROWS]

    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = merchant_key(r["description"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "merchant": key,
                "pattern_suggestion": suggest_pattern(r["description"]),
                "count": 0,
                "total": 0.0,
                "first_date": r["date"],
                "last_date": r["date"],
                "type_mix": Counter(),
                "accounts": set(),
                "sample_transaction_ids": [],
                "sample_descriptions": [],
            }
        g["count"] += 1
        g["total"] += abs(num(r["amount"]) or 0.0)
        g["first_date"] = min(g["first_date"], r["date"])
        g["last_date"] = max(g["last_date"], r["date"])
        g["type_mix"][r["type"]] += 1
        g["accounts"].add(r["account_name"])
        if len(g["sample_transaction_ids"]) < 3:
            g["sample_transaction_ids"].append(str(r["id"]))
        if len(g["sample_descriptions"]) < 2 and r["description"] not in g["sample_descriptions"]:
            g["sample_descriptions"].append(r["description"])

    items = [
        {
            **g,
            "total": _round(g["total"]),
            "first_date": g["first_date"].isoformat(),
            "last_date": g["last_date"].isoformat(),
            "type_mix": dict(g["type_mix"]),
            "accounts": sorted(g["accounts"]),
        }
        for g in groups.values()
        if g["count"] >= min_count
    ]
    items.sort(key=lambda g: (g["total"], g["count"]), reverse=True)
    return {
        "from_date": fd.isoformat() if fd else None,
        "to_date": td.isoformat() if td else None,
        "total_uncategorized_count": len(rows),
        "merchant_count": len(groups),
        "items": items[:limit],
        "truncated": truncated,
        "notes": (
            "pattern_suggestion is the stable prefix of the raw description in upper case; "
            "rules match case- and accent-insensitively, so it works verbatim as the value "
            "of a description 'contains' condition."
        ),
    }


# --- rules -----------------------------------------------------------------------

def _first_description_pattern(conditions: Any) -> Optional[str]:
    """The first `description` leaf condition of a rule, rendered as the bare
    value for `contains` and `op:value` otherwise; nested groups are walked."""
    for c in conditions or []:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("conditions"), list):
            found = _first_description_pattern(c["conditions"])
            if found is not None:
                return found
            continue
        if c.get("field") == "description":
            value = c.get("value")
            return str(value) if c.get("op") == "contains" else f"{c.get('op')}:{value}"
    return None


@tool(
    name="list_rules",
    description=(
        "The workspace's categorization rules with category names resolved, in the "
        "order they run (ascending priority; the first rule that sets a category wins). "
        "Compact by default: name, priority, category_name and the description pattern "
        "each rule matches on. Pass verbose=true only when you need the full conditions "
        "and actions. Read this before proposing rules so new ones use existing "
        "categories, do not duplicate a pattern, and take "
        "suggested_priority_for_new_merchant_rule."
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_inactive": {"type": "boolean", "default": False},
            "verbose": {
                "type": "boolean",
                "default": False,
                "description": "Full conditions and actions per rule; the default is the compact form",
            },
        },
        "additionalProperties": False,
    },
    tags=["read", "finance", "rules"],
)
async def list_rules(
    *,
    session: AsyncSession,
    ctx: CallContext,
    include_inactive: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    rules = await rule_service.get_rules(session, ws_id)
    if not include_inactive:
        rules = [r for r in rules if r.is_active]
    truncated = len(rules) > _MAX_RULES
    rules = rules[:_MAX_RULES]

    names = {
        str(cid): cname
        for cid, cname in (
            await session.execute(select(Category.id, Category.name).where(Category.workspace_id == ws_id))
        ).all()
    }

    def _action(a: Any) -> Any:
        if isinstance(a, dict) and a.get("op") == "set_category":
            return {**a, "category_name": names.get(str(a.get("value")))}
        return a

    def _category_name(r: Any) -> Optional[str]:
        for a in r.actions or []:
            if isinstance(a, dict) and a.get("op") == "set_category":
                return names.get(str(a.get("value")))
        return None

    if verbose:
        items = [
            {
                "id": str(r.id),
                "name": r.name,
                "priority": int(r.priority),
                "is_active": bool(r.is_active),
                "conditions_op": r.conditions_op,
                "conditions": r.conditions or [],
                "actions": [_action(a) for a in (r.actions or [])],
            }
            for r in rules
        ]
    else:
        items = []
        for r in rules:
            item: dict[str, Any] = {
                "name": r.name,
                "priority": int(r.priority),
                "category_name": _category_name(r),
                "pattern": _first_description_pattern(r.conditions),
            }
            if include_inactive:
                item["is_active"] = bool(r.is_active)
            items.append(item)
    active_priorities = [int(r.priority) for r in rules if r.is_active]
    counts = Counter(active_priorities)
    most_common = counts.most_common(1)[0][0] if counts else None
    stats = {
        "min": min(active_priorities) if active_priorities else None,
        "max": max(active_priorities) if active_priorities else None,
        "most_common": most_common,
    }
    return {
        "items": items,
        "total": len(items),
        "truncated": truncated,
        "priority_stats": stats,
        "suggested_priority_for_new_merchant_rule": most_common if most_common is not None else 10,
        "notes": (
            "Rules run in ascending priority and a rule never overrides a category another "
            "rule already set, so account-scoped rules sit at low numbers and broad merchant "
            "fallbacks at high ones."
        ),
    }


# --- holdings --------------------------------------------------------------------

async def _asset_item(session: AsyncSession, asset: Asset) -> dict[str, Any]:
    latest = await asset_service._get_latest_value(session, asset.id)
    value = asset_service._compute_current_value(asset, latest)
    cost = num(asset.purchase_price)
    return {
        "id": str(asset.id),
        "name": asset.name,
        "type": asset.type,
        "ticker": asset.ticker,
        "currency": asset.currency,
        "units": num(asset.units),
        "average_price": num(asset.average_price),
        "last_price": num(asset.last_price),
        "current_value": _round(value) if value is not None else None,
        "cost_basis": _round(cost) if cost is not None else None,
        "gain_loss": _round(value - cost) if value is not None and cost is not None else None,
        "realized_gain": num(asset.realized_gain),
        "linked_account_id": asset_service.linked_account_id(asset),
        "is_archived": bool(asset.is_archived),
    }


async def _investment_positions(
    session: AsyncSession,
    ws_id: uuid.UUID,
    primary: str,
    *,
    account_id: Optional[uuid.UUID] = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """Investment accounts with their linked holdings, plus unlinked holdings.

    Shared by `get_holdings` and `fire_projection` so both value the portfolio
    the same way as the net worth report: a holding linked to an account is
    already inside that account's balance and is never added on top.
    """
    accounts = await account_service.get_accounts(session, ws_id)
    primary_map = {
        str(aid): bp
        for aid, bp in (
            await session.execute(select(Account.id, Account.balance_primary).where(Account.workspace_id == ws_id))
        ).all()
    }
    buckets: dict[str, dict[str, Any]] = {}
    for a in accounts:
        aid = str(a["id"])
        if account_id is not None:
            if aid != str(account_id):
                continue
        elif a.get("type") != "investment":
            continue
        balance = num(a.get("current_balance")) or 0.0
        bp = primary_map.get(aid)
        buckets[aid] = {
            "account_id": aid,
            "name": a.get("display_name") or a.get("name"),
            "type": a.get("type"),
            "currency": a.get("currency"),
            "balance": _round(balance),
            "balance_primary": _round(balance) if a.get("currency") == primary else (num(bp) if bp is not None else None),
            "holdings": [],
        }

    q = select(Asset).where(Asset.workspace_id == ws_id, Asset.sell_date.is_(None))
    if not include_archived:
        q = q.where(Asset.is_archived.is_(False))
    assets = (await session.execute(q.order_by(Asset.position, Asset.name))).scalars().all()

    unlinked: list[dict[str, Any]] = []
    for asset in assets:
        item = await _asset_item(session, asset)
        linked = item["linked_account_id"]
        if linked is not None and linked in buckets:
            buckets[linked]["holdings"].append(item)
        elif linked is None and account_id is None:
            unlinked.append(item)

    accounts_total = 0.0
    unconverted: list[dict[str, Any]] = []
    for b in buckets.values():
        if b["balance_primary"] is not None:
            accounts_total += b["balance_primary"]
        else:
            unconverted.append({"account_id": b["account_id"], "currency": b["currency"], "balance": b["balance"]})
    by_currency: dict[str, float] = defaultdict(float)
    for item in unlinked:
        if item["current_value"] is not None:
            by_currency[item["currency"]] += item["current_value"]

    return {
        "accounts": list(buckets.values()),
        "unlinked_holdings": unlinked,
        "investment_accounts_total_primary": _round(accounts_total),
        "unlinked_holdings_total_by_currency": {k: _round(v) for k, v in by_currency.items()},
        "unconverted_accounts": unconverted,
    }


@tool(
    name="get_holdings",
    description=(
        "Investment accounts with their balances and the holdings inside them, "
        "plus holdings tracked on their own (not linked to an account). Use for "
        "'what do I hold', 'how much is invested', per-account positions and "
        "cost basis. A holding linked to an account is already inside that "
        "account's balance — never add the two together."
    ),
    parameters={
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "format": "uuid", "description": "Only this account (any type)"},
            "include_archived": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    tags=["read", "finance", "assets"],
)
async def get_holdings(
    *,
    session: AsyncSession,
    ctx: CallContext,
    account_id: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    primary = await _primary_currency(session, ctx)
    positions = await _investment_positions(
        session, ws_id, primary, account_id=parse_uuid(account_id), include_archived=include_archived
    )
    return {
        "currency": primary,
        **positions,
        "double_count_note": (
            "Holdings listed under an account are already included in that account's "
            "balance; investment_accounts_total_primary counts each account once and "
            "unlinked_holdings_total_by_currency is additional, per native currency."
        ),
    }


# --- FIRE projection ---------------------------------------------------------------

@tool(
    name="fire_projection",
    description=(
        "Financial-independence math: FI number (annual spend / withdrawal rate), gap, "
        "progress, years to FI and a year-by-year trajectory. Inputs you omit are "
        "derived from Securo data — annual_spend and annual_contribution from the "
        "trailing 365 days (the expense and invested figures of get_transactions_summary), "
        "invested_assets from investment-account balances plus holdings not linked to an "
        "account — and echoed under `derived` so you can state where each number came "
        "from. real_return is after inflation; spending stays in today's money."
    ),
    parameters={
        "type": "object",
        "properties": {
            "annual_spend": {"type": "number", "exclusiveMinimum": 0, "description": "Yearly spending in the primary currency"},
            "invested_assets": {"type": "number", "minimum": 0, "description": "Portfolio today, primary currency"},
            "annual_contribution": {"type": "number", "minimum": 0, "description": "New money invested per year"},
            "real_return": {"type": "number", "minimum": -0.1, "maximum": 0.2, "default": 0.05, "description": "Expected yearly return after inflation"},
            "withdrawal_rate": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.2, "default": 0.04},
        },
        "additionalProperties": False,
    },
    tags=["read", "finance", "projection"],
)
async def fire_projection(
    *,
    session: AsyncSession,
    ctx: CallContext,
    annual_spend: float | None = None,
    invested_assets: float | None = None,
    annual_contribution: float | None = None,
    real_return: float = 0.05,
    withdrawal_rate: float = 0.04,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    primary = await _primary_currency(session, ctx)
    derived: dict[str, Any] = {}

    if annual_spend is None or annual_contribution is None:
        today = date.today()
        window = await _summary_window(session, ws_id, ctx.user_id, today - timedelta(days=364), today)
        span = f"{window['from_date']} to {window['to_date']}"
        if annual_spend is None:
            annual_spend = window["expense"]
            derived["annual_spend"] = {"value": annual_spend, "source": f"expense, trailing 365 days ({span})"}
        if annual_contribution is None:
            annual_contribution = window["invested"]
            derived["annual_contribution"] = {
                "value": annual_contribution,
                "source": f"invested, trailing 365 days ({span})",
            }

    if invested_assets is None:
        positions = await _investment_positions(session, ws_id, primary)
        by_currency = positions["unlinked_holdings_total_by_currency"]
        invested_assets = _round(positions["investment_accounts_total_primary"] + by_currency.get(primary, 0.0))
        derived["invested_assets"] = {
            "value": invested_assets,
            "source": "investment account balances plus unlinked holdings, primary currency only",
            "unconverted": {
                "holdings_by_currency": {k: v for k, v in by_currency.items() if k != primary},
                "accounts": positions["unconverted_accounts"],
            },
        }

    try:
        inputs = FireInputs(
            annual_spend=float(annual_spend),
            invested_assets=float(invested_assets),
            annual_contribution=float(annual_contribution),
            real_return=float(real_return),
            withdrawal_rate=float(withdrawal_rate),
        )
        result = fire_service.project(inputs)
    except (TypeError, ValueError) as exc:
        return {"error": str(exc), "derived": derived}

    return {
        "currency": primary,
        "inputs": asdict(inputs),
        "derived": derived,
        **result.to_dict(),
        "notes": (
            "Deterministic arithmetic, not advice: fi_number = annual_spend / withdrawal_rate; "
            "years_to_fi assumes the same contribution and the same real return every year "
            "(None = not reached within max_years). State the inputs and their sources when "
            "quoting it."
        ),
    }
