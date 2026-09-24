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
NUMBER_RE = re.compile(r"(?<![\w.])[-−]?\d(?:[\d.,\u00a0\u202f\u2009 ]*\d)?(?:\s*[kK](?![\w]))?(?:[\s\u00a0\u202f\u2009]*%)?")

_SMALL_INT_LIMIT = 12


@dataclass(frozen=True)
class NumberToken:
    raw: str
    candidates: tuple[float, ...]
    is_percent: bool
    # half of the last written decimal place, per candidate: the narration may
    # round a figure to the precision it writes, but every digit it writes must
    # be right ("15,614" may stand for 15,614.39; "11,781.20" may not stand for 11,781.43)
    tolerances: tuple[float, ...] = ()


def _decimals(digits: str) -> int:
    return len(digits.split(".", 1)[1]) if "." in digits else 0


def _candidates(body: str) -> list[tuple[float, int]]:
    """All plausible readings of a numeric string given ambiguous separators,
    each with the number of decimals that reading was written with."""
    s = body.replace(" ", "").replace("\u00a0", "").replace("−", "-")
    has_dot, has_comma = "." in s, "," in s
    out: list[tuple[float, int]] = []
    try:
        if has_dot and has_comma:
            # The last separator is the decimal mark; the other one groups thousands.
            norm = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
            out.append((float(norm), _decimals(norm)))
        elif has_comma:
            out.append((float(s.replace(",", "")), 0))                       # 5,234 -> 5234
            norm = s.replace(",", ".")
            out.append((float(norm), _decimals(norm)))  # 1,5 -> 1.5
        elif has_dot:
            out.append((float(s), _decimals(s)))                             # 27.5
            out.append((float(s.replace(".", "")), 0))                       # 5.234 -> 5234
        else:
            out.append((float(s), 0))
    except ValueError:
        return []
    return out


_NAMED_NUMBERS_RE = re.compile(
    r"\b\d{3}\s*\(\s*[a-z]\s*\)"                                   # 401(k), 403(b), 457(b)
    r"|\b(?:S\s*&\s*P|SP|Nasdaq|NASDAQ|Russell|FTSE|Dow|Nikkei|CAC|DAX|MSCI|Fidelity|Vanguard|Schwab|iShares|SPDR)\s*\d{2,4}\b"
    r"|\b529\b",
    re.IGNORECASE,
)


def pack_strings(pack: Any, *, min_len: int = 3) -> list[str]:
    """Every string leaf of the pack (account names, position names, tickers, labels),
    longest first, so names like 'Bloomberg L.P. 401(k) Plan (2-01)' or 'S&P 500 ETF'
    can be removed from a narration before its digits are checked."""
    out: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            s = node.strip()
            if len(s) >= min_len and any(ch.isdigit() for ch in s):
                out.add(s)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(pack)
    return sorted(out, key=len, reverse=True)


def strip_named_numbers(text: str, pack: Any = None) -> str:
    """Remove digit-bearing names (plan types, index names, and any name that
    appears in the pack) so their digits are never mistaken for figures."""
    cleaned = text or ""
    for s in pack_strings(pack) if pack is not None else []:
        cleaned = re.sub(re.escape(s), " ", cleaned, flags=re.IGNORECASE)
    return _NAMED_NUMBERS_RE.sub(" ", cleaned)


def extract_numbers(text: str, pack: Any = None) -> list[NumberToken]:
    cleaned = _NOT_A_FIGURE_RE.sub(" ", strip_named_numbers(text or "", pack))
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
        pairs = _candidates(body)
        if has_k:
            pairs = [(c * 1000, d) for c, d in pairs]
        cands = [c for c, _ in pairs]
        tols = [(0.5 * 10 ** (-d)) * (1000 if has_k else 1) for _, d in pairs]
        if is_percent:
            cands = cands + [c / 100 for c in cands]
            tols = tols + [tl / 100 for tl in tols]
        if cands:
            tokens.append(NumberToken(raw=raw.strip(), candidates=tuple(cands), is_percent=is_percent, tolerances=tuple(tols)))
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


def _matches(candidate: float, tol: float, values: set[float], *, rel_tol: float) -> bool:
    """A written number matches a pack value when the value rounds to it at the
    precision it was written with (tol = half the last written decimal place),
    plus any extra relative slack the caller allows (off by default)."""
    for v in values:
        if abs(candidate - v) <= tol + rel_tol * abs(v) + 1e-9:
            return True
    return False


def ungrounded(text: str, pack: Any, *, rel_tol: float = 0.0, abs_tol: float = 0.0) -> list[str]:
    """Return the raw number tokens in `text` that do not match any pack value.
    `abs_tol` is kept for callers that want extra absolute slack; the default
    accepts only faithful roundings ("15,614" for 15,614.39) and rejects altered
    digits ("11,781.20" for 11,781.43, "28%" for 27.48)."""
    values = pack_values(pack)
    offenders: list[str] = []
    for tok in extract_numbers(text, pack):
        tols = tok.tolerances or tuple(0.0 for _ in tok.candidates)
        if any(_matches(c, max(tl, abs_tol), values, rel_tol=rel_tol) for c, tl in zip(tok.candidates, tols)):
            continue
        if tok.raw not in offenders:
            offenders.append(tok.raw)
    return offenders
