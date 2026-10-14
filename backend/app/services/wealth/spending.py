"""Spending patterns over monthly category totals."""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Optional

from app.services.wealth import policy
from app.services.wealth.patterns import NON_CATEGORY_RE


def month_start(d: date, back: int) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def category_spikes(month_cats: dict[date, dict[str, float]], month: date) -> Optional[list[dict[str, Any]]]:
    """Categories in `month` above 1.5x their usual level and at least 200 over it. The usual
    level is the mean of up to 6 complete months before, inside the data coverage; fewer than
    3 such months means too little history (None), never a guess."""
    base_months = [bm for bm in (month_start(month, k) for k in range(1, 7)) if bm in month_cats]
    if len(base_months) < 3 or month not in month_cats:
        return None
    out = []
    for cat, value in month_cats[month].items():
        if NON_CATEGORY_RE.match(cat or ""):
            continue
        series = [month_cats[bm].get(cat, 0.0) for bm in base_months]
        if sum(1 for x in series if x > 0) < 3:
            continue
        baseline = sum(series) / len(series)
        excess = value - baseline
        if value > policy.SPIKE_RATIO * baseline and excess >= policy.SPIKE_MIN_EXCESS:
            out.append({"category": cat, "amount": round(value, 2), "baseline": round(baseline, 2), "excess": round(excess, 2)})
    return sorted(out, key=lambda s: s["excess"], reverse=True)
