"""Deterministic projection engine (plan P5; fork). No Monte Carlo — see the
plan's Critique log S5, C8, C9 for why: a 92-day-old dataset cannot support a
confidence fan, so the engine is cross-checked against a closed-form annuity
instead. No timing assertions anywhere in this file (P5/P11 do not ask for
them, and they would be flaky in CI)."""
from __future__ import annotations

import itertools
from datetime import date

import pytest

from app.schemas.wealth import FiFigures
from app.services.wealth.projection import (
    DEFAULT_HORIZON_MONTHS,
    ProjectionInputs,
    add_months,
    balance_path,
    closed_form_months,
    fi_figures,
    label,
    levers,
    monthly_rate,
    months_to_target,
    precision_for,
    scenarios,
)

AS_OF = date(2026, 10, 14)

# --- the grid: months_to_target (the stepwise loop) vs closed_form_months ------

_GRID = [
    ProjectionInputs(invested=invested, non_invested=non_invested, target_total=1_000_000.0,
                      monthly_contribution=contribution, annual_return=annual_return)
    for invested, non_invested, contribution, annual_return in itertools.product(
        [0.0, 1e5, 4e5, 9.9e5], [0.0, 1e5], [-1000.0, 0.0, 500.0, 4000.0], [0.0, 0.03, 0.06, 0.1],
    )
]


@pytest.mark.parametrize("inputs", _GRID)
def test_loop_agrees_with_closed_form_over_the_grid(inputs):
    """`months_to_target` (the stepwise loop) and `closed_form_months` (its
    algebraic cross-check, P5) must agree exactly over the whole grid — no
    fuzzy "off by one is fine" tolerance here.

    The one real source of disagreement would be a log-based month count
    landing a hair off an exact whole number purely from floating-point
    rounding, which `math.ceil` would then round the wrong way relative to
    the loop's plain `>=` comparison. Both engine functions already guard
    this by snapping anything within a 1e-9 relative tolerance of a whole
    month to that whole month before taking the ceiling (see
    `_snap_to_whole_month` in `app.services.wealth.projection`), so the
    comparison below is a plain `==` rather than a tolerance in the test
    itself — and this grid (128 combinations, each run to a 360-month cap)
    confirms it holds with no exceptions.
    """
    assert months_to_target(inputs) == closed_form_months(inputs)


# --- already reached / unreachable ---------------------------------------------


def test_already_reached_is_zero_months():
    inputs = ProjectionInputs(invested=1_100_000, non_invested=0, target_total=1_000_000,
                               monthly_contribution=1000, annual_return=0.06)
    assert months_to_target(inputs) == 0
    assert closed_form_months(inputs) == 0


def test_unreachable_with_a_non_positive_contribution():
    inputs = ProjectionInputs(invested=100_000, non_invested=0, target_total=1_000_000,
                               monthly_contribution=-500, annual_return=0.0)
    assert months_to_target(inputs) is None
    assert closed_form_months(inputs) is None


def test_unreachable_beyond_the_horizon():
    # $10/month with no growth needs 100,000 months to close a $1M gap.
    inputs = ProjectionInputs(invested=0, non_invested=0, target_total=1_000_000,
                               monthly_contribution=10, annual_return=0.0)
    assert months_to_target(inputs) is None
    assert closed_form_months(inputs) is None


def test_negative_contribution_still_reaches_the_target_through_returns():
    """A drawdown (c < 0) is not automatically unreachable: a balance well
    above the equilibrium point where growth exactly offsets the drawdown
    keeps compounding upward despite it."""
    inputs = ProjectionInputs(invested=900_000, non_invested=0, target_total=1_000_000,
                               monthly_contribution=-1000, annual_return=0.08)
    loop_months = months_to_target(inputs)
    assert loop_months is not None
    assert loop_months == closed_form_months(inputs)
    # independent check: a plain loop written separately from the engine
    rate = (1.08) ** (1 / 12) - 1
    balance, n = 900_000.0, 0
    while balance < 1_000_000:
        balance = balance * (1 + rate) - 1000
        n += 1
    assert n == loop_months


# --- determinism -----------------------------------------------------------------


def test_determinism_same_inputs_same_output():
    inputs = ProjectionInputs(invested=250_000, non_invested=75_000, target_total=1_000_000,
                               monthly_contribution=2200, annual_return=0.055)
    assert months_to_target(inputs) == months_to_target(inputs)
    assert closed_form_months(inputs) == closed_form_months(inputs)
    assert balance_path(inputs, 24) == balance_path(inputs, 24)
    assert scenarios(inputs, AS_OF, "month") == scenarios(inputs, AS_OF, "month")


# --- levers ------------------------------------------------------------------------

_LEVER_BASE = ProjectionInputs(invested=300_000, non_invested=50_000, target_total=1_000_000,
                                monthly_contribution=1500, annual_return=0.06)


def test_lever_signs_idle_cash_and_plus_are_never_later_than_base():
    base_months = months_to_target(_LEVER_BASE)
    results = levers(_LEVER_BASE, AS_OF, "month", idle_cash=20_000, top_cut_monthly=300)
    by_key = {r.key: r for r in results}
    for key in ("idle_cash", "plus_500", "plus_1000", "plus_2000", "cut_top"):
        lever = by_key[key]
        assert lever.months_to_target is not None and base_months is not None
        assert lever.months_to_target <= base_months
        assert lever.shift_months is not None and lever.shift_months <= 0


def test_lever_sign_return_minus_1_is_never_earlier():
    base_months = months_to_target(_LEVER_BASE)
    results = levers(_LEVER_BASE, AS_OF, "month", idle_cash=0, top_cut_monthly=None)
    minus = next(r for r in results if r.key == "return_minus_1")
    assert minus.months_to_target is not None and base_months is not None
    assert minus.months_to_target >= base_months
    assert minus.shift_months is not None and minus.shift_months >= 0


def test_levers_report_the_dollar_amount_each_changes():
    results = levers(_LEVER_BASE, AS_OF, "month", idle_cash=20_000, top_cut_monthly=300)
    by_key = {r.key: r.amount for r in results}
    assert by_key == {
        "idle_cash": 20_000.0, "plus_500": 500.0, "plus_1000": 1000.0, "plus_2000": 2000.0,
        "cut_top": 300.0, "return_plus_1": None, "return_minus_1": None,
    }


def test_idle_cash_and_cut_top_levers_are_omitted_when_there_is_nothing_to_apply():
    results = levers(_LEVER_BASE, AS_OF, "month", idle_cash=0, top_cut_monthly=None)
    keys = {r.key for r in results}
    assert "idle_cash" not in keys
    assert "cut_top" not in keys
    assert keys == {"plus_500", "plus_1000", "plus_2000", "return_plus_1", "return_minus_1"}

    negative = levers(_LEVER_BASE, AS_OF, "month", idle_cash=-100, top_cut_monthly=-5)
    assert {r.key for r in negative} == {"plus_500", "plus_1000", "plus_2000", "return_plus_1", "return_minus_1"}


def test_shift_months_is_lever_minus_base_and_none_when_either_side_is_none():
    results = levers(_LEVER_BASE, AS_OF, "month", idle_cash=20_000, top_cut_monthly=300)
    base_months = months_to_target(_LEVER_BASE)
    for lever in results:
        if lever.months_to_target is None or base_months is None:
            assert lever.shift_months is None
        else:
            assert lever.shift_months == lever.months_to_target - base_months

    # a base scenario too weak to reach $1M within the horizon, where a lever
    # still reaches it: shift_months must be None (the base side is None).
    weak_base = ProjectionInputs(invested=50_000, non_invested=0, target_total=1_000_000,
                                  monthly_contribution=50, annual_return=0.062)
    assert months_to_target(weak_base) is None
    weak_results = levers(weak_base, AS_OF, "month", idle_cash=0, top_cut_monthly=None)
    plus_2000 = next(r for r in weak_results if r.key == "plus_2000")
    assert plus_2000.months_to_target is not None
    assert plus_2000.shift_months is None


# --- scenarios ---------------------------------------------------------------------


def test_scenario_returns_are_base_mp_3_points():
    inputs = ProjectionInputs(invested=300_000, non_invested=50_000, target_total=1_000_000,
                               monthly_contribution=1500, annual_return=0.06)
    result = scenarios(inputs, AS_OF, "month")
    assert result["low"].annual_return == pytest.approx(0.03)
    assert result["base"].annual_return == pytest.approx(0.06)
    assert result["high"].annual_return == pytest.approx(0.09)


def test_scenario_path_covers_a_year_past_the_projected_date():
    inputs = ProjectionInputs(invested=300_000, non_invested=50_000, target_total=1_000_000,
                               monthly_contribution=1500, annual_return=0.06)
    result = scenarios(inputs, AS_OF, "month")
    base = result["base"]
    assert base.months_to_target is not None
    assert len(base.path) == min(base.months_to_target + 12, DEFAULT_HORIZON_MONTHS) + 1
    assert base.path[0].month == AS_OF.replace(day=1)
    assert base.path[0].balance == pytest.approx(300_000.0)


# --- precision, labels, add_months -------------------------------------------------


def test_precision_boundary_is_180_days():
    assert precision_for(179) == "quarter"
    assert precision_for(180) == "month"


@pytest.mark.parametrize("d, precision, expected", [
    (date(2029, 3, 1), "month", "Mar 2029"),
    (date(2029, 3, 15), "month", "Mar 2029"),
    (date(2029, 1, 1), "quarter", "Q1 2029"),
    (date(2029, 4, 1), "quarter", "Q2 2029"),
    (date(2029, 7, 1), "quarter", "Q3 2029"),
    (date(2029, 12, 31), "quarter", "Q4 2029"),
])
def test_label_formats(d, precision, expected):
    assert label(d, precision) == expected


@pytest.mark.parametrize("start, n, expected", [
    (date(2026, 10, 14), 0, date(2026, 10, 1)),
    (date(2026, 11, 10), 2, date(2027, 1, 1)),
    (date(2026, 12, 5), 1, date(2027, 1, 1)),
    (date(2026, 1, 31), 12, date(2027, 1, 1)),
    (date(2026, 10, 14), 150, date(2039, 4, 1)),
])
def test_add_months_across_year_ends(start, n, expected):
    assert add_months(start, n) == expected


# --- fi_figures ----------------------------------------------------------------


def test_fi_figures_computes_the_real_number():
    result = fi_figures(annual_expenses=120_000, withdrawal_rate=0.04)
    assert result == FiFigures(fi_number_real=3_000_000.0, annual_expenses_typical=120_000, withdrawal_rate=0.04)


def test_fi_figures_with_no_expenses_figure_yet():
    result = fi_figures(annual_expenses=None, withdrawal_rate=0.04)
    assert result.fi_number_real is None
    assert result.annual_expenses_typical is None
    assert result.withdrawal_rate == 0.04


# --- the hand-computed example --------------------------------------------------


def test_hand_computed_example_400k_invested_100k_cash_3k_per_month_6pct():
    """invested $400,000, non_invested $100,000 (so the invested balance alone
    must cover $900,000 of the $1,000,000 target), contribution $3,000/month,
    nominal return 6%. Expected month count by an independent method: a plain
    loop written separately from the engine, not by calling it."""
    inputs = ProjectionInputs(invested=400_000, non_invested=100_000, target_total=1_000_000,
                               monthly_contribution=3000, annual_return=0.06)

    rate = (1.06) ** (1 / 12) - 1
    balance, expected_months = 400_000.0, 0
    while balance < 900_000.0:
        balance = balance * (1 + rate) + 3000
        expected_months += 1

    assert expected_months == 83
    assert months_to_target(inputs) == expected_months
    assert closed_form_months(inputs) == expected_months
    assert add_months(AS_OF, expected_months) == date(2033, 9, 1)
    assert label(add_months(AS_OF, expected_months), "month") == "Sep 2033"


def test_monthly_rate_matches_the_nominal_annual_compounding_identity():
    rate = monthly_rate(0.06)
    assert (1 + rate) ** 12 == pytest.approx(1.06)
