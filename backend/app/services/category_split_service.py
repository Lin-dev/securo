"""Splitting one transaction into category lines.

A split materializes one child row per line under the original (the
"parent") and hides the parent with ``is_ignored=True``. Children are
ordinary rows that copy the parent's account, date and description, so
balances, budgets, reports and the transactions summary count the lines
with no special handling, and the parent contributes nothing anywhere money
is summed.

Children carry ``source="split"`` and no ``external_id``. That keeps the
sync's fuzzy matcher (which only claims manual rows) and the recurring
matcher (which has its own source allowlist) away from them. The parent
keeps its provider identity, so a re-sync matches it by ``external_id`` and
leaves it frozen like any other ignored row.
"""

import logging
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category
from app.models.rule import Rule
from app.models.transaction import Transaction
from app.schemas.transaction import CategorySplitLineInput
from app.services._query_filters import is_split_parent
from app.services.category_service import get_hidden_category_ids
from app.services.rule_engine import evaluate_conditions

logger = logging.getLogger(__name__)

SPLIT_SOURCE = "split"
SPLIT_RULE_OP = "split_categories"
_UNSPLITTABLE_SOURCES = ("opening_balance", "settlement", SPLIT_SOURCE)
_CENT = Decimal("0.01")


def _quantize(amount: Decimal) -> Decimal:
    return Decimal(str(amount)).quantize(_CENT, rounding=ROUND_HALF_UP)


async def _load(
    session: AsyncSession, transaction_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[Transaction]:
    result = await session.execute(
        select(Transaction)
        .where(
            Transaction.id == transaction_id,
            Transaction.workspace_id == workspace_id,
        )
        .options(
            selectinload(Transaction.split_children),
            selectinload(Transaction.splits),
        )
        # The caller may already hold this row (a sync just created it, a
        # test built it); refresh it so both collections are loaded and no
        # later access lazy-loads under the async session.
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def assert_splittable(tx: Transaction) -> None:
    """Raise ValueError unless `tx` may become a split parent.

    Requires `split_children` and `splits` to be loaded on `tx`.
    """
    if tx.parent_transaction_id is not None:
        raise ValueError("A split line cannot be split again")
    if tx.status != "posted":
        raise ValueError("Only posted transactions can be split")
    if tx.transfer_pair_id is not None:
        raise ValueError("Transfers cannot be split; unlink the transfer first")
    if tx.source in _UNSPLITTABLE_SOURCES:
        raise ValueError("Opening balance and settlement transactions cannot be split")
    if tx.installment_series_id is not None or tx.installment_number is not None:
        raise ValueError("Installment transactions cannot be split")
    if tx.splits:
        raise ValueError("Remove the group split before splitting by category")


def materialize_lines(
    parent_amount: Decimal,
    parent_amount_primary: Optional[Decimal],
    lines: Sequence[CategorySplitLineInput],
) -> list[tuple[CategorySplitLineInput, Decimal, Optional[Decimal]]]:
    """Resolve lines into ``(line, amount, amount_primary)`` with exact sums.

    Amounts are quantized to cents and must add up to the parent's absolute
    amount. The primary-currency amount is shared out in proportion, with the
    last line absorbing the rounding residual so the lines sum exactly to the
    parent's own stamp. Signs follow the parent: amounts are stored positive
    today, and copying the sign keeps that an implementation detail.
    """
    if len(lines) < 2:
        raise ValueError("At least two split lines are required")
    total = _quantize(parent_amount).copy_abs()
    amounts = [_quantize(line.amount) for line in lines]
    if any(amount <= 0 for amount in amounts):
        raise ValueError("Split lines must be greater than zero")
    if sum(amounts, Decimal("0")) != total:
        raise ValueError("Split lines must sum to the transaction amount")

    primaries: list[Optional[Decimal]]
    if parent_amount_primary is None:
        primaries = [None] * len(lines)
    else:
        total_primary = _quantize(parent_amount_primary).copy_abs()
        shares = [_quantize(total_primary * amount / total) for amount in amounts[:-1]]
        shares.append(total_primary - sum(shares, Decimal("0")))
        primaries = list(shares)

    negative = Decimal(str(parent_amount)) < 0
    negative_primary = (
        parent_amount_primary is not None and Decimal(str(parent_amount_primary)) < 0
    )
    resolved = []
    for line, amount, primary in zip(lines, amounts, primaries):
        signed = -amount if negative else amount
        signed_primary = (
            None if primary is None else (-primary if negative_primary else primary)
        )
        resolved.append((line, signed, signed_primary))
    return resolved


async def ensure_categories_in_workspace(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    category_ids: Iterable[Optional[uuid.UUID]],
) -> None:
    wanted = {category_id for category_id in category_ids if category_id is not None}
    if not wanted:
        return
    result = await session.execute(
        select(Category.id).where(
            Category.id.in_(wanted),
            Category.workspace_id == workspace_id,
        )
    )
    if {row[0] for row in result.all()} != wanted:
        raise ValueError("Category not found")


def _child_from(
    tx: Transaction,
    line: CategorySplitLineInput,
    amount: Decimal,
    amount_primary: Optional[Decimal],
) -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        user_id=tx.user_id,
        workspace_id=tx.workspace_id,
        account_id=tx.account_id,
        category_id=line.category_id,
        description=tx.description,
        original_description=tx.original_description,
        amount=amount,
        currency=tx.currency,
        date=tx.date,
        effective_date=tx.effective_date,
        type=tx.type,
        source=SPLIT_SOURCE,
        status="posted",
        payee=tx.payee,
        payee_id=tx.payee_id,
        notes=line.notes,
        amount_primary=amount_primary,
        fx_rate_used=tx.fx_rate_used if amount_primary is not None else None,
        effective_bill_date=tx.effective_bill_date,
        bill_id=tx.bill_id,
        exclude_from_pnl=tx.exclude_from_pnl,
        is_ignored=False,
        parent_transaction_id=tx.id,
    )


async def _materialize(
    session: AsyncSession,
    tx: Transaction,
    lines: Sequence[CategorySplitLineInput],
) -> list[Transaction]:
    """Replace `tx`'s lines with `lines` and hide `tx`. Flushes, never commits.

    Shared by the API and the rule path. Requires `split_children` loaded on
    `tx`: assigning the collection lets the delete-orphan cascade remove any
    previous lines in the same flush.
    """
    resolved = materialize_lines(tx.amount, tx.amount_primary, lines)
    children = [_child_from(tx, line, amount, primary) for line, amount, primary in resolved]
    tx.split_children = children
    tx.is_ignored = True
    await session.flush()
    return children


async def split_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    lines: Sequence[CategorySplitLineInput],
    *,
    commit: bool = True,
) -> Optional[tuple[Transaction, list[Transaction]]]:
    """Split a transaction into category lines, replacing any existing lines.

    Returns ``None`` when the transaction is not in the workspace; raises
    ``ValueError`` for anything the caller should report as a bad request.
    """
    tx = await _load(session, transaction_id, workspace_id)
    if tx is None:
        return None
    assert_splittable(tx)
    await ensure_categories_in_workspace(
        session, workspace_id, (line.category_id for line in lines)
    )
    children = await _materialize(session, tx, lines)
    if commit:
        await session.commit()
        await session.refresh(tx, ["split_children"])
    return tx, children


async def unsplit_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
    *,
    commit: bool = True,
) -> Optional[Transaction]:
    """Delete a parent's lines and bring the parent back into the totals."""
    tx = await _load(session, transaction_id, workspace_id)
    if tx is None:
        return None
    if not tx.split_children:
        raise ValueError("Transaction is not split")
    tx.split_children = []
    tx.is_ignored = False
    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(tx, ["split_children"])
    return tx


async def child_counts(
    session: AsyncSession, tx_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Number of lines under each id in `tx_ids`, for ids that have any."""
    if not tx_ids:
        return {}
    result = await session.execute(
        select(Transaction.parent_transaction_id, func.count(Transaction.id))
        .where(Transaction.parent_transaction_id.in_(tx_ids))
        .group_by(Transaction.parent_transaction_id)
    )
    return {row[0]: row[1] for row in result.all()}


# ─── Rule-driven splits ───────────────────────────────────────────────────
#
# A `split_categories` rule action carries its lines as a list:
#   [{"category_id": "...", "amount": "250.00"},
#    {"category_id": "...", "percent": 50},
#    {"category_id": "...", "remainder": true}]
# Each line takes exactly one of amount / percent / remainder; at most one
# line takes the remainder. The pure rule engine ignores the op — it cannot
# create rows — and `apply_split_rules` materializes it afterwards, once the
# parent row is complete (posted, dated, bill-linked).


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Each split line needs an amount, a percent or remainder")


def validate_rule_lines(value: Any) -> list[dict]:
    """Normalize a split action's value or raise ValueError.

    Returns one dict per line with `category_id` (UUID), `amount` (Decimal or
    None), `percent` (Decimal or None) and `remainder` (bool). Category
    existence is the caller's check; it needs the workspace.
    """
    if not isinstance(value, list) or len(value) < 2:
        raise ValueError("Split action needs at least two lines")
    lines: list[dict] = []
    remainders = 0
    fixed = 0
    percent_total = Decimal("0")
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Each split line needs an amount, a percent or remainder")
        try:
            category_id = uuid.UUID(str(raw.get("category_id")))
        except (TypeError, ValueError):
            raise ValueError("Category not found")
        amount = raw.get("amount")
        percent = raw.get("percent")
        remainder = bool(raw.get("remainder"))
        if (amount is not None) + (percent is not None) + remainder != 1:
            raise ValueError("Each split line needs an amount, a percent or remainder")
        line = {"category_id": category_id, "amount": None, "percent": None, "remainder": remainder}
        if amount is not None:
            amount = _quantize(_decimal(amount))
            if amount <= 0:
                raise ValueError("Split line amounts must be greater than zero")
            line["amount"] = amount
            fixed += 1
        if percent is not None:
            percent = _decimal(percent)
            if not (Decimal("0") < percent <= Decimal("100")):
                raise ValueError("Split line percents must be between 0 and 100")
            line["percent"] = percent
            percent_total += percent
        remainders += remainder
        lines.append(line)
    if remainders > 1:
        raise ValueError("Only one split line can take the remainder")
    if percent_total > Decimal("100"):
        raise ValueError("Split percents add up to more than 100")
    if remainders == 0 and fixed == 0 and percent_total != Decimal("100"):
        raise ValueError("Split percents must add up to 100")
    return lines


def resolve_rule_lines(
    lines: Sequence[dict], total: Decimal, hidden_category_ids: Iterable[uuid.UUID] = ()
) -> Optional[list[CategorySplitLineInput]]:
    """Turn validated rule lines into concrete amounts for one transaction.

    Fixed amounts are taken as written, percents are taken of the absolute
    total, and the remainder line absorbs what is left. Returns None when the
    lines do not fit the transaction (fixed amounts that do not add up, a
    line that would be zero or negative) or point at a hidden category; the
    caller leaves such a transaction unsplit.
    """
    hidden = set(hidden_category_ids)
    if any(line["category_id"] in hidden for line in lines):
        return None
    total = _quantize(total).copy_abs()
    amounts: list[Optional[Decimal]] = []
    for line in lines:
        if line["amount"] is not None:
            amounts.append(line["amount"])
        elif line["percent"] is not None:
            amounts.append(_quantize(total * line["percent"] / Decimal("100")))
        else:
            amounts.append(None)
    allocated = sum((a for a in amounts if a is not None), Decimal("0"))
    remainder_slots = [i for i, a in enumerate(amounts) if a is None]
    if remainder_slots:
        amounts[remainder_slots[0]] = total - allocated
    elif allocated != total:
        # Percent lines round; the last percent line takes the cents so the
        # lines add up. Fixed-only lines that miss the total do not fit.
        percent_slots = [i for i, line in enumerate(lines) if line["percent"] is not None]
        residual = total - allocated
        if not percent_slots or residual.copy_abs() > _CENT * len(lines):
            return None
        amounts[percent_slots[-1]] = (amounts[percent_slots[-1]] or Decimal("0")) + residual
    if any(a is None or a <= 0 for a in amounts):
        return None
    return [
        CategorySplitLineInput(category_id=line["category_id"], amount=amount)
        for line, amount in zip(lines, amounts)
    ]


def _eligibility_filters(workspace_id: uuid.UUID) -> list:
    """SQL twin of `assert_splittable`, minus the group-split check."""
    return [
        Transaction.workspace_id == workspace_id,
        Transaction.status == "posted",
        Transaction.parent_transaction_id.is_(None),
        ~is_split_parent(),
        Transaction.transfer_pair_id.is_(None),
        Transaction.source.not_in(_UNSPLITTABLE_SOURCES),
        Transaction.installment_series_id.is_(None),
        Transaction.installment_number.is_(None),
    ]


async def splittable_ids(session: AsyncSession, workspace_id: uuid.UUID) -> set[uuid.UUID]:
    """Ids a split rule could act on right now; for the rule editor's preview."""
    result = await session.execute(
        select(Transaction.id).where(*_eligibility_filters(workspace_id))
    )
    return {row[0] for row in result.all()}


def _condition_target(tx: Transaction, description: str) -> SimpleNamespace:
    """The fields conditions may read, with a swapped-in description."""
    return SimpleNamespace(
        description=description,
        payee=tx.payee,
        notes=tx.notes,
        amount=tx.amount,
        type=tx.type,
        account_id=tx.account_id,
        payee_id=tx.payee_id,
        date=tx.date,
    )


def _rule_matches(rule: Rule, tx: Transaction) -> bool:
    conditions = rule.conditions or []
    if evaluate_conditions(rule.conditions_op, conditions, tx):
        return True
    # A rule that renamed the row earlier should still recognise it, the same
    # way `apply_single_rule` retries against the imported text.
    if tx.original_description is not None and tx.original_description != tx.description:
        return evaluate_conditions(
            rule.conditions_op, conditions, _condition_target(tx, tx.original_description)
        )
    return False


async def apply_split_rules(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    transaction_ids: Optional[Sequence[uuid.UUID]] = None,
    account_ids: Optional[Sequence[uuid.UUID]] = None,
    since: Optional[date] = None,
) -> int:
    """Split every eligible unsplit row the first matching split rule fits.

    Runs after sync, import, manual creation and rule application, on rows
    that are complete. Never touches an existing split, so editing a rule
    only affects rows that have not been split yet. Flushes, never commits.
    Returns the number of rows split.
    """
    rules_result = await session.execute(
        select(Rule)
        .where(Rule.workspace_id == workspace_id, Rule.is_active.is_(True))
        .order_by(Rule.priority, Rule.id)
    )
    prepared: list[tuple[Rule, list[dict]]] = []
    for rule in rules_result.scalars().all():
        action = next(
            (a for a in (rule.actions or []) if isinstance(a, dict) and a.get("op") == SPLIT_RULE_OP),
            None,
        )
        if action is None:
            continue
        try:
            prepared.append((rule, validate_rule_lines(action.get("value"))))
        except ValueError as exc:
            logger.warning("Split rule %s has an invalid action and is skipped: %s", rule.id, exc)
    if not prepared:
        return 0

    query = (
        select(Transaction)
        .where(*_eligibility_filters(workspace_id))
        .options(
            selectinload(Transaction.split_children),
            selectinload(Transaction.splits),
        )
        .execution_options(populate_existing=True)
    )
    if transaction_ids is not None:
        if not transaction_ids:
            return 0
        query = query.where(Transaction.id.in_(list(transaction_ids)))
    if account_ids is not None:
        if not account_ids:
            return 0
        query = query.where(Transaction.account_id.in_(list(account_ids)))
    if since is not None:
        query = query.where(Transaction.date >= since)
    candidates = list((await session.execute(query)).scalars().all())
    if not candidates:
        return 0

    hidden = await get_hidden_category_ids(session, workspace_id)
    count = 0
    for tx in candidates:
        if tx.splits:
            # Same rule as the API: a group-shared row is not split by category.
            continue
        for rule, lines in prepared:
            if not _rule_matches(rule, tx):
                continue
            resolved = resolve_rule_lines(lines, tx.amount, hidden)
            if resolved is None:
                logger.info(
                    "Split rule %s matched transaction %s but its lines do not fit", rule.id, tx.id
                )
                continue
            await _materialize(session, tx, resolved)
            count += 1
            break
    return count
