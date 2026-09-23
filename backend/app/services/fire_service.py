"""Financial-independence arithmetic (fork addition, qc7).

Pure functions, no database: the MCP tool derives the inputs from Securo's
data and the agent narrates the result. Everything is in real (after
inflation) terms, so `real_return` is the expected return net of inflation
and the spending figure keeps today's purchasing power.

Convention: contributions land at the end of each year, so the closed form
and the year-by-year trajectory agree exactly:
    end = start * (1 + r) + contribution
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

MAX_TRAJECTORY_ROWS = 40


@dataclass(frozen=True)
class FireInputs:
    annual_spend: float
    invested_assets: float
    annual_contribution: float
    real_return: float = 0.05
    withdrawal_rate: float = 0.04
    max_years: int = 60


@dataclass
class FireResult:
    fi_number: float
    gap: float
    progress: float
    years_to_fi: Optional[float]
    trajectory: list[dict[str, float]]
    trajectory_truncated: bool
    assumptions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate(inputs: FireInputs) -> None:
    if not inputs.annual_spend > 0:
        raise ValueError("annual_spend must be greater than zero")
    if inputs.invested_assets < 0:
        raise ValueError("invested_assets cannot be negative")
    if inputs.annual_contribution < 0:
        raise ValueError("annual_contribution cannot be negative")
    if not 0 < inputs.withdrawal_rate <= 0.2:
        raise ValueError("withdrawal_rate must be between 0 (exclusive) and 0.2")
    if not -0.1 <= inputs.real_return <= 0.2:
        raise ValueError("real_return must be between -0.1 and 0.2")
    if not 1 <= inputs.max_years <= 100:
        raise ValueError("max_years must be between 1 and 100")


def fi_number(annual_spend: float, withdrawal_rate: float) -> float:
    """The portfolio that sustains `annual_spend` at `withdrawal_rate`."""
    if not withdrawal_rate > 0:
        raise ValueError("withdrawal_rate must be greater than zero")
    return round(annual_spend / withdrawal_rate, 2)


def years_to_target(balance: float, contribution: float, rate: float, target: float) -> Optional[float]:
    """Years until `balance` reaches `target` — None when it never does.

    Closed form for a positive rate (end-of-year contributions):
        n = ln((T·r + c) / (b·r + c)) / ln(1 + r)
    A zero or negative real return is walked year by year, since the closed
    form has no answer when growth cannot carry the balance to the target.
    """
    if balance >= target:
        return 0.0
    if rate > 0:
        numerator = target * rate + contribution
        denominator = balance * rate + contribution
        if denominator <= 0:
            return None
        return round(math.log(numerator / denominator) / math.log(1 + rate), 2)
    if rate == 0:
        return round((target - balance) / contribution, 2) if contribution > 0 else None
    # rate < 0: the balance shrinks each year; only contributions can close the gap.
    b = balance
    for year in range(1, 1001):
        prev = b
        b = b * (1 + rate) + contribution
        if b >= target:
            # Interpolate inside the year for a fractional answer.
            return round(year - 1 + (target - prev) / (b - prev), 2)
        if b <= prev:
            return None
    return None


def project(inputs: FireInputs) -> FireResult:
    validate(inputs)
    target = fi_number(inputs.annual_spend, inputs.withdrawal_rate)
    gap = round(max(target - inputs.invested_assets, 0.0), 2)
    progress = round(min(inputs.invested_assets / target, 1.0), 4) if target > 0 else 1.0
    years = years_to_target(inputs.invested_assets, inputs.annual_contribution, inputs.real_return, target)
    if years is not None and years > inputs.max_years:
        years = None

    trajectory: list[dict[str, float]] = []
    truncated = False
    balance = inputs.invested_assets
    if balance < target:
        for year in range(1, inputs.max_years + 1):
            growth = balance * inputs.real_return
            end = balance + growth + inputs.annual_contribution
            if len(trajectory) >= MAX_TRAJECTORY_ROWS:
                truncated = True
                break
            trajectory.append({
                "year": year,
                "start": round(balance, 2),
                "contribution": round(inputs.annual_contribution, 2),
                "growth": round(growth, 2),
                "end": round(end, 2),
            })
            balance = end
            if end >= target:
                break

    return FireResult(
        fi_number=target,
        gap=gap,
        progress=progress,
        years_to_fi=years,
        trajectory=trajectory,
        trajectory_truncated=truncated,
        assumptions={
            **asdict(inputs),
            "contribution_timing": "end of year",
            "real_return_means": "expected return after inflation; spending stays in today's money",
            "fi_rule": "fi_number = annual_spend / withdrawal_rate",
        },
    )
