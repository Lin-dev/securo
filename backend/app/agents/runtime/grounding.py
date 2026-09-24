"""Numeric grounding check for guided-mode narrations.

Guided mode computes every figure in code (the "figures pack") and asks the
model only to narrate them. This module verifies that claim after the fact:
every number the narration mentions must correspond to a value in the pack.
A local model that "helpfully" annualises, averages or rounds a figure into
something new is caught here, and the caller regenerates or falls back to a
templated sentence.

Pure functions, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Things that look like numbers but are not figures: ISO dates, ISO weeks,
# clock times and bare 4-digit years.
_NOT_A_FIGURE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"          # 2026-09-01
    r"|\b\d{4}-\d{2}\b"                # 2026-09
    r"|\b\d{4}-W\d{2}\b"               # 2026-W38
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"   # 07:30, 07:30:15
    r"|\b(?:19|20)\d{2}\b"             # 1999, 2026
)

# A number: optional minus (ASCII or U+2212), digits with `.`/`,` groups, or a
# space / nbsp used as a thousands separator only when exactly three digits
# follow; optional `k` multiplier; optional `%`.
NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"[-−]?\d+(?:(?:[.,]|[  ](?=\d{3}(?!\d)))\d+)*"
    r"(?:\s?[kK](?![A-Za-z]))?"
    r"(?:\s?%)?"
)

_SMALL_INT_LIMIT = 12


@dataclass(frozen=True)
class NumberToken:
    raw: str
    candidates: tuple[float, ...]
    is_percent: bool


def _candidates(body: str) -> list[float]:
    """All plausible readings of a numeric string given ambiguous separators."""
    s = body.replace(" ", "").replace(" ", "").replace("−", "-")
    has_dot, has_comma = "." in s, "," in s
    out: list[float] = []
    try:
        if has_dot and has_comma:
            # The last separator is the decimal mark; the other one groups thousands.
            if s.rfind(".") > s.rfind(","):
                out.append(float(s.replace(",", "")))
            else:
                out.append(float(s.replace(".", "").replace(",", ".")))
        elif has_comma:
            out.append(float(s.replace(",", "")))       # 5,234 -> 5234
            out.append(float(s.replace(",", ".")))      # 1,5   -> 1.5
        elif has_dot:
            out.append(float(s))                        # 27.5
            out.append(float(s.replace(".", "")))       # 5.234 -> 5234
        else:
            out.append(float(s))
    except ValueError:
        return []
    return out


def extract_numbers(text: str) -> list[NumberToken]:
    cleaned = _NOT_A_FIGURE_RE.sub(" ", text or "")
    tokens: list[NumberToken] = []
    for m in NUMBER_RE.finditer(cleaned):
        raw = m.group(0)
        is_percent = raw.rstrip().endswith("%")
        body = raw.rstrip("%").rstrip()
        has_k = body[-1:] in ("k", "K")
        if has_k:
            body = body[:-1].rstrip()
        plain_int = not is_percent and not has_k and not any(ch in body for ch in ".,  ")
        if plain_int:
            try:
                if abs(int(body.replace("−", "-"))) <= _SMALL_INT_LIMIT:
                    continue  # ordinals, "10 categories", "12 months"
            except ValueError:
                pass
        cands = _candidates(body)
        if has_k:
            cands = [c * 1000 for c in cands]
        if is_percent:
            cands = cands + [c / 100 for c in cands]
        if cands:
            tokens.append(NumberToken(raw=raw.strip(), candidates=tuple(cands), is_percent=is_percent))
    return tokens


def pack_values(pack: Any) -> set[float]:
    """Every numeric leaf of the pack, its negation and its ×100 (rates), plus
    the length of every list (so "12 months" or "5 accounts" stays grounded)."""
    values: set[float] = set()

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            v = float(node)
            for x in (v, -v, v * 100.0):
                values.add(round(x, 4))
            return
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
            return
        if isinstance(node, (list, tuple)):
            values.add(float(len(node)))
            for v in node:
                walk(v)

    walk(pack)
    return values


def _matches(candidate: float, values: set[float], *, rel_tol: float, abs_tol: float) -> bool:
    """Relative tolerance everywhere (rounding a money figure to the nearest
    unit is fine); the absolute tolerance only covers money-sized values, so a
    fraction such as 0.28 cannot pass for 0.2748."""
    for v in values:
        tol = rel_tol * abs(v)
        if abs(v) >= 1.0:
            tol = max(tol, abs_tol)
        if abs(candidate - v) <= tol:
            return True
    return False


def ungrounded(text: str, pack: Any, *, rel_tol: float = 0.005, abs_tol: float = 0.05) -> list[str]:
    """Return the raw number tokens in `text` that do not match any pack value."""
    values = pack_values(pack)
    offenders: list[str] = []
    for tok in extract_numbers(text):
        if any(_matches(c, values, rel_tol=rel_tol, abs_tol=abs_tol) for c in tok.candidates):
            continue
        if tok.raw not in offenders:
            offenders.append(tok.raw)
    return offenders
