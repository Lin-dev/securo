"""Finance-analysis MCP tools (fork addition, qc7).

Each tool is a thin wrapper over a service the UI already uses, so the tests
seed real rows and check that the tool quotes the same figures the service
does, with the shapes the agent prompt relies on.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import mcp_server.tools  # noqa: F401  (registers the tools)
from app.models.account import Account
from app.models.asset import Asset
from app.models.asset_value import AssetValue
from app.models.category import Category
from app.models.transaction import Transaction
from app.services._query_filters import INVESTMENT_CONTRIBUTION_CATEGORY
from app.services.asset_service import ACCOUNT_LINK_METADATA_KEY
from app.services.rule_engine import _normalize
from app.services.transaction_service import get_transactions
from mcp_server.auth import CallContext
from mcp_server.registry import REGISTRY
from mcp_server.tools._helpers import merchant_key, suggest_pattern

pytestmark = pytest.mark.asyncio


def _py_to_char(value, fmt):
    """SQLite stand-in for the Postgres to_char the report buckets periods with."""
    if value is None:
        return None
    y, m, d = str(value)[:10].split("-")
    return {"YYYY-MM-DD": f"{y}-{m}-{d}", "YYYY-MM": f"{y}-{m}", "YYYY": y}.get(fmt, str(value)[:10])


@pytest.fixture(autouse=True)
async def _ensure_to_char(session: AsyncSession):
    raw = await session.connection()

    def _do(sync_conn):
        sync_conn.connection.dbapi_connection.create_function("to_char", 2, _py_to_char)

    await raw.run_sync(_do)
    yield


@pytest_asyncio.fixture
async def ctx(test_user) -> CallContext:
    return CallContext(user_id=test_user.id, conversation_id=uuid.uuid4())


async def _account(session, user_id, workspace_id, name, acc_type, **kw):
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type=acc_type, balance=Decimal("0"), currency=kw.pop("currency", "BRL"), **kw,
    )
    session.add(acc)
    await session.flush()
    return acc


async def _category(session, user_id, workspace_id, name, transfer=True):
    cat = Category(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        icon="circle-help", color="#123456", treat_as_transfer=transfer,
    )
    session.add(cat)
    await session.flush()
    return cat


def _txn(user_id, workspace_id, account, amount, typ, category=None, pair=None, **kw):
    when = kw.pop("when", date.today())
    return Transaction(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, account_id=account.id,
        description=kw.pop("description", f"{typ} {amount}"), amount=Decimal(str(amount)),
        date=when, type=typ, source="manual", currency="BRL", status="posted",
        created_at=datetime.now(timezone.utc),
        category_id=category.id if category else None, transfer_pair_id=pair, **kw,
    )


async def _seed_summary_rows(session, uid, wid):
    """income 1000, expense 30, invested 225 (100 paired + 50 direct + 75 unpaired)."""
    checking = await _account(session, uid, wid, "Checking", "checking")
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    contribution = await _category(session, uid, wid, INVESTMENT_CONTRIBUTION_CATEGORY)
    salary = await _category(session, uid, wid, "Salary", transfer=False)
    pair = uuid.uuid4()
    session.add_all([
        _txn(uid, wid, checking, 1000, "credit", salary),
        _txn(uid, wid, checking, 30, "debit"),
        _txn(uid, wid, checking, 100, "debit", contribution, pair),
        _txn(uid, wid, brokerage, 100, "credit", contribution, pair),
        _txn(uid, wid, brokerage, 50, "credit", contribution),
        _txn(uid, wid, checking, 75, "debit", contribution),
    ])
    await session.commit()
    return checking, brokerage


# --- registry ------------------------------------------------------------------

def test_registry_contains_finance_tools():
    expected = {
        "get_transactions_summary",
        "get_money_map",
        "list_uncategorized_merchants",
        "list_rules",
        "get_holdings",
        "fire_projection",
    }
    assert expected <= set(REGISTRY.keys())
    for name in expected:
        assert not REGISTRY[name].is_proposal
        assert "finance" in REGISTRY[name].tags


# --- get_transactions_summary -----------------------------------------------------

async def test_get_transactions_summary_matches_service_summary(
    session: AsyncSession, ctx: CallContext, test_user, test_workspace
):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    today = date.today().isoformat()

    result = await REGISTRY["get_transactions_summary"].handler(
        session=session, ctx=ctx, from_date=today, to_date=today
    )

    _, _, svc = await get_transactions(
        session, test_workspace.id, test_user.id,
        from_date=date.today(), to_date=date.today(), include_summary=True,
    )
    assert result["currency"] == "BRL"
    assert result["income"] == float(svc["income"]) == 1000.0
    assert result["expense"] == float(svc["expense"]) == 30.0
    assert result["net"] == 970.0
    assert result["invested"] == float(svc["invested"]) == 225.0
    assert result["excluded"] == float(svc["excluded"])
    assert result["savings_rate"] == round(970 / 1000, 4)
    assert "items" not in result
    assert "notes" in result


async def test_get_transactions_summary_group_by_month_returns_one_row_per_month(
    session: AsyncSession, ctx: CallContext, test_user, test_workspace
):
    await _seed_summary_rows(session, test_user.id, test_workspace.id)
    today = date.today()
    prev_month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)

    result = await REGISTRY["get_transactions_summary"].handler(
        session=session, ctx=ctx,
        from_date=prev_month_start.isoformat(), to_date=today.isoformat(), group_by="month",
    )

    assert [i["month"] for i in result["items"]] == [
        prev_month_start.strftime("%Y-%m"), today.strftime("%Y-%m"),
    ]
    empty, current = result["items"]
    assert empty["income"] == 0.0 and empty["savings_rate"] is None
    assert current["income"] == 1000.0 and current["invested"] == 225.0
    assert current["from_date"] == today.replace(day=1).isoformat()
    assert current["to_date"] == today.isoformat()
    assert result["income"] == 1000.0  # the whole-range figures stay on top


async def test_get_transactions_summary_rejects_long_month_ranges(
    session: AsyncSession, ctx: CallContext
):
    result = await REGISTRY["get_transactions_summary"].handler(
        session=session, ctx=ctx, from_date="2020-01-01", to_date="2026-09-01", group_by="month",
    )
    assert "max 24" in result["error"]


async def test_get_transactions_summary_validates_input(session: AsyncSession, ctx: CallContext):
    handler = REGISTRY["get_transactions_summary"].handler
    assert "error" in await handler(session=session, ctx=ctx, from_date="2026-02-01", to_date="2026-01-01")
    assert "error" in await handler(session=session, ctx=ctx, from_date="2026-01-01", to_date="2026-01-31", accounting_mode="weird")


# --- get_money_map ------------------------------------------------------------------

async def test_get_money_map_regroups_report_composition(
    session: AsyncSession, ctx: CallContext, test_user, test_workspace
):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, "Checking", "checking")
    card = await _account(session, uid, wid, "Card", "credit_card")
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    plan = await _account(session, uid, wid, "Employer 401(k)", "investment")
    contribution = await _category(session, uid, wid, INVESTMENT_CONTRIBUTION_CATEGORY)
    card_payment = await _category(session, uid, wid, "Card payment")
    brokerage_activity = await _category(session, uid, wid, "Brokerage activity")
    salary = await _category(session, uid, wid, "Salary", transfer=False)
    groceries = await _category(session, uid, wid, "Groceries", transfer=False)

    paired_contribution = uuid.uuid4()
    paired_card_payment = uuid.uuid4()
    session.add_all([
        _txn(uid, wid, checking, 5000, "credit", salary),
        _txn(uid, wid, checking, 120, "debit", groceries),
        _txn(uid, wid, checking, 400, "debit", contribution),
        _txn(uid, wid, checking, 300, "debit", contribution, paired_contribution),
        _txn(uid, wid, brokerage, 300, "credit", contribution, paired_contribution),
        _txn(uid, wid, plan, 1000, "credit", contribution),
        _txn(uid, wid, plan, 250, "credit", contribution),
        _txn(uid, wid, brokerage, 700, "debit", brokerage_activity),
        _txn(uid, wid, plan, 1250, "debit", brokerage_activity),
        _txn(uid, wid, brokerage, 80, "credit", brokerage_activity),
        _txn(uid, wid, checking, 200, "debit", card_payment),
        _txn(uid, wid, checking, 500, "debit", card_payment, paired_card_payment),
        _txn(uid, wid, card, 500, "credit", card_payment, paired_card_payment),
    ])
    await session.commit()

    result = await REGISTRY["get_money_map"].handler(session=session, ctx=ctx, days=30)

    lanes = result["lanes"]
    assert lanes["income"]["total"] == 5000.0
    assert lanes["expenses"]["total"] == 120.0
    assert lanes["investments"]["total"] == 400 + 300 + 1000 + 250
    assert lanes["direct_contributions"]["total"] == 1250.0
    assert lanes["transfers_out"]["total"] == 200.0
    assert lanes["transfers_in"]["total"] == 0.0
    assert {i["label"] for i in lanes["investments"]["items"]} == {
        INVESTMENT_CONTRIBUTION_CATEGORY, "Employer 401(k)",
    }
    totals = result["totals"]
    assert totals["net"] == 5000 + 1250 - 120 - 1950 - 200
    assert totals["cash_savings_rate"] == round((5000 - 120) / 5000, 4)
    assert totals["contribution_aware_savings_rate"] == round((5000 - 120 + 1250) / (5000 + 1250), 4)
    assert result["window"]["days"] == 30
    assert result["window"]["from_date"] == (date.today() - timedelta(days=29)).isoformat()
    assert result["currency"] == "BRL"


async def test_get_money_map_rejects_both_windows(session: AsyncSession, ctx: CallContext):
    result = await REGISTRY["get_money_map"].handler(session=session, ctx=ctx, days=30, months=2)
    assert "error" in result


# --- list_uncategorized_merchants -----------------------------------------------------

async def test_list_uncategorized_merchants_groups_by_normalized_description(
    session: AsyncSession, ctx: CallContext, test_user, test_workspace, test_account, test_transactions
):
    today = date.today()
    session.add_all([
        _txn(test_user.id, test_workspace.id, test_account, 10, "debit",
             description="UBER *TRIP 1234", when=today),
        _txn(test_user.id, test_workspace.id, test_account, 20, "debit",
             description="UBER *TRIP 5678", when=today - timedelta(days=2)),
    ])
    await session.commit()

    result = await REGISTRY["list_uncategorized_merchants"].handler(session=session, ctx=ctx)

    # NETFLIX and PIX RECEBIDO from the fixture are uncategorized; the two
    # UBER rows collapse into one merchant.
    assert result["total_uncategorized_count"] == 4
    assert result["merchant_count"] == 3
    assert result["truncated"] is False
    assert result["items"][0]["merchant"] == "PIX RECEBIDO"  # largest total first

    uber = next(i for i in result["items"] if i["merchant"] == "UBER TRIP")
    assert uber["count"] == 2
    assert uber["total"] == 30.0
    assert uber["pattern_suggestion"] == "UBER"
    assert uber["type_mix"] == {"debit": 2}
    assert uber["accounts"] == [test_account.name]
    assert uber["first_date"] == (today - timedelta(days=2)).isoformat()
    assert uber["last_date"] == today.isoformat()
    assert len(uber["sample_transaction_ids"]) == 2
    assert set(uber["sample_descriptions"]) == {"UBER *TRIP 1234", "UBER *TRIP 5678"}

    only_repeats = await REGISTRY["list_uncategorized_merchants"].handler(
        session=session, ctx=ctx, min_count=2
    )
    assert [i["merchant"] for i in only_repeats["items"]] == ["UBER TRIP"]


def test_merchant_key_and_pattern_suggestion_strip_noise():
    assert merchant_key("Uber *Trip 1234") == "UBER TRIP"
    assert merchant_key("Padaria São João #12") == "PADARIA SAO JOAO"
    assert merchant_key("") == "(blank)"

    cases = {
        "UBER *TRIP 1234": "UBER",
        "AMZN Mktp US*2K3JD": "AMZN MKTP US",
        "WHOLEFDS MKT #10234": "WHOLEFDS MKT",
        "Padaria São João 12/03": "PADARIA SAO JOAO",
        "NETFLIX": "NETFLIX",
        "TST* CAFE 123": "TST",
    }
    for description, expected in cases.items():
        suggestion = suggest_pattern(description)
        assert suggestion == expected
        # The invariant a rule needs: a `contains` condition built on the
        # suggestion matches the row it came from.
        assert suggestion in _normalize(description)


# --- list_rules -----------------------------------------------------------------------

async def test_list_rules_includes_category_names_and_priority_stats(
    session: AsyncSession, ctx: CallContext, test_rules, test_categories
):
    result = await REGISTRY["list_rules"].handler(session=session, ctx=ctx)

    assert result["total"] == 3
    assert [r["priority"] for r in result["items"]] == [10, 10, 10]
    uber = next(r for r in result["items"] if r["name"] == "UBER rule")
    assert uber["actions"][0]["category_name"] == test_categories[1].name
    assert uber["conditions"] == [{"field": "description", "op": "starts_with", "value": "UBER"}]
    assert result["priority_stats"] == {"min": 10, "max": 10, "most_common": 10}
    assert result["suggested_priority_for_new_merchant_rule"] == 10

    test_rules[0].is_active = False
    await session.commit()
    active_only = await REGISTRY["list_rules"].handler(session=session, ctx=ctx)
    assert active_only["total"] == 2
    everything = await REGISTRY["list_rules"].handler(session=session, ctx=ctx, include_inactive=True)
    assert everything["total"] == 3


# --- get_holdings ---------------------------------------------------------------------

async def _asset(session, uid, wid, name, value, *, currency="BRL", account_id=None):
    metadata = {"symbol": name}
    if account_id is not None:
        metadata[ACCOUNT_LINK_METADATA_KEY] = str(account_id)
    asset = Asset(
        id=uuid.uuid4(), user_id=uid, workspace_id=wid, name=name, type="investment",
        currency=currency, source="manual", external_metadata=metadata,
        purchase_price=Decimal(str(value)), purchase_date=date.today() - timedelta(days=30),
        valuation_method="manual",
    )
    session.add(asset)
    await session.flush()
    session.add(AssetValue(
        id=uuid.uuid4(), asset_id=asset.id, workspace_id=wid,
        amount=Decimal(str(value)), date=date.today(), source="manual",
    ))
    await session.commit()
    return asset


async def test_get_holdings_attaches_linked_assets_to_investment_account(
    session: AsyncSession, ctx: CallContext, test_user, test_workspace
):
    uid, wid = test_user.id, test_workspace.id
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    checking = await _account(session, uid, wid, "Checking", "checking")
    activity = await _category(session, uid, wid, "Brokerage activity")
    session.add(_txn(uid, wid, brokerage, 5000, "credit", activity))
    session.add(_txn(uid, wid, checking, 800, "credit"))
    await session.commit()
    linked = await _asset(session, uid, wid, "VTI", 900, account_id=brokerage.id)
    loose = await _asset(session, uid, wid, "BTC", 400, currency="USD")

    result = await REGISTRY["get_holdings"].handler(session=session, ctx=ctx)

    assert result["currency"] == "BRL"
    assert [a["account_id"] for a in result["accounts"]] == [str(brokerage.id)]  # checking is not investment
    bucket = result["accounts"][0]
    assert bucket["balance"] == 5000.0 and bucket["balance_primary"] == 5000.0
    assert [h["id"] for h in bucket["holdings"]] == [str(linked.id)]
    holding = bucket["holdings"][0]
    assert holding["current_value"] == 900.0 and holding["cost_basis"] == 900.0 and holding["gain_loss"] == 0.0
    assert holding["linked_account_id"] == str(brokerage.id)
    assert [h["id"] for h in result["unlinked_holdings"]] == [str(loose.id)]
    assert result["investment_accounts_total_primary"] == 5000.0
    assert result["unlinked_holdings_total_by_currency"] == {"USD": 400.0}
    assert result["unconverted_accounts"] == []
    assert "double_count_note" in result


async def test_get_holdings_account_filter_returns_only_that_account(
    session: AsyncSession, ctx: CallContext, test_user, test_workspace
):
    uid, wid = test_user.id, test_workspace.id
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    other = await _account(session, uid, wid, "Other brokerage", "investment")
    await _asset(session, uid, wid, "VTI", 900, account_id=brokerage.id)
    await _asset(session, uid, wid, "BTC", 400)

    result = await REGISTRY["get_holdings"].handler(session=session, ctx=ctx, account_id=str(other.id))

    assert [a["account_id"] for a in result["accounts"]] == [str(other.id)]
    assert result["accounts"][0]["holdings"] == []
    assert result["unlinked_holdings"] == []


# --- fire_projection ------------------------------------------------------------------

async def test_fire_projection_derives_defaults_from_data(
    session: AsyncSession, ctx: CallContext, test_user, test_workspace
):
    uid, wid = test_user.id, test_workspace.id
    checking = await _account(session, uid, wid, "Checking", "checking")
    brokerage = await _account(session, uid, wid, "Brokerage", "investment")
    contribution = await _category(session, uid, wid, INVESTMENT_CONTRIBUTION_CATEGORY)
    activity = await _category(session, uid, wid, "Brokerage activity")
    session.add_all([
        _txn(uid, wid, checking, 3000, "debit"),                # expense
        _txn(uid, wid, checking, 75, "debit", contribution),    # invested
        _txn(uid, wid, brokerage, 5000, "credit", activity),    # balance, not P&L
    ])
    await session.commit()
    await _asset(session, uid, wid, "VTI", 900, account_id=brokerage.id)  # inside the balance
    await _asset(session, uid, wid, "BOND", 100)                          # unlinked, BRL → counts
    await _asset(session, uid, wid, "BTC", 400, currency="USD")           # unlinked, USD → listed only

    result = await REGISTRY["fire_projection"].handler(session=session, ctx=ctx)

    assert result["currency"] == "BRL"
    assert result["derived"]["annual_spend"]["value"] == 3000.0
    assert result["derived"]["annual_contribution"]["value"] == 75.0
    assert result["derived"]["invested_assets"]["value"] == 5100.0
    assert result["derived"]["invested_assets"]["unconverted"]["holdings_by_currency"] == {"USD": 400.0}
    assert result["inputs"] == {
        "annual_spend": 3000.0, "invested_assets": 5100.0, "annual_contribution": 75.0,
        "real_return": 0.05, "withdrawal_rate": 0.04, "max_years": 60,
    }
    assert result["fi_number"] == 75_000.0
    assert result["gap"] == 75_000.0 - 5100.0
    assert result["years_to_fi"] is not None and 40 < result["years_to_fi"] < 60
    assert result["trajectory"][0]["start"] == 5100.0
    assert result["trajectory_truncated"] is True  # capped at 40 rows
    assert "notes" in result


async def test_fire_projection_uses_explicit_inputs_and_reports_invalid_ones(
    session: AsyncSession, ctx: CallContext, test_user
):
    handler = REGISTRY["fire_projection"].handler
    result = await handler(
        session=session, ctx=ctx, annual_spend=40_000, invested_assets=200_000, annual_contribution=30_000,
    )
    assert result["derived"] == {}
    assert result["fi_number"] == 1_000_000.0
    assert result["progress"] == 0.2
    assert result["trajectory"][-1]["end"] >= 1_000_000.0

    bad = await handler(
        session=session, ctx=ctx, annual_spend=40_000, invested_assets=1, annual_contribution=1, withdrawal_rate=0,
    )
    assert "withdrawal_rate" in bad["error"]
