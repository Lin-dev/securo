"""Pure and near-pure pieces of category_split_service."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction
from app.schemas.transaction import CategorySplitLineInput
from app.services.category_split_service import (
    assert_splittable,
    child_counts,
    materialize_lines,
    split_transaction,
)


def _lines(*amounts: str) -> list[CategorySplitLineInput]:
    return [CategorySplitLineInput(category_id=uuid.uuid4(), amount=Decimal(a)) for a in amounts]


def _tx(**overrides) -> Transaction:
    fields = dict(
        id=uuid.uuid4(), user_id=uuid.uuid4(), account_id=uuid.uuid4(),
        description="x", amount=Decimal("500.00"), currency="USD", date=date.today(),
        type="debit", source="manual", status="posted", parent_transaction_id=None,
        transfer_pair_id=None, installment_series_id=None, installment_number=None,
    )
    fields.update(overrides)
    tx = Transaction(**fields)
    tx.splits = []
    tx.split_children = []
    return tx


def test_materialize_lines_requires_exact_sum_and_two_lines():
    with pytest.raises(ValueError, match="At least two"):
        materialize_lines(Decimal("500"), None, _lines("500"))
    with pytest.raises(ValueError, match="sum to the transaction amount"):
        materialize_lines(Decimal("500"), None, _lines("250", "249.99"))
    with pytest.raises(ValueError, match="greater than zero"):
        materialize_lines(Decimal("500"), None, _lines("500", "0.001"))


def test_materialize_lines_shares_primary_amount_with_residual_on_last_line():
    resolved = materialize_lines(Decimal("100"), Decimal("33.33"), _lines("33.33", "33.33", "33.34"))
    primaries = [p for _, _, p in resolved]
    assert primaries == [Decimal("11.11"), Decimal("11.11"), Decimal("11.11")]
    assert sum(primaries) == Decimal("33.33")

    resolved = materialize_lines(Decimal("500"), Decimal("2500.00"), _lines("250", "250"))
    assert [a for _, a, _ in resolved] == [Decimal("250.00"), Decimal("250.00")]
    assert [p for _, _, p in resolved] == [Decimal("1250.00"), Decimal("1250.00")]


def test_materialize_lines_keeps_the_parent_sign_and_missing_primary():
    resolved = materialize_lines(Decimal("-500"), None, _lines("200", "300"))
    assert [a for _, a, _ in resolved] == [Decimal("-200.00"), Decimal("-300.00")]
    assert [p for _, _, p in resolved] == [None, None]
    resolved = materialize_lines(Decimal("500"), Decimal("-500"), _lines("200", "300"))
    assert [p for _, _, p in resolved] == [Decimal("-200.00"), Decimal("-300.00")]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"parent_transaction_id": uuid.uuid4()}, "split again"),
        ({"status": "pending"}, "posted"),
        ({"transfer_pair_id": uuid.uuid4()}, "Transfers"),
        ({"source": "opening_balance"}, "Opening balance"),
        ({"source": "settlement"}, "Opening balance and settlement"),
        ({"installment_series_id": uuid.uuid4()}, "Installment"),
        ({"installment_number": 2}, "Installment"),
    ],
)
def test_assert_splittable_matrix(overrides, message):
    with pytest.raises(ValueError, match=message):
        assert_splittable(_tx(**overrides))


def test_assert_splittable_accepts_a_plain_posted_row():
    assert_splittable(_tx())
    assert_splittable(_tx(source="sync"))


@pytest.mark.asyncio
async def test_child_counts_only_reports_parents(session: AsyncSession, test_user, test_workspace):
    account = Account(id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
                      name="A", type="checking", balance=Decimal("0"), currency="USD")
    session.add(account)
    await session.flush()
    parent = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        account_id=account.id, description="P", amount=Decimal("10.00"), currency="USD",
        date=date.today(), type="debit", source="manual", status="posted",
    )
    plain = Transaction(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id,
        account_id=account.id, description="Q", amount=Decimal("10.00"), currency="USD",
        date=date.today(), type="debit", source="manual", status="posted",
    )
    session.add_all([parent, plain])
    await session.commit()

    uncategorized = [
        CategorySplitLineInput(category_id=None, amount=Decimal(a)) for a in ("4", "6")
    ]
    result = await split_transaction(session, test_workspace.id, parent.id, uncategorized)
    assert result is not None
    _, children = result

    counts = await child_counts(session, [parent.id, plain.id, *[c.id for c in children]])
    assert counts == {parent.id: 2}
    assert await child_counts(session, []) == {}
