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

import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category
from app.models.transaction import Transaction
from app.schemas.transaction import CategorySplitLineInput

SPLIT_SOURCE = "split"
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


async def _ensure_categories_in_workspace(
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
    await _ensure_categories_in_workspace(
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
