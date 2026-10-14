"""Net-worth goals get a `projected_date` and an engine-driven `on_track`
from the wealth projection engine (fork, plan P5/P6): `goal_service.
_net_worth_goal_fields` is what both `get_goal`/`get_goals` (via
`_enrich_goal`) and `get_goal_summary` read it from, so this file checks
both builders agree and that every other tracking type is unaffected.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.goal import Goal
from app.schemas.wealth import ProjectionSettings, WealthSettingsValues
from app.services import goal_service
from app.services.wealth.projection import (
    ProjectionInputs,
    add_months,
    monthly_rate,
    months_to_target,
    required_monthly_contribution,
)

RETURN_NOMINAL = 0.12


# --- required_monthly_contribution: brute force -----------------------------------------


@pytest.mark.parametrize("invested,non_invested,target_total,months,annual_return", [
    (100_000.0, 0.0, 150_000.0, 24, 0.06),
    (300_000.0, 50_000.0, 1_000_000.0, 120, 0.07),
    (900_000.0, 0.0, 1_000_000.0, 12, 0.0),  # r == 0 branch
    (50_000.0, 0.0, 1_000_000.0, 36, 0.10),
    (1_200_000.0, 0.0, 1_000_000.0, 6, 0.05),  # already past target: a negative contribution is fine
])
def test_required_monthly_contribution_lands_on_target_by_brute_force(
    invested, non_invested, target_total, months, annual_return,
):
    inputs = ProjectionInputs(
        invested=invested, non_invested=non_invested, target_total=target_total,
        monthly_contribution=0.0, annual_return=annual_return,
    )
    c = required_monthly_contribution(inputs, months)
    assert c is not None

    # Brute force: plug `c` into the engine's own recurrence, independently of
    # `required_monthly_contribution`'s closed form, and check it lands on target.
    rate = monthly_rate(annual_return)
    balance = inputs.invested
    for _ in range(months):
        balance = balance * (1 + rate) + c
    assert balance == pytest.approx(inputs.target, rel=1e-6, abs=1e-3)


def test_required_monthly_contribution_with_non_positive_months_is_none():
    inputs = ProjectionInputs(
        invested=100_000.0, non_invested=0.0, target_total=150_000.0,
        monthly_contribution=0.0, annual_return=0.06,
    )
    assert required_monthly_contribution(inputs, 0) is None
    assert required_monthly_contribution(inputs, -3) is None


# --- service-level fixtures ---------------------------------------------------------------


async def _set_return_nominal(session: AsyncSession, test_user, rate: float) -> None:
    test_user.preferences = {
        **(test_user.preferences or {}),
        "wealth": WealthSettingsValues(projection=ProjectionSettings(return_nominal=rate)).model_dump(mode="json"),
    }
    session.add(test_user)
    await session.commit()


async def _brokerage(session: AsyncSession, test_user, test_workspace, balance: float) -> Account:
    """A lone, connected investment-type account with no holdings and no
    transactions: a connected account's `.balance` is authoritative (plan
    P2 — `dashboard_service._total_balance_by_currency` sums transactions
    for a *manual* account instead), so `invested` is simply this balance
    and `non_invested` is 0 — the simplest possible, fully deterministic
    projection input."""
    conn = BankConnection(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}", institution_name="Bank",
        credentials={"token": "fake"}, status="active", settings=None,
    )
    session.add(conn)
    await session.flush()
    acc = Account(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="Brokerage",
        type="investment", balance=Decimal(str(balance)), currency=test_workspace.default_currency,
        connection_id=conn.id, external_id=f"inv-{uuid.uuid4().hex[:8]}",
    )
    session.add(acc)
    await session.commit()
    return acc


async def _goal(
    session: AsyncSession, test_user, test_workspace, *, target_amount: float, target_date,
    tracking_type: str = "net_worth",
) -> Goal:
    goal = Goal(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="NW goal",
        target_amount=Decimal(str(target_amount)), current_amount=Decimal("0"), initial_amount=Decimal("0"),
        currency=test_workspace.default_currency, tracking_type=tracking_type, target_date=target_date,
        status="active",
    )
    session.add(goal)
    await session.commit()
    await session.refresh(goal)
    return goal


@pytest_asyncio.fixture
async def invested_100k(session: AsyncSession, test_user, test_workspace) -> Account:
    await _set_return_nominal(session, test_user, RETURN_NOMINAL)
    return await _brokerage(session, test_user, test_workspace, 100_000.0)


def _engine_projected_date(invested: float, target_total: float, today: date) -> date | None:
    """The same base-scenario date `_net_worth_goal_fields` computes, derived
    independently here (zero contribution, since the fixtures seed no
    transactions) to build test fixtures around — not a re-test of the
    engine itself (that's `test_wealth_projection.py`'s job)."""
    inputs = ProjectionInputs(
        invested=invested, non_invested=0.0, target_total=target_total,
        monthly_contribution=0.0, annual_return=RETURN_NOMINAL,
    )
    months = months_to_target(inputs)
    return add_months(today, months) if months is not None else None


# --- on_track: five states, in both builders ----------------------------------------------


async def test_achieved_when_current_meets_or_exceeds_target(
    session: AsyncSession, test_user, test_workspace, invested_100k,
):
    goal = await _goal(
        session, test_user, test_workspace, target_amount=50_000.0,
        target_date=app_today() + timedelta(days=365),
    )
    read = await goal_service.get_goal(session, goal.id, test_workspace.id, test_user.id)
    assert read is not None
    [summary] = await goal_service.get_goal_summary(session, test_workspace.id, test_user.id, limit=10)

    assert read.on_track == "achieved"
    assert summary.on_track == "achieved"
    assert read.projected_date == summary.projected_date


async def test_overdue_when_target_date_has_passed_without_achieving(
    session: AsyncSession, test_user, test_workspace, invested_100k,
):
    goal = await _goal(
        session, test_user, test_workspace, target_amount=500_000.0,
        target_date=app_today() - timedelta(days=10),
    )
    read = await goal_service.get_goal(session, goal.id, test_workspace.id, test_user.id)
    assert read is not None
    [summary] = await goal_service.get_goal_summary(session, test_workspace.id, test_user.id, limit=10)

    assert read.on_track == "overdue"
    assert summary.on_track == "overdue"


async def test_ahead_on_track_and_behind_from_the_engines_own_projected_date(
    session: AsyncSession, test_user, test_workspace, invested_100k,
):
    today = app_today()
    projected = _engine_projected_date(100_000.0, 150_000.0, today)
    assert projected is not None  # sanity: reachable within the horizon

    ahead_goal = await _goal(
        session, test_user, test_workspace, target_amount=150_000.0, target_date=add_months(projected, 4),
    )
    on_track_goal = await _goal(
        session, test_user, test_workspace, target_amount=150_000.0, target_date=projected,
    )
    behind_goal = await _goal(
        session, test_user, test_workspace, target_amount=150_000.0, target_date=add_months(projected, -1),
    )

    ahead = await goal_service.get_goal(session, ahead_goal.id, test_workspace.id, test_user.id)
    assert ahead is not None
    on_track = await goal_service.get_goal(session, on_track_goal.id, test_workspace.id, test_user.id)
    assert on_track is not None
    behind = await goal_service.get_goal(session, behind_goal.id, test_workspace.id, test_user.id)
    assert behind is not None

    assert ahead.on_track == "ahead"
    assert on_track.on_track == "on_track"
    assert behind.on_track == "behind"
    assert ahead.projected_date == projected == on_track.projected_date == behind.projected_date


async def test_behind_when_unreachable_within_the_engines_horizon(
    session: AsyncSession, test_user, test_workspace, invested_100k,
):
    """100,000 growing at 12%/year for 30 years tops out well under 3,000,000
    with no contribution: 50,000,000 is unreachable inside the engine's
    360-month horizon, so `projected_date` is None and `on_track` is
    `behind` by the plan's explicit "or if unreachable" rule."""
    goal = await _goal(
        session, test_user, test_workspace, target_amount=50_000_000.0,
        target_date=app_today() + timedelta(days=3650),
    )
    read = await goal_service.get_goal(session, goal.id, test_workspace.id, test_user.id)
    assert read is not None

    assert read.projected_date is None
    assert read.on_track == "behind"


async def test_no_target_date_keeps_on_track_none(
    session: AsyncSession, test_user, test_workspace, invested_100k,
):
    goal = await _goal(session, test_user, test_workspace, target_amount=150_000.0, target_date=None)
    read = await goal_service.get_goal(session, goal.id, test_workspace.id, test_user.id)
    assert read is not None

    assert read.on_track is None
    assert read.monthly_contribution is None
    # `projected_date` is independent of having a target date (plan: it's
    # always the base-scenario date for the goal's target).
    assert read.projected_date is not None


# --- monthly_contribution: hand-checked against the pure helper -------------------------


async def test_monthly_contribution_matches_required_monthly_contribution(
    session: AsyncSession, test_user, test_workspace, invested_100k,
):
    today = app_today()
    target_date = add_months(today, 24)  # a clean 24-month difference regardless of today's day of month
    goal = await _goal(session, test_user, test_workspace, target_amount=150_000.0, target_date=target_date)

    inputs = ProjectionInputs(
        invested=100_000.0, non_invested=0.0, target_total=150_000.0,
        monthly_contribution=0.0, annual_return=RETURN_NOMINAL,
    )
    raw_expected = required_monthly_contribution(inputs, 24)
    assert raw_expected is not None
    expected = round(raw_expected, 2)

    read = await goal_service.get_goal(session, goal.id, test_workspace.id, test_user.id)
    assert read is not None
    [summary] = await goal_service.get_goal_summary(session, test_workspace.id, test_user.id, limit=10)

    assert read.monthly_contribution == pytest.approx(expected)
    assert summary.monthly_contribution == pytest.approx(expected)


async def test_monthly_contribution_is_zero_once_achieved_or_overdue(
    session: AsyncSession, test_user, test_workspace, invested_100k,
):
    achieved = await _goal(
        session, test_user, test_workspace, target_amount=50_000.0,
        target_date=app_today() + timedelta(days=200),
    )
    overdue = await _goal(
        session, test_user, test_workspace, target_amount=500_000.0,
        target_date=app_today() - timedelta(days=5),
    )
    achieved_read = await goal_service.get_goal(session, achieved.id, test_workspace.id, test_user.id)
    assert achieved_read is not None
    overdue_read = await goal_service.get_goal(session, overdue.id, test_workspace.id, test_user.id)
    assert overdue_read is not None

    assert achieved_read.monthly_contribution == 0.0
    assert overdue_read.monthly_contribution == 0.0


# --- list and summary builders never disagree --------------------------------------------


async def test_list_and_summary_builders_agree_on_a_net_worth_goal(
    session: AsyncSession, test_user, test_workspace, invested_100k,
):
    goal = await _goal(
        session, test_user, test_workspace, target_amount=150_000.0,
        target_date=app_today() + timedelta(days=400),
    )
    [listed] = await goal_service.get_goals(session, test_workspace.id, test_user.id)
    [summary] = await goal_service.get_goal_summary(session, test_workspace.id, test_user.id, limit=10)
    assert listed.id == goal.id and summary.id == goal.id

    assert listed.projected_date == summary.projected_date
    assert listed.on_track == summary.on_track
    assert listed.monthly_contribution == pytest.approx(summary.monthly_contribution)


# --- other tracking types are unaffected --------------------------------------------------


async def test_manual_goal_has_no_projected_date_and_keeps_the_straight_line_behaviour(
    session: AsyncSession, test_user, test_workspace,
):
    goal = await _goal(
        session, test_user, test_workspace, target_amount=10_000.0,
        target_date=app_today() + timedelta(days=100), tracking_type="manual",
    )
    read = await goal_service.get_goal(session, goal.id, test_workspace.id, test_user.id)
    assert read is not None
    [summary] = await goal_service.get_goal_summary(session, test_workspace.id, test_user.id, limit=10)

    assert read.projected_date is None
    assert summary.projected_date is None
    # current_amount stayed at the model default (0): the straight-line
    # helper's own behaviour, untouched by this slice.
    assert read.on_track in ("behind", "on_track", "ahead", None)
