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


# ---------------------------------------------------------------------------
# Rule lines
# ---------------------------------------------------------------------------

from app.services.category_split_service import resolve_rule_lines, validate_rule_lines  # noqa: E402

CAT_A, CAT_B, CAT_C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _rule_value(*lines: dict) -> list[dict]:
    return [dict(line) for line in lines]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("nope", "at least two lines"),
        ([{"category_id": str(CAT_A), "amount": 1}], "at least two lines"),
        ([{"category_id": "x", "amount": 1}, {"category_id": str(CAT_B), "amount": 1}], "Category not found"),
        ([{"category_id": str(CAT_A)}, {"category_id": str(CAT_B), "amount": 1}], "needs an amount, a percent or remainder"),
        ([{"category_id": str(CAT_A), "amount": 1, "percent": 5}, {"category_id": str(CAT_B), "remainder": True}], "needs an amount"),
        ([{"category_id": str(CAT_A), "amount": 0}, {"category_id": str(CAT_B), "remainder": True}], "greater than zero"),
        ([{"category_id": str(CAT_A), "percent": 150}, {"category_id": str(CAT_B), "remainder": True}], "between 0 and 100"),
        ([{"category_id": str(CAT_A), "remainder": True}, {"category_id": str(CAT_B), "remainder": True}], "Only one split line"),
        ([{"category_id": str(CAT_A), "percent": 60}, {"category_id": str(CAT_B), "percent": 60}], "more than 100"),
        ([{"category_id": str(CAT_A), "percent": 40}, {"category_id": str(CAT_B), "percent": 40}], "must add up to 100"),
        ([{"category_id": str(CAT_A), "amount": "abc"}, {"category_id": str(CAT_B), "remainder": True}], "needs an amount"),
    ],
)
def test_validate_rule_lines_rejects(value, message):
    with pytest.raises(ValueError, match=message):
        validate_rule_lines(value)


def test_validate_rule_lines_normalizes():
    lines = validate_rule_lines(_rule_value(
        {"category_id": str(CAT_A), "amount": "250"},
        {"category_id": str(CAT_B), "percent": "25.5"},
        {"category_id": str(CAT_C), "remainder": True},
    ))
    assert lines[0] == {"category_id": CAT_A, "amount": Decimal("250.00"), "percent": None, "remainder": False}
    assert lines[1]["percent"] == Decimal("25.5")
    assert lines[2]["remainder"] is True
    # Fixed-only lines are allowed: they simply skip rows they do not fit.
    validate_rule_lines(_rule_value({"category_id": str(CAT_A), "amount": 250}, {"category_id": str(CAT_B), "amount": 250}))


def _resolved(lines, total, hidden=()):
    resolved = resolve_rule_lines(validate_rule_lines(lines), Decimal(total), hidden)
    return None if resolved is None else [(line.category_id, line.amount) for line in resolved]


def test_resolve_rule_lines_fixed_percent_and_remainder():
    fixed = _rule_value({"category_id": str(CAT_A), "amount": 250}, {"category_id": str(CAT_B), "amount": 250})
    assert _resolved(fixed, "500") == [(CAT_A, Decimal("250.00")), (CAT_B, Decimal("250.00"))]
    assert _resolved(fixed, "300") is None

    percent = _rule_value({"category_id": str(CAT_A), "percent": 50}, {"category_id": str(CAT_B), "percent": 50})
    assert _resolved(percent, "333.33") == [(CAT_A, Decimal("166.67")), (CAT_B, Decimal("166.66"))]

    remainder = _rule_value(
        {"category_id": str(CAT_A), "amount": 250},
        {"category_id": str(CAT_B), "percent": 10},
        {"category_id": str(CAT_C), "remainder": True},
    )
    assert _resolved(remainder, "1000") == [
        (CAT_A, Decimal("250.00")), (CAT_B, Decimal("100.00")), (CAT_C, Decimal("650.00")),
    ]
    # The remainder line would be zero or negative: the rule does not fit.
    assert _resolved(remainder, "250") is None
    assert _resolved(remainder, "-1000") == [
        (CAT_A, Decimal("250.00")), (CAT_B, Decimal("100.00")), (CAT_C, Decimal("650.00")),
    ]


def test_resolve_rule_lines_skips_hidden_categories():
    fixed = _rule_value({"category_id": str(CAT_A), "amount": 250}, {"category_id": str(CAT_B), "amount": 250})
    assert _resolved(fixed, "500", hidden={CAT_B}) is None
