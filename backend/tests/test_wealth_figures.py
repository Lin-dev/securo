"""P1 figures against a hand-computed scenario (fork, plan P1).

One seeded household exercises every rule in `wealth.figures`: salary income, a
refund netted per category, an uncategorized credit, an uncategorized debit and
credit inside an investment account (excluded), a paired contribution, an
unpaired ("direct") contribution, a card-payment transfer pair, an
opening-balance row and a pending row (all three excluded), and a one-off
contribution that `detect_one_offs` proposes and a settings override can turn
off. A second, smaller scenario isolates the investment-bucket override rule
for a *categorized* row.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.transaction import Transaction
from app.schemas.wealth import AccountAnnotation, WealthSettingsValues
from app.services.wealth import figures
from app.services.wealth.spending import month_end, month_start

pytestmark = pytest.mark.asyncio


async def _account(session: AsyncSession, user_id, workspace_id, name, acc_type, **kw) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type=acc_type, balance=Decimal("0"), currency=kw.pop("currency", "USD"), **kw,
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session: AsyncSession, user_id, workspace_id, name, transfer=False) -> Category:
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        icon="circle-help", color="#123456", treat_as_transfer=transfer,
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(user_id, workspace_id, account, amount, typ, category=None, pair=None, **kw) -> Transaction:
    when = kw.pop("when", date.today())
    return Transaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=kw.pop("description", f"{typ} {amount}"), amount=Decimal(str(amount)),
        date=when, type=typ, source=kw.pop("source", "manual"), currency="USD",
        status=kw.pop("status", "posted"), created_at=datetime.now(timezone.utc),
        category_id=category.id if category else None, transfer_pair_id=pair, **kw,
    )


def _month_keys() -> dict[str, date]:
    today = date.today()
    return {
        "today": today,
        "m0": date(today.year, today.month, 1),
        "m1": month_start(today, 1),
        "m2": month_start(today, 2),
        "m3": month_start(today, 3),
    }


# Per base month: income 5000, expenses 700 (800 - 100 refund), unclassified
# credit 50, invested 1300 (1000 paired + 300 direct), direct 300.
BASE_INCOME = 5000.0
BASE_EXPENSES = 700.0
BASE_UNCLASSIFIED = 50.0
BASE_INVESTED = 1300.0
BASE_DIRECT = 300.0
BASE_CASH_FLOW_AFTER_INVESTING = BASE_INCOME - BASE_EXPENSES - (BASE_INVESTED - BASE_DIRECT)  # 3300
BASE_SAVING = BASE_INCOME - BASE_EXPENSES + BASE_DIRECT  # 4600
ONE_OFF_AMOUNT = 9000.0


async def _seed_scenario(session: AsyncSession, test_user, test_workspace) -> dict:
    uid, wid = test_user.id, test_workspace.id
    months = _month_keys()
    checking = await _account(session, uid, wid, "Checking", "checking")
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    card = await _account(session, uid, wid, "Card", "credit_card")

    salary = await _category(session, uid, wid, "Salary")
    shopping = await _category(session, uid, wid, "Shopping")
    contrib = await _category(session, uid, wid, "Investment contribution", transfer=True)

    txns: list[Transaction] = []
    for month in (months["m3"], months["m2"], months["m1"]):
        pair = uuid.uuid4()
        txns += [
            _txn(uid, wid, checking, 5000, "credit", salary, when=month),  # salary income
            _txn(uid, wid, checking, 800, "debit", shopping, when=month),  # expense
            _txn(uid, wid, checking, 100, "credit", shopping, when=month),  # refund, nets to 700
            _txn(uid, wid, checking, 50, "credit", None, when=month),  # uncategorized credit
            _txn(uid, wid, brokerage, 20, "debit", None, when=month),  # uncategorized debit in an investment account
            _txn(uid, wid, brokerage, 30, "credit", None, when=month),  # uncategorized credit in an investment account
            _txn(uid, wid, checking, 1000, "debit", contrib, pair=pair, when=month),  # paired contribution (debit leg)
            _txn(uid, wid, brokerage, 1000, "credit", contrib, pair=pair, when=month),  # paired contribution (credit leg)
            _txn(uid, wid, brokerage, 300, "credit", contrib, when=month),  # unpaired = direct contribution
        ]
        # An opening-balance row and a pending row, same day as everything else,
        # so their exclusion is actually exercised (not just out of date range).
        txns += [
            _txn(uid, wid, checking, 10000, "credit", None, source="opening_balance", when=month),
            _txn(uid, wid, checking, 500, "debit", shopping, status="pending", when=month),
        ]

    one_off_pair = uuid.uuid4()
    one_off_date = months["m2"] + timedelta(days=14)
    one_off_debit = _txn(uid, wid, checking, ONE_OFF_AMOUNT, "debit", contrib, pair=one_off_pair, when=one_off_date)
    txns += [
        one_off_debit,
        _txn(uid, wid, brokerage, ONE_OFF_AMOUNT, "credit", contrib, pair=one_off_pair, when=one_off_date),
    ]

    card_pair = uuid.uuid4()
    card_date = months["m1"] + timedelta(days=19)
    txns += [
        _txn(uid, wid, checking, 1000, "debit", None, pair=card_pair, when=card_date),  # card payment, must be excluded
        _txn(uid, wid, card, 1000, "credit", None, pair=card_pair, when=card_date),
    ]

    session.add_all(txns)
    await session.commit()
    return {
        "checking": checking, "brokerage": brokerage, "card": card,
        "months": months, "one_off_txn_id": one_off_debit.id,
    }


async def test_period_figures_base_month_matches_hand_computed_scenario(session, test_user, test_workspace):
    seed = await _seed_scenario(session, test_user, test_workspace)
    m3 = seed["months"]["m3"]
    settings = WealthSettingsValues()

    result = await figures.period_figures(
        session, test_workspace.id, test_user.id, m3, month_end(m3), settings=settings,
    )

    assert result.income == pytest.approx(BASE_INCOME)
    assert result.expenses == pytest.approx(BASE_EXPENSES)
    assert result.unclassified_credits == pytest.approx(BASE_UNCLASSIFIED)
    assert result.invested == pytest.approx(BASE_INVESTED)
    assert result.direct == pytest.approx(BASE_DIRECT)
    assert result.cash_flow_after_investing == pytest.approx(BASE_CASH_FLOW_AFTER_INVESTING)
    assert result.savings_rate == pytest.approx((BASE_INCOME - BASE_EXPENSES + BASE_DIRECT) / (BASE_INCOME + BASE_DIRECT))
    assert result.cash_rate == pytest.approx((BASE_INCOME - BASE_EXPENSES) / BASE_INCOME)


async def test_one_off_detection_and_override(session, test_user, test_workspace):
    seed = await _seed_scenario(session, test_user, test_workspace)
    m2 = seed["months"]["m2"]
    one_off_id = seed["one_off_txn_id"]
    default_settings = WealthSettingsValues()

    one_offs = await figures.detect_one_offs(
        session, test_workspace.id, test_user.id, seed["months"]["today"], settings=default_settings,
    )
    invested_rows = [row for row in one_offs if row.lane == "invested"]
    assert len(invested_rows) == 1
    row = invested_rows[0]
    assert row.transaction_id == one_off_id
    assert row.amount == pytest.approx(ONE_OFF_AMOUNT)
    assert row.proposed is True
    assert row.selected is True  # no override yet

    # Actual (un-excluded) M2 invested includes the one-off on top of the base pattern.
    actual = await figures.period_figures(
        session, test_workspace.id, test_user.id, m2, month_end(m2), settings=default_settings,
    )
    assert actual.invested == pytest.approx(BASE_INVESTED + ONE_OFF_AMOUNT)

    # Excluding one-offs (default settings, proposal stands) drops it back to the base pattern.
    typical = await figures.period_figures(
        session, test_workspace.id, test_user.id, m2, month_end(m2), settings=default_settings,
        exclude_one_offs=True,
    )
    assert typical.invested == pytest.approx(BASE_INVESTED)

    # An override turns the proposal off: still proposed, no longer selected.
    override_settings = WealthSettingsValues(one_off_overrides={str(one_off_id): False})
    overridden_one_offs = await figures.detect_one_offs(
        session, test_workspace.id, test_user.id, seed["months"]["today"], settings=override_settings,
    )
    overridden_row = next(r for r in overridden_one_offs if r.transaction_id == one_off_id)
    assert overridden_row.proposed is True
    assert overridden_row.selected is False

    # ... so excluding one-offs under that override keeps the full amount in.
    not_excluded = await figures.period_figures(
        session, test_workspace.id, test_user.id, m2, month_end(m2), settings=override_settings,
        exclude_one_offs=True,
    )
    assert not_excluded.invested == pytest.approx(BASE_INVESTED + ONE_OFF_AMOUNT)


async def test_monthly_figures_split_in_coverage_partial_and_none_rates(session, test_user, test_workspace):
    seed = await _seed_scenario(session, test_user, test_workspace)
    months = seed["months"]
    settings = WealthSettingsValues()

    result = await figures.monthly_figures(
        session, test_workspace.id, test_user.id, months["m3"], months["m0"], settings=settings,
    )

    assert [m.month for m in result] == [months["m3"], months["m2"], months["m1"], months["m0"]]
    by_month = {m.month: m for m in result}

    for key in ("m3", "m1"):
        entry = by_month[months[key]]
        assert entry.in_coverage is True
        assert entry.partial is False
        assert entry.figures.income == pytest.approx(BASE_INCOME)
        assert entry.figures.expenses == pytest.approx(BASE_EXPENSES)
        assert entry.figures.invested == pytest.approx(BASE_INVESTED)
        assert entry.figures.direct == pytest.approx(BASE_DIRECT)

    m2_entry = by_month[months["m2"]]
    assert m2_entry.in_coverage is True
    assert m2_entry.partial is False
    assert m2_entry.figures.invested == pytest.approx(BASE_INVESTED + ONE_OFF_AMOUNT)  # not excluded by default

    m0_entry = by_month[months["m0"]]
    assert m0_entry.in_coverage is False
    assert m0_entry.partial is True
    assert m0_entry.figures.income == 0.0
    assert m0_entry.figures.expenses == 0.0
    assert m0_entry.figures.invested == 0.0
    assert m0_entry.figures.savings_rate is None  # denominator (income + direct) is 0
    assert m0_entry.figures.cash_rate is None  # income is 0


async def test_typical_figures_annualized_and_median_month(session, test_user, test_workspace):
    seed = await _seed_scenario(session, test_user, test_workspace)
    settings = WealthSettingsValues()

    result = await figures.typical_figures(session, test_workspace.id, test_user.id, settings=settings)

    assert result.coverage["from_date"] == seed["months"]["m3"]
    assert result.coverage["insufficient_history"] is False
    scale = result.coverage["scale"]

    assert result.income.all == pytest.approx(3 * BASE_INCOME * scale)
    assert result.income.typical == pytest.approx(3 * BASE_INCOME * scale)
    assert result.expenses.all == pytest.approx(3 * BASE_EXPENSES * scale)
    assert result.expenses.typical == pytest.approx(3 * BASE_EXPENSES * scale)
    assert result.direct.all == pytest.approx(3 * BASE_DIRECT * scale)
    assert result.direct.typical == pytest.approx(3 * BASE_DIRECT * scale)

    # "all" carries the one-off; "typical" (one-offs excluded) does not.
    assert result.invested.all == pytest.approx((3 * BASE_INVESTED + ONE_OFF_AMOUNT) * scale)
    assert result.invested.typical == pytest.approx(3 * BASE_INVESTED * scale)

    # saving = income - expenses + direct never touches `invested`, so it agrees either way.
    expected_saving = (3 * BASE_INCOME - 3 * BASE_EXPENSES + 3 * BASE_DIRECT) * scale
    assert result.saving.all == pytest.approx(expected_saving)
    assert result.saving.typical == pytest.approx(expected_saving)

    assert result.median_month.full_months == 3
    assert result.median_month.cash_flow_after_investing == pytest.approx(BASE_CASH_FLOW_AFTER_INVESTING)
    assert result.median_month.saving == pytest.approx(BASE_SAVING)


async def test_categorized_row_in_investment_account_counts_regardless_of_bucket_override(
    session, test_user, test_workspace,
):
    """P1's investment-bucket rule only drops *uncategorized* rows; a categorized
    row counts whether or not the account's bucket is overridden away from an
    investment bucket. An uncategorized row in the same account, by contrast,
    only counts once the override moves it out of an investment bucket."""
    uid, wid = test_user.id, test_workspace.id
    cash_mgmt = await _account(session, uid, wid, "Investment Cash Mgmt", "investment")
    fee_cat = await _category(session, uid, wid, "Advisory Fee")
    day = date.today()

    session.add_all([
        _txn(uid, wid, cash_mgmt, 200, "debit", fee_cat, when=day),  # categorized
        _txn(uid, wid, cash_mgmt, 75, "debit", None, when=day),  # uncategorized
    ])
    await session.commit()

    no_override = WealthSettingsValues()
    result_no_override = await figures.period_figures(
        session, wid, uid, day, day, settings=no_override,
    )
    assert result_no_override.expenses == pytest.approx(200.0)  # uncategorized $75 excluded (default bucket: taxable)

    with_override = WealthSettingsValues(accounts={str(cash_mgmt.id): AccountAnnotation(bucket="cash")})
    result_with_override = await figures.period_figures(
        session, wid, uid, day, day, settings=with_override,
    )
    assert result_with_override.expenses == pytest.approx(275.0)  # now the uncategorized $75 counts too
