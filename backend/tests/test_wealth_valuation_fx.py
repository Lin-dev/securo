"""Past net-worth points never ask the FX provider for a missing rate (fork, plan P2)."""
from datetime import timedelta

from app.core.app_clock import app_today
from app.services.wealth import valuation


def test_only_today_may_fetch_a_rate():
    today = app_today()
    assert valuation._may_fetch(today) is True
    assert valuation._may_fetch(today - timedelta(days=1)) is False
    assert valuation._may_fetch(today.replace(day=1) - timedelta(days=1)) is False
