"""FIRE arithmetic (fork addition, qc7) — pure functions."""
import pytest

from app.services.fire_service import (
    MAX_TRAJECTORY_ROWS,
    FireInputs,
    fi_number,
    project,
    years_to_target,
)


def test_fi_number_is_spend_over_withdrawal_rate():
    assert fi_number(40_000, 0.04) == 1_000_000
    assert fi_number(30, 0.04) == 750
    with pytest.raises(ValueError):
        fi_number(40_000, 0)


def test_years_to_target_closed_form_agrees_with_year_by_year_loop():
    b, c, r, t = 200_000, 30_000, 0.05, 1_000_000
    years = years_to_target(b, c, r, t)
    assert years is not None

    balance, n = b, 0
    while balance < t:
        balance = balance * (1 + r) + c
        n += 1
    assert n - 1 < years <= n


def test_years_to_target_edge_cases():
    assert years_to_target(1_000_000, 0, 0.05, 1_000_000) == 0.0  # already there
    assert years_to_target(0, 0, 0.0, 100) is None                # nothing ever moves
    assert years_to_target(0, 50, 0.0, 100) == 2.0                # linear when r = 0
    assert years_to_target(0, 0, 0.05, 100) is None               # no balance, no contribution
    assert years_to_target(50, 30, -0.05, 100) is not None        # contributions beat the decay
    assert years_to_target(50, 1, -0.05, 100) is None             # decay beats the contributions


def test_project_returns_trajectory_that_reaches_the_target():
    result = project(FireInputs(annual_spend=40_000, invested_assets=200_000, annual_contribution=30_000))

    assert result.fi_number == 1_000_000
    assert result.gap == 800_000
    assert result.progress == 0.2
    assert result.years_to_fi is not None
    assert result.trajectory[0]["start"] == 200_000
    assert result.trajectory[0]["end"] == round(200_000 * 1.05 + 30_000, 2)
    assert result.trajectory[-1]["end"] >= result.fi_number
    assert len(result.trajectory) == -(-result.years_to_fi // 1)  # ceil(years)
    assert result.trajectory_truncated is False
    assert result.assumptions["withdrawal_rate"] == 0.04
    assert result.to_dict()["fi_number"] == 1_000_000


def test_project_when_already_financially_independent():
    result = project(FireInputs(annual_spend=20_000, invested_assets=900_000, annual_contribution=0))
    assert result.fi_number == 500_000
    assert result.gap == 0
    assert result.progress == 1.0
    assert result.years_to_fi == 0.0
    assert result.trajectory == []


def test_project_truncates_long_trajectories_and_caps_years():
    slow = project(FireInputs(annual_spend=100_000, invested_assets=0, annual_contribution=1_000, real_return=0.0, max_years=60))
    assert slow.years_to_fi is None  # 2500 years, beyond max_years
    assert len(slow.trajectory) == MAX_TRAJECTORY_ROWS
    assert slow.trajectory_truncated is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"annual_spend": 0},
        {"withdrawal_rate": 0},
        {"withdrawal_rate": 0.5},
        {"real_return": 0.5},
        {"invested_assets": -1},
        {"annual_contribution": -1},
        {"max_years": 0},
    ],
)
def test_project_rejects_invalid_inputs(kwargs):
    base = {"annual_spend": 40_000, "invested_assets": 10_000, "annual_contribution": 1_000}
    with pytest.raises(ValueError):
        project(FireInputs(**{**base, **kwargs}))
