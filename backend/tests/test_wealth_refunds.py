"""Refund categories are not income, and their credits reduce spending (fork, P1 refinement at the R1 gate).

Real category trees file "Refunds & credits" under the Income group. Counting those
credits as income inflated income and the savings rate; leaving the category out of
income made the credits vanish, because per-category netting had nothing to net
them against. Refunds beyond their own category's spending now reduce total spending.
"""
from datetime import date, timedelta

import pytest

from app.models.category import Category
from app.models.category_group import CategoryGroup
from app.schemas.wealth import WealthSettingsValues
from app.services.wealth import figures
from app.services.wealth.classify import is_default_income_category
from tests.test_wealth_figures import _account, _txn

pytestmark = pytest.mark.asyncio


class _Cat:
    def __init__(self, name, group_name):
        self.name, self.group_name = name, group_name


@pytest.mark.parametrize("name, group, expected", [
    ("Salary & wages", "Income", True),
    ("Investment income", "Income", True),
    ("Benefits & reimbursements", "Income", True),
    ("Refunds & credits", "Income", False),
    ("Cashback", "Income", False),
    ("Dining & delivery", "Food", False),
])
async def test_default_income_list_leaves_refund_categories_out(name, group, expected):
    assert is_default_income_category(_Cat(name, group)) is expected


async def test_refund_category_credits_reduce_total_spending(session, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    income_group = CategoryGroup(user_id=uid, workspace_id=wid, name="Income")
    food = CategoryGroup(user_id=uid, workspace_id=wid, name="Food")
    session.add_all([income_group, food])
    await session.flush()
    salary = Category(user_id=uid, workspace_id=wid, name="Salary & wages", group_id=income_group.id, icon="x", color="#000000")
    refunds = Category(user_id=uid, workspace_id=wid, name="Refunds & credits", group_id=income_group.id, icon="x", color="#000000")
    dining = Category(user_id=uid, workspace_id=wid, name="Dining & delivery", group_id=food.id, icon="x", color="#000000")
    session.add_all([salary, refunds, dining])
    await session.flush()
    checking = await _account(session, uid, wid, "Checking", "checking")
    day = date.today() - timedelta(days=2)
    session.add_all([
        _txn(uid, wid, checking, 4000, "credit", salary, when=day),
        _txn(uid, wid, checking, 500, "debit", dining, when=day),
        _txn(uid, wid, checking, 50, "credit", dining, when=day),     # refund in its own category
        _txn(uid, wid, checking, 100, "credit", refunds, when=day),   # statement credit, dedicated category
    ])
    await session.commit()

    result = await figures.period_figures(
        session, wid, uid, day, day, settings=WealthSettingsValues(),
    )

    assert result.income == 4000.0
    assert result.expenses == 350.0   # (500 - 50) - 100
    assert result.unclassified_credits == 0.0
