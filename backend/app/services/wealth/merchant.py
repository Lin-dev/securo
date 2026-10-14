"""The same merchant across noisy descriptions, and charges that repeat every month."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any

from app.services.wealth.spending import month_start

MERCHANT_NOISE_RE = re.compile(r"[#*]|\b\d[\d/.\-:]*\b")
SPACES_RE = re.compile(r"\s+")


def merchant_key(description: str) -> str:
    """Grouping key for "the same merchant" across noisy descriptions.

    Same normalization the rule engine matches with (upper case, accents
    stripped), then card references, dates and ids dropped and spaces
    collapsed. Not guaranteed to be a substring of the description — use
    `suggest_pattern` for a value that a `contains` rule can carry.
    """
    from app.services.rule_engine import _normalize

    s = _normalize(description or "")
    s = MERCHANT_NOISE_RE.sub(" ", s)
    s = SPACES_RE.sub(" ", s).strip()
    return s[:60] or "(blank)"


def detect_recurring(rows: list[tuple[date, str, float]], today: date) -> list[dict[str, Any]]:
    """Charges from the same merchant in at least 3 of the last 4 complete months, at most 2 a
    month, each month within 15% (or 2.00) of the median. Figures only; no rule uses them."""
    months = [month_start(today, k) for k in range(1, 5)]
    by_merchant: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
    for when, desc, amount in rows:
        ms = date(when.year, when.month, 1)
        if ms in months:
            by_merchant[merchant_key(desc or "")[:40] or "(blank)"][ms].append(amount)
    out = []
    for merchant, per_month in by_merchant.items():
        if len(per_month) < 3 or any(len(v) > 2 for v in per_month.values()):
            continue
        totals = sorted(sum(v) for v in per_month.values())
        median = totals[len(totals) // 2]
        if all(abs(t - median) <= max(0.15 * median, 2.0) for t in totals):
            out.append({"merchant": merchant, "monthly": round(median, 2), "months": len(per_month)})
    return sorted(out, key=lambda r: r["monthly"], reverse=True)
