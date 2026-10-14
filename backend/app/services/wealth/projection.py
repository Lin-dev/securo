"""Deterministic $1M projection engine (plan P5; no Monte Carlo — see the plan's
Critique log S5, C8, C9: a 92-day-old dataset cannot support a confidence fan,
so the engine reports a low/base/high range from shifted returns instead).

Pure, no I/O: every function here takes plain numbers and dates and returns
plain numbers, dates or the `app.schemas.wealth` models that mirror them. The
DB-backed inputs (today's invested balance, the goal's target, typical saving)
are assembled by another slice; this module never imports the database, the
agents package or the MCP server.

The model, in one place since every function below leans on it:
  - Steps are monthly. The monthly rate is `(1 + r) ** (1/12) - 1` for a nominal
    annual return `r` — nominal throughout; the $1M target is a nominal dollar
    figure, never inflation-adjusted.
  - Contributions land at month end: `B_next = B * (1 + r_m) + c`. `c` may be
    negative (a drawdown) if typical saving is negative.
  - `B` starts at the invested balance; its target is `target_total -
    non_invested` (the invested balance alone has to cover whatever the target
    doesn't already come from). If `B >= target` at the start, that's 0 months.
  - The date is the first month index `n >= 1` with `B >= target`, capped at a
    360-month (30-year) horizon; beyond it the answer is `None`.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

from app.schemas.wealth import (
    FiFigures,
    LeverKey,
    LeverResult,
    Precision,
    ProjectionPathPoint,
    ProjectionScenario,
)

# P5: a 30-year cap. Beyond it the answer is reported as "not within 30 years"
# rather than as a specific (and increasingly speculative) date.
DEFAULT_HORIZON_MONTHS = 360
# P5: below 180 days of coverage, a monthly date overstates the data's precision.
QUARTER_PRECISION_MAX_DAYS = 179
# P5: low/base/high scenarios are the base annual return shifted by this much.
SCENARIO_RETURN_SHIFT = 0.03
# P5: the return-sensitivity levers shift the base return by this much.
LEVER_RETURN_SHIFT = 0.01
# P5: the chart shows at least a year past the projected date.
PATH_TAIL_MONTHS = 12
# The log-based closed form can land a hair off an exact month boundary purely
# from floating-point rounding; values this close (relative) to a whole month
# are treated as exactly that month instead of being ceiling-rounded up by one.
_BOUNDARY_EPSILON = 1e-9


@dataclass(frozen=True)
class ProjectionInputs:
    """Everything the engine needs for one run. `invested` excludes property and
    vehicles (P5); `non_invested` is the rest of net worth; `target_total` is
    the goal's dollar target (nominal); `monthly_contribution` is typical annual
    saving / 12 and may be negative; `annual_return` is nominal."""

    invested: float
    non_invested: float
    target_total: float
    monthly_contribution: float
    annual_return: float

    @property
    def target(self) -> float:
        """The amount the invested balance alone must reach."""
        return self.target_total - self.non_invested


def monthly_rate(annual_return: float) -> float:
    """The monthly-compounding rate equivalent to a nominal annual return."""
    return (1 + annual_return) ** (1 / 12) - 1


def _snap_to_whole_month(n_raw: float) -> int:
    """Ceiling-round a fractional month count, snapping values within a 1e-9
    relative tolerance of a whole number to that number first. Without this, a
    log-based n that floating-point rounding nudges to, say, 83.0000000001
    would ceil to 84 — one month later than the stepwise loop (which compares
    the balance to the target with a plain `>=`) finds for the same inputs."""
    nearest = round(n_raw)
    if abs(n_raw - nearest) <= _BOUNDARY_EPSILON * max(1.0, abs(nearest)):
        return max(int(nearest), 0)
    return math.ceil(n_raw)


def months_to_target(inputs: ProjectionInputs, horizon: int = DEFAULT_HORIZON_MONTHS) -> Optional[int]:
    """The first month `n >= 1` with the invested balance at or above target,
    by stepping the recurrence `B_next = B * (1 + r_m) + c` month by month.
    Already-there is 0 months; unreached by `horizon` months is `None`.

    A step that does not grow the balance while it is still short of target
    means the balance will never grow again: the recurrence is affine in `B`,
    so the sign of `B_next - B` cannot flip from one step to the next (either
    it keeps diverging away from a repelling equilibrium, or it is converging
    monotonically toward an attracting one — never crossing back). That lets
    this return `None` as soon as growth stalls, covering `c <= 0` directly
    and a return too negative for even a positive contribution to outrun.
    """
    balance = inputs.invested
    target = inputs.target
    if balance >= target:
        return 0
    rate = monthly_rate(inputs.annual_return)
    contribution = inputs.monthly_contribution
    for month in range(1, horizon + 1):
        next_balance = balance * (1 + rate) + contribution
        if next_balance <= balance and next_balance < target:
            return None
        balance = next_balance
        if balance >= target:
            return month
    return None


def closed_form_months(inputs: ProjectionInputs, horizon: int = DEFAULT_HORIZON_MONTHS) -> Optional[int]:
    """Solve the same recurrence for `n` directly, as a cross-check for
    `months_to_target` (P5).

    - `r_m == 0`: the plain division `ceil((T - B) / c)` (`None` when `c <= 0`).
    - `r_m > 0`: the usual annuity closed form,
      `n = ceil(log((T*r_m + c) / (B*r_m + c)) / log(1 + r_m))` — the same
      identity `app.services.fire_service.years_to_target` uses for yearly
      compounding. The denominator `B*r_m + c` is this month's change in
      balance; `<= 0` means the balance cannot grow from here, so `None`.
    - `r_m < 0`: there is no closed form worth deriving for a shrinking rate —
      `fire_service.years_to_target` makes the same choice for yearly
      compounding — so this walks `months_to_target` instead. The grid this
      is cross-checked against only exercises non-negative returns.
    """
    balance = inputs.invested
    target = inputs.target
    if balance >= target:
        return 0
    rate = monthly_rate(inputs.annual_return)
    contribution = inputs.monthly_contribution
    if rate == 0:
        if contribution <= 0:
            return None
        months = _snap_to_whole_month((target - balance) / contribution)
    elif rate > 0:
        denominator = balance * rate + contribution
        if denominator <= 0:
            return None
        numerator = target * rate + contribution
        months = _snap_to_whole_month(math.log(numerator / denominator) / math.log(1 + rate))
    else:
        return months_to_target(inputs, horizon)
    return months if months <= horizon else None


def balance_path(inputs: ProjectionInputs, months: int) -> list[float]:
    """The invested balance at month 0 (today) through `months`, inclusive —
    `months + 1` values, unrounded. Callers round to cents when they turn this
    into chart points (`ProjectionPathPoint`); keeping the raw path here lets
    other callers (such as a future lever comparison) compose it further."""
    rate = monthly_rate(inputs.annual_return)
    contribution = inputs.monthly_contribution
    path = [inputs.invested]
    balance = inputs.invested
    for _ in range(months):
        balance = balance * (1 + rate) + contribution
        path.append(balance)
    return path


def precision_for(coverage_days: int) -> Precision:
    """P5: below 180 days of coverage, a monthly date is false precision."""
    return "quarter" if coverage_days <= QUARTER_PRECISION_MAX_DAYS else "month"


def add_months(d: date, n: int) -> date:
    """The first day of the month that is `n` months after `d`'s month — the
    result is always the 1st, regardless of `d`'s own day of month."""
    total = d.year * 12 + (d.month - 1) + n
    year, month0 = divmod(total, 12)
    return date(year, month0 + 1, 1)


def label(d: date, precision: Precision) -> str:
    """An English label for a projected date — "Mar 2029" at month precision,
    "Q1 2029" at quarter precision. The frontend may re-format the date; this
    is only the string Securo itself writes into advice and summaries."""
    if precision == "month":
        return f"{calendar.month_abbr[d.month]} {d.year}"
    quarter = (d.month - 1) // 3 + 1
    return f"Q{quarter} {d.year}"


def _path_points(inputs: ProjectionInputs, as_of: date, months_to_chart: int) -> list[ProjectionPathPoint]:
    raw = balance_path(inputs, months_to_chart)
    return [
        ProjectionPathPoint(month=add_months(as_of, i), balance=round(bal, 2))
        for i, bal in enumerate(raw)
    ]


def scenarios(
    inputs: ProjectionInputs, as_of: date, precision: Precision, horizon: int = DEFAULT_HORIZON_MONTHS,
) -> dict[str, ProjectionScenario]:
    """Low/base/high: the same engine with `inputs.annual_return` (the base
    assumption) shifted by `SCENARIO_RETURN_SHIFT` (P5). Each path runs from
    month 0 to a year past its own projected date (`PATH_TAIL_MONTHS`), or the
    full horizon when the target isn't reached, so the chart always has
    somewhere to go."""
    result: dict[str, ProjectionScenario] = {}
    for key, shift in (("low", -SCENARIO_RETURN_SHIFT), ("base", 0.0), ("high", SCENARIO_RETURN_SHIFT)):
        scenario_inputs = replace(inputs, annual_return=inputs.annual_return + shift)
        months = months_to_target(scenario_inputs, horizon)
        months_to_chart = min(months + PATH_TAIL_MONTHS, horizon) if months is not None else horizon
        result_date = add_months(as_of, months) if months is not None else None
        result[key] = ProjectionScenario(
            annual_return=scenario_inputs.annual_return,
            months_to_target=months,
            date=result_date,
            label=label(result_date, precision) if result_date is not None else None,
            path=_path_points(scenario_inputs, as_of, months_to_chart),
        )
    return result


def levers(
    inputs: ProjectionInputs,
    as_of: date,
    precision: Precision,
    idle_cash: float,
    top_cut_monthly: Optional[float],
    horizon: int = DEFAULT_HORIZON_MONTHS,
) -> list[LeverResult]:
    """Each lever reruns the engine with exactly one input changed, reported
    against the base scenario (P5) — never against low/high. A lever with
    nothing to apply is left out rather than reported as a no-op: `idle_cash`
    needs idle cash to move, `cut_top` needs a positive monthly cut."""
    base_months = months_to_target(inputs, horizon)

    def result_for(key: LeverKey, lever_inputs: ProjectionInputs, amount: Optional[float]) -> LeverResult:
        months = months_to_target(lever_inputs, horizon)
        lever_date = add_months(as_of, months) if months is not None else None
        shift = months - base_months if months is not None and base_months is not None else None
        return LeverResult(
            key=key,
            amount=amount,
            months_to_target=months,
            date=lever_date,
            label=label(lever_date, precision) if lever_date is not None else None,
            shift_months=shift,
        )

    out: list[LeverResult] = []
    if idle_cash > 0:
        moved = replace(inputs, invested=inputs.invested + idle_cash, non_invested=inputs.non_invested - idle_cash)
        out.append(result_for("idle_cash", moved, idle_cash))
    out.append(result_for(
        "plus_500", replace(inputs, monthly_contribution=inputs.monthly_contribution + 500.0), 500.0,
    ))
    out.append(result_for(
        "plus_1000", replace(inputs, monthly_contribution=inputs.monthly_contribution + 1000.0), 1000.0,
    ))
    out.append(result_for(
        "plus_2000", replace(inputs, monthly_contribution=inputs.monthly_contribution + 2000.0), 2000.0,
    ))
    if top_cut_monthly is not None and top_cut_monthly > 0:
        out.append(result_for(
            "cut_top",
            replace(inputs, monthly_contribution=inputs.monthly_contribution + top_cut_monthly),
            top_cut_monthly,
        ))
    out.append(result_for(
        "return_plus_1", replace(inputs, annual_return=inputs.annual_return + LEVER_RETURN_SHIFT), None,
    ))
    out.append(result_for(
        "return_minus_1", replace(inputs, annual_return=inputs.annual_return - LEVER_RETURN_SHIFT), None,
    ))
    return out


def required_monthly_contribution(inputs: ProjectionInputs, months: int) -> Optional[float]:
    """The constant monthly contribution that lands exactly on `inputs.target`
    after `months` steps of the engine's own recurrence (`B_next = B * (1 +
    r_m) + c`), starting from `inputs.invested` at `inputs.annual_return` —
    `inputs.monthly_contribution` is ignored; this solves for the one that
    reaches the target, by inverting the future-value-of-an-annuity identity.

    `r_m == 0`: the plain division `(target - balance) / months`. `r_m != 0`:
    `c = (target - balance * (1 + r_m) ** months) / (((1 + r_m) ** months - 1)
    / r_m)` (the annuity factor in the denominator is this contribution's
    total future value per dollar; it is 0 only when `r_m == -1`, an input
    this engine never produces). The result may be negative (growth alone
    overshoots the target) — never clamped here, same as `monthly_contribution`
    elsewhere in this module. `months <= 0` has no well-defined contribution:
    `None`."""
    if months <= 0:
        return None
    rate = monthly_rate(inputs.annual_return)
    balance = inputs.invested
    target = inputs.target
    if rate == 0:
        return (target - balance) / months
    growth = (1 + rate) ** months
    annuity_factor = (growth - 1) / rate
    if annuity_factor == 0:
        return None
    return (target - balance * growth) / annuity_factor


def fi_figures(annual_expenses: Optional[float], withdrawal_rate: float) -> FiFigures:
    """The FI number in today's dollars (P5): typical annual expenses /
    withdrawal rate. Unlike the $1M target, this is never inflated to a nominal
    future figure — there's nothing to inflate; it's already today's spending.
    `None` expenses (too little history) gives a `None` FI number rather than
    a misleading one built on an assumed figure."""
    fi_number_real = annual_expenses / withdrawal_rate if annual_expenses is not None else None
    return FiFigures(
        fi_number_real=fi_number_real,
        annual_expenses_typical=annual_expenses,
        withdrawal_rate=withdrawal_rate,
    )
