"""/api/wealth's summary, net-worth history, savings-rate, projection and
levers endpoints, through the `client` fixture (fork, plan B3).

One seeded household drives every test here: a checking and a savings
account (cash), a brokerage account with synced holdings (taxable), a
card (card debt), 3 full months of salary income, a "Dining" expense with
a refund (non-essential, the top controllable category), a "Rent" expense
(essential, excluded from "top controllable"), a paired 401(k)-style
contribution, a direct (unpaired) contribution, a one-off expense, and two
net-worth goals. The current (partial) month is deliberately left empty —
`partial` is a date flag, not a function of having data.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.bank_connection import BankConnection
from app.models.category import Category
from app.models.goal import Goal
from app.models.transaction import Transaction
from app.schemas.wealth import WealthSettingsValues
from app.services import dashboard_service
from app.services.asset_service import ACCOUNT_LINK_METADATA_KEY
from app.services.wealth import figures as figures_module
from app.services.wealth.policy import SAVINGS_BANDS
from app.services.wealth.spending import month_start

pytestmark = pytest.mark.asyncio

CURRENCY = "BRL"  # matches `test_workspace.default_currency`, so no FX conversion is in play

BASE_INCOME = 5000.0
BASE_DINING_NET = 700.0  # 800 debit - 100 refund
BASE_RENT = 1200.0
BASE_EXPENSES = BASE_DINING_NET + BASE_RENT  # 1900
BASE_PAIRED_INVESTED = 1000.0
BASE_DIRECT = 300.0
BASE_INVESTED = BASE_PAIRED_INVESTED + BASE_DIRECT  # 1300
ONE_OFF_AMOUNT = 9000.0

CASH_TOTAL = 20_000.0  # checking 8,000 + savings 12,000
BROKERAGE_BALANCE = 15_500.0
BROKERAGE_HOLDINGS = 15_000.0  # cash offset inside the brokerage account: 500
CARD_OWED = 300.0
NET_WORTH = CASH_TOTAL + BROKERAGE_BALANCE - CARD_OWED  # 35,200

BUFFER_MONTHS_DEFAULT = 3.0
MEDIAN_MONTHLY_SPEND = BASE_EXPENSES  # identical every full month once the one-off is excluded
BUFFER = BUFFER_MONTHS_DEFAULT * MEDIAN_MONTHLY_SPEND  # 5,700
IDLE_CASH = CASH_TOTAL - BUFFER  # 14,300
CASH_CUSHION_MONTHS = CASH_TOTAL / MEDIAN_MONTHLY_SPEND

GOAL_BIG_TARGET = 200_000.0
GOAL_SMALL_TARGET = 20_000.0


# --- seeding helpers -----------------------------------------------------------------


async def _connection(session: AsyncSession, user_id, workspace_id) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}", institution_name="Bank",
        credentials={"token": "fake"}, status="active", settings=None,
    )
    session.add(conn)
    await session.flush()
    return conn


async def _account(
    session: AsyncSession, user_id, workspace_id, connection_id, *, name, acc_type, balance, external_id,
) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name, type=acc_type,
        balance=Decimal(str(balance)), currency=CURRENCY, connection_id=connection_id, external_id=external_id,
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session: AsyncSession, user_id, workspace_id, name, *, transfer=False) -> Category:
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
        date=when, type=typ, source=kw.pop("source", "manual"), currency=CURRENCY, status="posted",
        category_id=category.id if category else None, transfer_pair_id=pair, **kw,
    )


async def _seed(session: AsyncSession, test_user, test_workspace, *, with_goals: bool) -> dict:
    uid, wid = test_user.id, test_workspace.id
    today = date.today()
    months = {"m3": month_start(today, 3), "m2": month_start(today, 2), "m1": month_start(today, 1)}

    conn = await _connection(session, uid, wid)
    checking = await _account(session, uid, wid, conn.id, name="Checking", acc_type="checking", balance=8_000, external_id="chk-1")
    savings = await _account(session, uid, wid, conn.id, name="Savings", acc_type="savings", balance=12_000, external_id="sav-1")
    brokerage = await _account(session, uid, wid, conn.id, name="Brokerage", acc_type="investment", balance=BROKERAGE_BALANCE, external_id="inv-1")
    card = await _account(session, uid, wid, conn.id, name="Card", acc_type="credit_card", balance=CARD_OWED, external_id="card-1")

    salary = await _category(session, uid, wid, "Salary")
    dining = await _category(session, uid, wid, "Dining")
    rent = await _category(session, uid, wid, "Rent")
    contrib = await _category(session, uid, wid, "Investment contribution", transfer=True)

    txns: list[Transaction] = []
    for month in (months["m3"], months["m2"], months["m1"]):
        pair = uuid.uuid4()
        txns += [
            _txn(uid, wid, checking, BASE_INCOME, "credit", salary, when=month),
            _txn(uid, wid, checking, 800, "debit", dining, when=month),
            _txn(uid, wid, checking, 100, "credit", dining, when=month),  # refund, nets dining to 700
            _txn(uid, wid, checking, BASE_RENT, "debit", rent, when=month),
            _txn(uid, wid, checking, BASE_PAIRED_INVESTED, "debit", contrib, pair=pair, when=month),
            _txn(uid, wid, brokerage, BASE_PAIRED_INVESTED, "credit", contrib, pair=pair, when=month),
            _txn(uid, wid, brokerage, BASE_DIRECT, "credit", contrib, when=month),  # unpaired = direct
        ]
    one_off_date = months["m2"] + timedelta(days=10)
    one_off_txn = _txn(uid, wid, checking, ONE_OFF_AMOUNT, "debit", dining, when=one_off_date)
    txns.append(one_off_txn)
    session.add_all(txns)

    asset = Asset(
        id=uuid.uuid4(), user_id=uid, workspace_id=wid, name="VTI", type="investment", currency=CURRENCY,
        source="simplefin", connection_id=conn.id, external_id=f"h-{uuid.uuid4().hex[:8]}",
        external_metadata={"symbol": "VTI", ACCOUNT_LINK_METADATA_KEY: str(brokerage.id)},
        purchase_price=Decimal("0"), purchase_date=today - timedelta(days=400), valuation_method="manual",
    )
    session.add(asset)
    await session.flush()
    session.add(AssetValue(
        id=uuid.uuid4(), asset_id=asset.id, workspace_id=wid, amount=Decimal(str(BROKERAGE_HOLDINGS)),
        date=today, source="sync",
    ))

    goal_big = goal_small = None
    if with_goals:
        goal_big = Goal(
            id=uuid.uuid4(), user_id=uid, workspace_id=wid, name="Big milestone",
            target_amount=Decimal(str(GOAL_BIG_TARGET)), current_amount=Decimal("0"), initial_amount=Decimal("0"),
            currency=CURRENCY, tracking_type="net_worth", status="active",
        )
        goal_small = Goal(
            id=uuid.uuid4(), user_id=uid, workspace_id=wid, name="Small milestone",
            target_amount=Decimal(str(GOAL_SMALL_TARGET)), current_amount=Decimal("0"), initial_amount=Decimal("0"),
            currency=CURRENCY, tracking_type="net_worth", status="active",
        )
        session.add_all([goal_big, goal_small])

    await session.commit()
    return {
        "checking": checking, "savings": savings, "brokerage": brokerage, "card": card,
        "months": months, "one_off_txn_id": one_off_txn.id, "goal_big": goal_big, "goal_small": goal_small,
    }


async def _typical_savings_rate_and_band_cut(session: AsyncSession, workspace_id, user_id) -> tuple[float, float]:
    """Independently re-derives the typical P1 savings rate and the
    `policy.SAVINGS_BANDS` band cut from `figures.typical_figures` — the same
    identity `wealth.levers._band_next_and_cut` uses, re-applied here so the
    test is a real cross-check, not a tautology."""
    typical = await figures_module.typical_figures(
        session, workspace_id, user_id, settings=WealthSettingsValues(),
    )
    income, expenses, direct = typical.income.typical, typical.expenses.typical, typical.direct.typical
    denom = income + direct
    rate = (income - expenses + direct) / denom
    band_next = next(b for b in SAVINGS_BANDS if rate < b)
    band_cut = (band_next * denom - (income - expenses + direct)) / 12
    return rate, band_cut


# --- summary --------------------------------------------------------------------------


async def test_summary_matches_dashboard_goal_and_projection(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    await _seed(session, test_user, test_workspace, with_goals=True)

    dash = await dashboard_service.get_summary(session, test_workspace.id, test_user.id)
    summary_resp = await client.get("/api/wealth/summary", headers=auth_headers)
    assert summary_resp.status_code == 200, summary_resp.text
    summary = summary_resp.json()

    assert summary["net_worth"] == pytest.approx(float(dash.total_balance_primary), abs=0.01)
    assert summary["net_worth"] == pytest.approx(NET_WORTH, abs=0.01)
    assert summary["goal"]["current"] == pytest.approx(summary["net_worth"], abs=0.01)
    assert summary["goal"]["target"] == pytest.approx(GOAL_BIG_TARGET, abs=0.01)  # the larger of the two
    assert summary["coverage"]["days"] < 180
    assert summary["precision"] == "quarter"

    projection_resp = await client.get("/api/wealth/projection", headers=auth_headers)
    assert projection_resp.status_code == 200, projection_resp.text
    base = projection_resp.json()["scenarios"]["base"]
    assert summary["projected_date"] == base["date"]
    assert summary["projected_label"] == base["label"]


async def test_summary_savings_rate_3m_is_a_ratio_of_sums(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    await _seed(session, test_user, test_workspace, with_goals=False)

    # Last 3 full months (m3, m2, m1): Σincome = 15,000, Σexpenses = 1,900 +
    # (1,900 + 9,000) + 1,900 = 14,700, Σdirect = 900 — a ratio of sums, not
    # an average of each month's own rate.
    expected = (3 * BASE_INCOME - (2 * BASE_EXPENSES + (BASE_EXPENSES + ONE_OFF_AMOUNT)) + 3 * BASE_DIRECT) / (
        3 * BASE_INCOME + 3 * BASE_DIRECT
    )

    resp = await client.get("/api/wealth/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["savings_rate_3m"] == pytest.approx(expected)


async def test_no_goal_defaults_to_one_million(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    await _seed(session, test_user, test_workspace, with_goals=False)

    summary_resp = await client.get("/api/wealth/summary", headers=auth_headers)
    assert summary_resp.status_code == 200, summary_resp.text
    assert summary_resp.json()["goal"] is None

    projection_resp = await client.get("/api/wealth/projection", headers=auth_headers)
    assert projection_resp.status_code == 200, projection_resp.text
    assert projection_resp.json()["inputs"]["target_total"] == pytest.approx(1_000_000.0)


async def test_settings_goal_id_overrides_the_default_largest_target(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    seed = await _seed(session, test_user, test_workspace, with_goals=True)

    put_resp = await client.put(
        "/api/wealth/settings", headers=auth_headers, json={"goal_id": str(seed["goal_small"].id)},
    )
    assert put_resp.status_code == 200, put_resp.text

    summary_resp = await client.get("/api/wealth/summary", headers=auth_headers)
    assert summary_resp.status_code == 200, summary_resp.text
    assert summary_resp.json()["goal"]["target"] == pytest.approx(GOAL_SMALL_TARGET)


# --- net worth history ------------------------------------------------------------------


async def test_net_worth_history_last_point_matches_summary(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    await _seed(session, test_user, test_workspace, with_goals=False)

    summary_resp = await client.get("/api/wealth/summary", headers=auth_headers)
    history_resp = await client.get("/api/wealth/net-worth-history", headers=auth_headers)
    assert history_resp.status_code == 200, history_resp.text

    points = history_resp.json()["points"]
    assert points[-1]["date"] == date.today().isoformat()
    assert points[-1]["total"] == pytest.approx(summary_resp.json()["net_worth"], abs=0.01)


# --- levers and projection --------------------------------------------------------------


async def test_levers_idle_cash_cushion_and_cash_flow(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    await _seed(session, test_user, test_workspace, with_goals=False)

    resp = await client.get("/api/wealth/levers", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["cash"] == pytest.approx(CASH_TOTAL, abs=0.01)
    assert body["buffer"] == pytest.approx(BUFFER, abs=0.01)
    assert body["idle_cash"] == pytest.approx(IDLE_CASH, abs=0.01)
    assert body["cash_cushion_months"] == pytest.approx(CASH_CUSHION_MONTHS)
    assert body["cash_flow"]["months"] == 3
    assert body["cash_flow"]["typical_monthly"] == pytest.approx(
        BASE_INCOME - BASE_EXPENSES - (BASE_INVESTED - BASE_DIRECT)
    )
    assert body["investing_exceeds_saving"] is False
    assert body["top_controllable"]["category"] == "Dining"  # Rent is essential, excluded

    _, expected_band_cut = await _typical_savings_rate_and_band_cut(session, test_workspace.id, test_user.id)
    assert body["top_controllable"]["band_cut"] == pytest.approx(expected_band_cut, abs=0.01)

    summary_resp = await client.get("/api/wealth/summary", headers=auth_headers)
    assert summary_resp.json()["idle_cash"] == pytest.approx(body["idle_cash"], abs=0.01)
    assert summary_resp.json()["cash_cushion_months"] == pytest.approx(body["cash_cushion_months"])
    assert summary_resp.json()["cash_flow_typical_monthly"] == pytest.approx(body["cash_flow"]["typical_monthly"])


async def test_projection_levers_include_idle_cash_and_the_band_cut(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    await _seed(session, test_user, test_workspace, with_goals=False)

    levers_resp = await client.get("/api/wealth/levers", headers=auth_headers)
    projection_resp = await client.get("/api/wealth/projection", headers=auth_headers)
    assert projection_resp.status_code == 200, projection_resp.text

    levers_by_key = {lv["key"]: lv for lv in projection_resp.json()["levers"]}
    assert IDLE_CASH > 0
    assert "idle_cash" in levers_by_key
    assert levers_by_key["idle_cash"]["amount"] == pytest.approx(levers_resp.json()["idle_cash"], abs=0.01)

    band_cut = levers_resp.json()["top_controllable"]["band_cut"]
    if band_cut is not None and band_cut > 0:
        assert "cut_top" in levers_by_key
        assert levers_by_key["cut_top"]["amount"] == pytest.approx(band_cut, abs=0.01)


# --- savings rate series -----------------------------------------------------------------


async def test_savings_rate_series_rolling_3m_and_partial_current_month(
    session: AsyncSession, test_user, test_workspace, client: AsyncClient, auth_headers,
):
    seed = await _seed(session, test_user, test_workspace, with_goals=False)

    resp = await client.get("/api/wealth/savings-rate?months=4", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    months = resp.json()["months"]
    assert len(months) == 4
    by_month = {m["month"]: m for m in months}

    current_month = seed["months"]["m1"]
    m1 = seed["months"]["m1"]
    m2 = seed["months"]["m2"]
    today_month = month_start(date.today(), 0).isoformat()

    assert by_month[today_month]["partial"] is True
    assert by_month[today_month]["in_coverage"] is False

    m1_entry = by_month[m1.isoformat()]
    assert m1_entry["income"] == pytest.approx(BASE_INCOME)
    assert m1_entry["expenses"] == pytest.approx(BASE_EXPENSES)
    assert m1_entry["direct"] == pytest.approx(BASE_DIRECT)
    assert m1_entry["rate"] == pytest.approx((BASE_INCOME - BASE_EXPENSES + BASE_DIRECT) / (BASE_INCOME + BASE_DIRECT))
    assert m1_entry["cash_rate"] == pytest.approx((BASE_INCOME - BASE_EXPENSES) / BASE_INCOME)

    m2_entry = by_month[m2.isoformat()]
    assert m2_entry["expenses"] == pytest.approx(BASE_EXPENSES + ONE_OFF_AMOUNT)  # the one-off is not excluded here

    # Rolling 3m at m1 (the window m3, m2, m1): a hand-checked ratio of sums.
    expected_rolling_3m = (3 * BASE_INCOME - (2 * BASE_EXPENSES + (BASE_EXPENSES + ONE_OFF_AMOUNT)) + 3 * BASE_DIRECT) / (
        3 * BASE_INCOME + 3 * BASE_DIRECT
    )
    assert m1_entry["rolling_3m"] == pytest.approx(expected_rolling_3m)
    assert current_month == m1  # sanity: m1 is indeed the window's last full month


# --- module gate -----------------------------------------------------------------------


async def test_business_workspace_summary_is_404(client: AsyncClient, auth_headers):
    created = await client.post(
        "/api/workspaces", headers=auth_headers,
        json={"name": "Consultoria", "kind": "business", "self_membership": True},
    )
    assert created.status_code == 201, created.text
    headers = {**auth_headers, "X-Workspace-Id": created.json()["id"]}

    resp = await client.get("/api/wealth/summary", headers=headers)
    assert resp.status_code == 404
