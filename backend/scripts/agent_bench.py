"""Agent bench for the Finance analyst (fork): same model, same data, arms compared on frozen questions.

Subcommands (run inside a pod from /app with PYTHONPATH=/app, except `summarize`, which needs no app):
  probe      counts and labels that fix the held-out questions (prints no amounts)
  truth      compute truth for v1, v1-corrected and v2 once, with the data fingerprint, into a JSON file
  run        ask the questions through the executor for each arm, scoring against a truth file
  summarize  aggregate JSONL rows; `--stage A|B` evaluates the pre-registered criteria
  cleanup    delete leftover bench conversations (idempotent)

Pre-registered in the plan (rev 2, 2026-10-09) before any handler code existed:
  v1   the 16 questions of the 2026-10-08 bake-off with their truth definitions, frozen; q08 and q11
       are a development set (they were the targeted failures)
  v1c  q08 only: every account whose institution contains "robinhood", the total and each account
  v2   16 held-out advisor questions; every item is tagged fact/policy and app/plan (whether the app
       already defines the figure); advice questions carry the rule the bench expects, computed here
       from the plan's decisions 1-5, never from app/agents/runtime/advice.py or the new handlers

Scoring uses a frozen copy of the qc18 number extractor so later app changes cannot move the ruler.
Full answers go to <out>/answers/ (copied off-cluster, never committed); JSONL rows carry no answer text
unless --keep-snippets.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import time
import urllib.request
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

BENCH_VERSION = "2026-10-09"
TITLE_PREFIX = "bench-"
OLLAMA_URL = os.environ.get("BENCH_OLLAMA_URL", "http://ollama:11434")

# Merchant B for v2 q3, fixed by `probe` with the rule in `_merchant_b_candidates` before the
# bench commit (2026-10-09: the winning token "food" leads every "FOOD WAY" spelling; the full
# name is used because "food" alone also matches a different store, FOODTOWN). Changing it
# afterwards excludes q3 from the criteria.
MERCHANT_B: Optional[str] = "Food Way"


# ============================================================================ frozen scoring
# Copy of app/agents/runtime/grounding.py as of 0.15.1-qc18 (number extraction only).

_NOT_A_FIGURE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{4}-\d{2}\b"
    r"|\b\d{4}-W\d{2}\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"
    r"|\b(?:19|20)\d{2}\b"
)
NUMBER_RE = re.compile(r"(?<![\w.])[-−]?\d(?:[\d.,    ]*\d)?(?:\s*[kK](?![\w]))?(?:[\s   ]*%)?")
_SMALL_INT_LIMIT = 12
_NAMED_NUMBERS_RE = re.compile(
    r"\b\d{3}\s*\(\s*[a-z]\s*\)"
    r"|\b(?:S\s*&\s*P|SP|Nasdaq|NASDAQ|Russell|FTSE|Dow|Nikkei|CAC|DAX|MSCI|Fidelity|Vanguard|Schwab|iShares|SPDR)\s*\d{2,4}\b"
    r"|\b529\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NumberToken:
    raw: str
    candidates: tuple[float, ...]
    is_percent: bool
    tolerances: tuple[float, ...] = ()


def _decimals(digits: str) -> int:
    return len(digits.split(".", 1)[1]) if "." in digits else 0


def _candidates(body: str) -> list[tuple[float, int]]:
    s = body.replace(" ", "").replace(" ", "").replace("−", "-")
    has_dot, has_comma = "." in s, "," in s
    out: list[tuple[float, int]] = []
    try:
        if has_dot and has_comma:
            norm = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
            out.append((float(norm), _decimals(norm)))
        elif has_comma:
            out.append((float(s.replace(",", "")), 0))
            norm = s.replace(",", ".")
            out.append((float(norm), _decimals(norm)))
        elif has_dot:
            out.append((float(s), _decimals(s)))
            out.append((float(s.replace(".", "")), 0))
        else:
            out.append((float(s), 0))
    except ValueError:
        return []
    return out


def _pack_strings(pack: Any, *, min_len: int = 3) -> list[str]:
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


def _strip_named_numbers(text: str, pack: Any = None) -> str:
    cleaned = text or ""
    for s in _pack_strings(pack) if pack is not None else []:
        cleaned = re.sub(re.escape(s), " ", cleaned, flags=re.IGNORECASE)
    return _NAMED_NUMBERS_RE.sub(" ", cleaned)


def extract_numbers(text: str, pack: Any = None) -> list[NumberToken]:
    cleaned = _NOT_A_FIGURE_RE.sub(" ", _strip_named_numbers(text or "", pack))
    tokens: list[NumberToken] = []
    for m in NUMBER_RE.finditer(cleaned):
        raw = m.group(0)
        is_percent = raw.rstrip().endswith("%")
        body = raw.rstrip("%").rstrip()
        has_k = body[-1:] in ("k", "K")
        if has_k:
            body = body[:-1].rstrip()
        plain_int = not is_percent and not has_k and not any(ch in body for ch in ".,  ")
        if plain_int:
            try:
                if abs(int(body.replace("−", "-"))) <= _SMALL_INT_LIMIT:
                    continue
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


def _pack_values(pack: Any) -> set[float]:
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


def ungrounded(text: str, pack: Any) -> list[str]:
    values = _pack_values(pack)
    offenders: list[str] = []
    for tok in extract_numbers(text, pack):
        tols = tok.tolerances or tuple(0.0 for _ in tok.candidates)
        ok = any(abs(c - v) <= tl + 1e-9 for c, tl in zip(tok.candidates, tols) for v in values)
        if not ok and tok.raw not in offenders:
            offenders.append(tok.raw)
    return offenders


def _num_match(tokens: list[NumberToken], value: Any) -> bool:
    if value is None:
        return False
    v = float(value)
    for tok in tokens:
        for c, tol in zip(tok.candidates, tok.tolerances or [0.0] * len(tok.candidates)):
            if abs(abs(c) - abs(v)) <= max(tol, 0.01 * abs(v), 0.005):
                return True
    return False


_ANY_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def _months_match(text: str, value: float) -> bool:
    """Months to one decimal, ±0.1; small integers count here (the extractor skips them)."""
    for m in _ANY_NUMBER_RE.finditer(text or ""):
        try:
            if abs(float(m.group(0)) - value) <= 0.1 + 1e-9:
                return True
        except ValueError:
            continue
    return False


def score_v1(answer: str, expected: list) -> Optional[float]:
    """The 2026-10-08 scorer, unchanged."""
    text = answer or ""
    tokens = extract_numbers(text)
    hits = 0
    for kind, val in expected:
        if kind == "num":
            hits += _num_match(tokens, val)
        elif kind == "pct":
            hits += _num_match(tokens, val) or _num_match(tokens, val * 100)
        elif kind == "str":
            hits += any(s and s.lower() in text.lower() for s in val)
    return hits / len(expected) if expected else None


def score_items(answer: str, items: list[dict]) -> list[bool]:
    """v2 items: {kind: num|pct|str|months, value, tag: fact|policy, std: app|plan, label}."""
    text = answer or ""
    tokens = extract_numbers(text)
    out = []
    for it in items:
        kind, val = it["kind"], it["value"]
        if kind == "num":
            out.append(_num_match(tokens, val))
        elif kind == "pct":
            out.append(_num_match(tokens, val) or _num_match(tokens, float(val) * 100))
        elif kind == "str":
            out.append(any(s and s.lower() in text.lower() for s in val))
        elif kind == "months":
            out.append(_months_match(text, float(val)))
        else:
            out.append(False)
    return out


HEDGE_RE = re.compile(
    r"\b(might|perhaps|may want to|consider consulting|it depends|not financial advice|everyone's situation|could potentially)\b",
    re.IGNORECASE,
)
SELL_RE = re.compile(r"(?i)\bsell\b")
BUY_TICKER_RE = re.compile(r"\b[Bb]uy\s+(?:more\s+)?[A-Z]{2,5}\b")
NEXT_MOVE_RE = re.compile(
    r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?\s*(?:next move|próximo passo|próximo paso)\s*:?\s*(?:\*\*)?\s*:?\s*(.+)$"
)


def lint(answer: str) -> dict[str, Any]:
    text = answer or ""
    nm = NEXT_MOVE_RE.findall(text)
    return {
        "hedges": sorted({m.lower() for m in HEDGE_RE.findall(text)}),
        "sell_lint": bool(SELL_RE.search(text) or BUY_TICKER_RE.search(text)),
        "next_move_lines": len(nm),
        "next_move_text": nm[-1].strip() if nm else None,
    }


# ============================================================================ plan decisions 1-5 (bench copy)

RET_RE = re.compile(r"401\s*\(?k\)?|401k|403\s*\(?b\)?|\b457\b|\bIRA\b|\bRoth\b|Retirement|Pension|Previd|Aposentad", re.IGNORECASE)
CRYPTO_ACCT_RE = re.compile(r"crypto|bitcoin|coinbase|ethereum", re.IGNORECASE)
BROAD_TICKERS = {"VTI", "VOO", "SPY", "IVV", "VT", "VXUS", "ITOT", "SCHB", "FXAIX", "FSKAX", "FZROX", "VTSAX", "VFIAX", "SWTSX", "SWPPX"}
BROAD_NAME_RE = re.compile(r"\b(total (stock )?market|s&p 500|target (date|retirement)|total world|all.?world|total international)\b", re.IGNORECASE)
INDEX_RE = re.compile(r"\bindex\b", re.IGNORECASE)
SECTOR_RE = re.compile(r"\b(technology|health|energy|financial|real estate|utilities|semiconductor|dividend|growth|value)\b", re.IGNORECASE)
CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "LTC"}
CRYPTO_NAME_RE = re.compile(r"bitcoin|ethereum", re.IGNORECASE)
CASH_NAME_RE = re.compile(r"\b(cash|money market|sweep)\b", re.IGNORECASE)
CASH_TICKERS = {"SPAXX", "FDRXX", "VMFXX", "SWVXX", "SPRXX", "FZFXX"}
BOND_RE = re.compile(r"\b(bond|treasury|fixed income)\b", re.IGNORECASE)
BOND_TICKERS = {"BND", "AGG", "BNDX"}
FUND_RE = re.compile(r"\b(fund|etf|trust|portfolio)\b", re.IGNORECASE)
# SimpleFIN names positions "189.745 shares of PLTR": no information beyond the ticker
SHARES_OF_RE = re.compile(r"^\s*[\d.,]+\s+shares?\s+of\s+\S+\s*$", re.IGNORECASE)
INTEREST_RE = re.compile(r"interest charge|finance charge|purchase interest", re.IGNORECASE)
ESSENTIAL_RE = re.compile(
    r"\b(rent|mortgages?|housing|utilit(?:y|ies)|insurance|tax(?:es)?|health(?:care)?|medical|childcare|loans?|debts?)\b",
    re.IGNORECASE,
)
NON_CATEGORY_RE = re.compile(r"^\s*(uncategori[sz]ed|other\b.*)$", re.IGNORECASE)

BANDS = (0.20, 0.35, 0.50, 0.65)
REAL_RETURN = 0.05
WITHDRAWAL = 0.04
YEARS_CAP = 60.0
PRIORITY = {"card_carry": 1, "cash_floor": 2, "spending_spike": 3, "idle_cash": 4, "cash_drag": 5,
            "crypto_cap": 7, "single_position": 8, "stock_share": 9, "spending_cut": 10, "keep_going": 12}
FOCUS = [
    (("cash", "emergenc", "buffer"), ("cash_floor", "idle_cash")),
    (("debt", "card", "interest"), ("card_carry",)),
    (("retire", "on track", "savings rate"), ("savings_band",)),
    (("invest more", "contribut", "put away"), ("savings_band", "idle_cash")),
    (("crypto",), ("crypto_cap",)),
    (("stock", "concentrat", "diversif"), ("single_position", "stock_share")),
    (("wast", "cut", "spend"), ("spending_spike", "spending_cut")),
]


def focus_rules(question: str) -> set[str]:
    q = question.lower()
    out: set[str] = set()
    for stems, rules in FOCUS:
        if any(s in q for s in stems):
            out.update(rules)
    if re.search(r"\bFI\b", question):
        out.add("savings_band")
    return out


def classify_asset(name: str, ticker: Optional[str], security_type: Optional[str], account_label: str = "") -> str:
    st = (security_type or "").strip().lower().replace("_", " ")
    t = (ticker or "").strip().upper()
    n = "" if SHARES_OF_RE.match(name or "") else (name or "")
    broad = t in BROAD_TICKERS or bool(BROAD_NAME_RE.search(n)) or (bool(INDEX_RE.search(n)) and not SECTOR_RE.search(n))
    if st:
        if st in ("etf", "mutual fund"):
            return "broad" if broad else "fund_other"
        if st == "equity":
            return "stock"
        if st in ("cryptocurrency", "crypto"):
            return "crypto"
        if st in ("cash", "money market"):
            return "cash"
        if st == "fixed income":
            return "bond"
        if st in ("derivative", "other"):
            return "other"
    if broad:
        return "broad"
    if CRYPTO_ACCT_RE.search(account_label or "") or t in CRYPTO_TICKERS or CRYPTO_NAME_RE.search(n):
        return "crypto"
    if CASH_NAME_RE.search(n) or t in CASH_TICKERS:
        return "cash"
    if BOND_RE.search(n) or t in BOND_TICKERS:
        return "bond"
    if FUND_RE.search(n):
        return "fund_other"
    if t:
        return "stock"
    return "unknown"


_MERCHANT_ALIASES = {"amazon": ("amazon", "amzn")}


def merchant_patterns(query: str) -> list[re.Pattern]:
    """Decision 9: case-insensitive; a token must not follow a letter or digit; tokens of 3 chars
    or fewer also need a trailing boundary; every token must match; apostrophes dropped."""
    q = (query or "").lower().replace("'", "").replace("’", "")
    tokens = [t for t in re.split(r"[^a-z0-9&]+", q) if t]
    pats = []
    for tok in tokens:
        alts = []
        for a in _MERCHANT_ALIASES.get(tok, (tok,)):
            p = r"(?<![a-z0-9])" + re.escape(a)
            if len(a) <= 3:
                p += r"(?![a-z0-9])"
            alts.append(p)
        pats.append(re.compile("(?:" + "|".join(alts) + ")", re.IGNORECASE))
    return pats


def merchant_hit(columns: list[Optional[str]], pats: list[re.Pattern]) -> bool:
    return any(c and all(p.search(c) for p in pats) for c in columns)


def years_to_target(balance: float, contribution: float, rate: float, target: float) -> Optional[float]:
    """Same closed form as fire_service.years_to_target (end-of-year contributions), capped at 60."""
    import math

    if balance >= target:
        return 0.0
    if rate > 0:
        num, den = target * rate + contribution, balance * rate + contribution
        if den <= 0:
            return None
        n = round(math.log(num / den) / math.log(1 + rate), 2)
        return n if n <= YEARS_CAP else None
    return None


# ============================================================================ app access (pod only)


async def _app():
    """Import the app lazily so `summarize` runs anywhere."""
    import mcp_server.tools  # noqa: F401  registers tools
    from app.core.database import async_session_maker

    return async_session_maker


async def tool(session, ctx, name, **kw):
    from mcp_server.registry import REGISTRY

    return await REGISTRY[name].handler(session=session, ctx=ctx, **kw)


async def _user_agent(session):
    from sqlalchemy import select

    from app.agents.models.agent import Agent
    from app.models.user import User

    user = (await session.execute(select(User))).scalars().first()
    agent = (await session.execute(select(Agent).where(Agent.name == "Finance analyst", Agent.user_id == user.id))).scalars().first()
    return user, agent


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def _ollama(model: str) -> dict[str, Any]:
    out: dict[str, Any] = {"model": model}
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/version", timeout=10) as r:
            out["version"] = json.loads(r.read()).get("version")
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=10) as r:
            tags = json.loads(r.read()).get("models") or []
        out["digest"] = next((m.get("digest") for m in tags if m.get("name") == model or m.get("model") == model), None)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


async def fingerprint(session, ws) -> dict[str, Any]:
    from sqlalchemy import func, select

    from app.models.asset import Asset
    from app.models.asset_value import AssetValue
    from app.models.bank_connection import BankConnection
    from app.models.transaction import Transaction

    conns = (await session.execute(select(BankConnection.id, BankConnection.last_sync_at).where(BankConnection.workspace_id == ws))).all()
    tx = (await session.execute(
        select(func.count(), func.max(Transaction.created_at), func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.workspace_id == ws)
    )).one()
    av = await session.scalar(select(func.max(AssetValue.date)).where(AssetValue.workspace_id == ws))
    lp = await session.scalar(select(func.max(Asset.last_price_at)).where(Asset.workspace_id == ws))
    return {
        "utc_date": datetime.now(timezone.utc).date().isoformat(),
        "connections": sorted([[str(i), s.isoformat() if s else None] for i, s in conns]),
        "tx_count": int(tx[0]),
        "tx_max_created": tx[1].isoformat() if tx[1] else None,
        "tx_amount_sum": str(tx[2]),
        "asset_value_max_date": av.isoformat() if av else None,
        "asset_last_price_at": lp.isoformat() if lp else None,
    }


def _amount(row_amount, row_amount_primary, row_currency, primary: str) -> float:
    if row_currency != primary and row_amount_primary is not None:
        return float(row_amount_primary)
    return float(row_amount)


async def merchant_rows(session, ws, primary: str, query: str, fd: date, td: date) -> dict[str, Any]:
    """Decision 9 charges/refunds/pending for one merchant query, bench implementation."""
    from sqlalchemy import or_, select

    from app.models.payee import Payee
    from app.models.transaction import Transaction
    from app.services._query_filters import counts_as_pnl, is_split_parent

    pats = merchant_patterns(query)
    first = (query or "").lower().replace("'", "").split()[0] if query else ""
    likes = _MERCHANT_ALIASES.get(first, (first,))
    cond = or_(*[c.ilike(f"%{a}%") for a in likes for c in (Transaction.description, Transaction.payee, Transaction.notes, Payee.name)])
    rows = (await session.execute(
        select(Transaction.type, Transaction.status, Transaction.amount, Transaction.amount_primary, Transaction.currency,
               Transaction.description, Transaction.payee, Transaction.notes, Payee.name)
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(Transaction.workspace_id == ws, Transaction.date >= fd, Transaction.date <= td,
               Transaction.source != "opening_balance", counts_as_pnl(), ~is_split_parent(), cond)
    )).all()
    charges = refunds = 0.0
    n_charges = n_refunds = n_pending = 0
    for typ, status, amt, amt_p, cur, desc, payee, notes, pname in rows:
        if not merchant_hit([desc, payee, notes, pname], pats):
            continue
        if status != "posted":
            n_pending += 1
            continue
        v = _amount(amt, amt_p, cur, primary)
        if typ == "debit":
            charges += v
            n_charges += 1
        else:
            refunds += v
            n_refunds += 1
    return {"charges": round(charges, 2), "refunds": round(refunds, 2), "n_charges": n_charges, "n_refunds": n_refunds, "n_pending": n_pending}


async def coverage_window(session, ws, today: date) -> tuple[date, int]:
    """Plan deviation 2026-10-09: most accounts were connected in mid-2026, so "trailing 365 days"
    held ~3 months of real data. The window starts when the last of the currently active
    spending accounts (open, >= 1 posted P&L debit in the last 90 days) has its first posted
    row, bounded below by today - 364. Figures over it are annualized x 365 / days."""
    from sqlalchemy import func, select

    from app.models.account import Account
    from app.models.transaction import Transaction
    from app.services._query_filters import counts_as_pnl

    active = (await session.execute(
        select(Transaction.account_id).join(Account, Account.id == Transaction.account_id).where(
            Transaction.workspace_id == ws, Account.is_closed.is_(False), Transaction.type == "debit",
            Transaction.status == "posted", Transaction.date >= today - timedelta(days=90),
            Transaction.source != "opening_balance", counts_as_pnl()).distinct()
    )).scalars().all()
    floor = today - timedelta(days=364)
    if not active:
        return floor, 365
    firsts = (await session.execute(
        select(func.min(Transaction.date)).where(Transaction.account_id.in_(active), Transaction.status == "posted",
                                                 Transaction.source != "opening_balance").group_by(Transaction.account_id)
    )).scalars().all()
    start = max([floor] + [d for d in firsts if d is not None])
    return start, (today - start).days + 1


async def advisor_facts(session, user, agent, today: date) -> dict[str, Any]:
    """Everything v2 needs, computed from app reads and the plan's decisions (bench copy)."""
    from sqlalchemy import select

    from app.agents.services import digest_service
    from app.models.asset import Asset
    from app.models.transaction import Transaction
    from app.services import account_service
    from mcp_server.auth import CallContext

    ws = agent.workspace_id
    ctx = CallContext(user_id=user.id, workspace_id=ws)
    primary = user.primary_currency or "USD"
    accounts = await account_service.get_accounts(session, ws)
    by_id = {str(a["id"]): a for a in accounts}

    def label(a):
        return a.get("display_name") or a.get("name") or ""

    # decision 1 (with the coverage deviation): FIRE figures from the summary line over the
    # coverage window, annualized; invested assets as fire_projection defines them
    from mcp_server.tools.finance import _summary_window

    cov_start, cov_days = await coverage_window(session, ws, today)
    scale = 365.0 / cov_days
    win = await _summary_window(session, ws, user.id, cov_start, today)
    annual_spend = round(float(win["expense"]) * scale, 2)
    annual_contribution = round(float(win["invested"]) * scale, 2)
    hold = await tool(session, ctx, "get_holdings")
    invested_assets = round(float(hold["investment_accounts_total_primary"]) + float((hold.get("unlinked_holdings_total_by_currency") or {}).get(primary, 0.0)), 2)
    fi_number = round(annual_spend / WITHDRAWAL, 2)
    years_now = years_to_target(invested_assets, annual_contribution, REAL_RETURN, fi_number)
    monthly_spend = annual_spend / 12

    # decision 1: cash-flow figures from the Money Map over the same window
    mm = await tool(session, ctx, "get_money_map", days=max(7, min(cov_days, 730)))
    t = mm["totals"]
    inc, exp, dc, net = float(t["income"]), float(t["expenses"]), float(t["direct_contributions"]), float(t["net"])
    rate = t.get("contribution_aware_savings_rate")
    mm90 = await tool(session, ctx, "get_money_map", days=90)
    surplus_month = net * scale / 12
    surplus_seasonal = float(mm90["totals"]["net"]) / 3

    # decisions 2-3: holdings, classes, liquid cash
    meta = {str(a.id): (a.external_metadata or {}) for a in (await session.execute(select(Asset).where(Asset.workspace_id == ws))).scalars()}
    classes: dict[str, float] = defaultdict(float)
    retirement_cash = brokerage_liquid_cash = 0.0
    positions: list[dict[str, Any]] = []
    for acct in hold["accounts"]:
        a = by_id.get(str(acct["account_id"])) or {}
        name = acct.get("name") or ""
        acct_label = f"{name} {a.get('institution_name') or ''}"
        bal = acct["balance_primary"] if acct.get("balance_primary") is not None else (acct.get("balance") or 0.0)
        is_ret = bool(RET_RE.search(name))
        hs = acct.get("holdings") or []
        hsum = 0.0
        for h in hs:
            v = float(h.get("current_value") or 0.0)
            cls = classify_asset(h.get("name") or "", h.get("ticker"), meta.get(str(h["id"]), {}).get("security_type"), acct_label)
            classes[cls] += v
            hsum += v
            positions.append({"name": h.get("name"), "ticker": h.get("ticker"), "value": v, "cls": cls, "account": name})
            if cls == "cash":
                if is_ret:
                    retirement_cash += v
                else:
                    brokerage_liquid_cash += v
        if hs:
            resid = float(bal) - hsum
            if resid > 0:
                classes["cash"] += resid
                if is_ret:
                    retirement_cash += resid
                else:
                    brokerage_liquid_cash += resid
        else:
            classes["crypto" if CRYPTO_ACCT_RE.search(acct_label) else "unknown"] += float(bal)
    for h in hold.get("unlinked_holdings") or []:
        if h.get("currency") not in (None, primary):
            continue
        v = float(h.get("current_value") or 0.0)
        cls = classify_asset(h.get("name") or "", h.get("ticker"), meta.get(str(h["id"]), {}).get("security_type"))
        classes[cls] += v
        positions.append({"name": h.get("name"), "ticker": h.get("ticker"), "value": v, "cls": cls, "account": None})

    stocks: dict[str, dict[str, Any]] = {}
    for p in positions:
        if p["cls"] != "stock":
            continue
        key = (p["ticker"] or p["name"] or "").upper()
        s = stocks.setdefault(key, {"name": p["name"], "ticker": p["ticker"], "value": 0.0})
        s["value"] += p["value"]
    largest_stock = max(stocks.values(), key=lambda s: s["value"], default=None)

    def share(v):
        return v / invested_assets if invested_assets > 0 else None

    checking_savings = sum(float(a["current_balance"]) for a in accounts if a["type"] in ("checking", "savings") and float(a["current_balance"]) > 0)
    liquid = checking_savings + brokerage_liquid_cash
    cards = [(label(a), max(-float(a["current_balance"]), 0.0), str(a["id"])) for a in accounts if a["type"] == "credit_card"]
    card_owed = sum(c[1] for c in cards)
    robin = [(label(a), float(a["current_balance"])) for a in accounts if "robinhood" in (a.get("institution_name") or "").lower()]

    # card interest, last 90 days (credit_card accounts)
    card_ids = [uuid.UUID(c[2]) for c in cards]
    interest_by_card: dict[str, float] = defaultdict(float)
    if card_ids:
        rows = (await session.execute(
            select(Transaction.account_id, Transaction.description, Transaction.amount, Transaction.amount_primary, Transaction.currency)
            .where(Transaction.workspace_id == ws, Transaction.account_id.in_(card_ids), Transaction.type == "debit",
                   Transaction.is_ignored.is_(False), Transaction.date >= today - timedelta(days=90), Transaction.date <= today)
        )).all()
        for aid, desc, amt, amt_p, cur in rows:
            if INTEREST_RE.search(desc or ""):
                interest_by_card[str(aid)] += _amount(amt, amt_p, cur, primary)
    interest_90 = round(sum(interest_by_card.values()), 2)

    # months and categories (expense_by_category: posted P&L debits by Transaction.date)
    def month_start(d: date, back: int) -> date:
        y, m = d.year, d.month - back
        while m <= 0:
            m += 12
            y -= 1
        return date(y, m, 1)

    def month_end(d: date) -> date:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return nxt - timedelta(days=1)

    latest_complete = month_start(today, 1)
    # complete months inside the coverage window (deviation 2026-10-09): older months are empty
    # because the accounts were not connected yet, so they cannot serve as a spending baseline
    first_full = cov_start if cov_start.day == 1 else month_start(cov_start, -1)
    month_cats: dict[date, dict[str, float]] = {}
    for back in range(1, 13):
        ms = month_start(today, back)
        if ms < first_full:
            break
        month_cats[ms] = await digest_service.expense_by_category(session, ws, ms, month_end(ms))

    def spikes_for(m: date) -> Optional[list[dict[str, Any]]]:
        base_months = [bm for bm in (month_start(m, k) for k in range(1, 7)) if bm in month_cats]
        if len(base_months) < 3 or m not in month_cats:
            return None  # not enough complete history: the rule is skipped, never guessed
        out = []
        for cat, val in month_cats[m].items():
            if NON_CATEGORY_RE.match(cat or ""):
                continue
            series = [month_cats[bm].get(cat, 0.0) for bm in base_months]
            if sum(1 for x in series if x > 0) < 3:
                continue
            baseline = sum(series) / len(series)
            excess = val - baseline
            if val > 1.5 * baseline and excess >= 200:
                out.append({"category": cat, "amount": round(val, 2), "baseline": round(baseline, 2), "excess": round(excess, 2)})
        return sorted(out, key=lambda s: s["excess"], reverse=True)

    spikes = spikes_for(latest_complete)

    # biggest controllable category: monthly average over the coverage window
    window_cats = await digest_service.expense_by_category(session, ws, cov_start, today)
    controllable = sorted(((c, v) for c, v in window_cats.items() if not ESSENTIAL_RE.search(c or "") and not NON_CATEGORY_RE.match(c or "")),
                          key=lambda kv: kv[1], reverse=True)
    top_cut = None
    if controllable:
        monthly = round(controllable[0][1] * scale / 12, 2)
        top_cut = {"category": controllable[0][0], "amount": monthly, "half": round(monthly / 2, 2)}

    # decision 4 rules (bench copy)
    fired: dict[str, float] = {}
    # card_carry needs interest in the last 90 days on a card that still owes money now
    # (deviation 2026-10-09: a paid-off card makes "pay off 0.00" meaningless)
    carrying = [(c[1], c[2]) for c in cards if interest_by_card.get(c[2], 0) > 0 and c[1] > 0]
    if carrying:
        fired["card_carry"] = round(max(carrying)[0], 2)
    if monthly_spend > 0 and liquid < 3 * monthly_spend:
        fired["cash_floor"] = round(3 * monthly_spend, 2)
    if spikes:
        fired["spending_spike"] = spikes[0]["excess"]
    buffer6 = 6 * monthly_spend + max(1000.0, 0.5 * monthly_spend)
    if monthly_spend > 0 and liquid > buffer6:
        fired["idle_cash"] = round(liquid - buffer6, 2)
    if invested_assets > 0 and retirement_cash > 0.05 * invested_assets and retirement_cash >= 1000:
        fired["cash_drag"] = round(retirement_cash, 2)
    band_next = next((b for b in BANDS if rate is not None and rate < b), None)
    band_cut = None
    if rate is not None and band_next is not None:
        band_cut = round((band_next * (inc + dc) - (inc - exp + dc)) * scale / 12, 2)
        fired["savings_band"] = band_cut
    crypto_v = classes.get("crypto", 0.0)
    if invested_assets > 0 and crypto_v > 0.05 * invested_assets:
        fired["crypto_cap"] = round(crypto_v - 0.05 * invested_assets, 2)
    if largest_stock and invested_assets > 0 and largest_stock["value"] > 0.10 * invested_assets:
        fired["single_position"] = round(largest_stock["value"] - 0.10 * invested_assets, 2)
    stock_total = sum(s["value"] for s in stocks.values())
    if invested_assets > 0 and stock_total > 0.20 * invested_assets:
        fired["stock_share"] = round(stock_total - 0.20 * invested_assets, 2)
    if rate is not None and rate < 0.65 and top_cut:
        fired["spending_cut"] = top_cut["half"]
    if rate is not None and rate >= 0.65 and not any(r in fired for r in ("card_carry", "cash_floor", "spending_spike", "idle_cash", "cash_drag", "crypto_cap", "single_position", "stock_share")):
        fired["keep_going"] = round(annual_contribution / 12, 2)

    def prio(r: str) -> int:
        if r == "savings_band":
            return 6 if (rate is not None and rate < 0.20) else 11
        return PRIORITY[r]

    order = sorted(fired, key=prio)
    years_plus_1000 = years_to_target(invested_assets, annual_contribution + 12000, REAL_RETURN, fi_number)
    fi_after_cut = years_after_cut = None
    if band_cut is not None:
        fi_after_cut = round((annual_spend - 12 * band_cut) / WITHDRAWAL, 2)
        years_after_cut = years_to_target(invested_assets, annual_contribution + 12 * band_cut, REAL_RETURN, fi_after_cut)

    return {
        "primary": primary, "coverage_start": cov_start.isoformat(), "coverage_days": cov_days,
        "annual_spend": annual_spend, "monthly_spend": round(monthly_spend, 2), "invested_assets": invested_assets,
        "annual_contribution": annual_contribution, "fi_number": fi_number, "years_now": years_now,
        "years_plus_1000": years_plus_1000, "fi_after_cut": fi_after_cut, "years_after_cut": years_after_cut,
        "income_365": inc, "expenses_365": exp, "direct_365": dc, "net_365": net, "savings_rate": rate,
        "surplus_month": round(surplus_month, 2), "surplus_seasonal": round(surplus_seasonal, 2),
        "band_next": band_next, "band_cut": band_cut,
        "classes": {k: round(v, 2) for k, v in classes.items()},
        "crypto_share": share(crypto_v), "stock_share": share(stock_total),
        "largest_stock": ({**largest_stock, "share": share(largest_stock["value"])} if largest_stock else None),
        "retirement_cash": round(retirement_cash, 2), "brokerage_liquid_cash": round(brokerage_liquid_cash, 2),
        "checking_savings": round(checking_savings, 2), "liquid_cash": round(liquid, 2),
        "months_covered": round(liquid / monthly_spend, 1) if monthly_spend > 0 else None,
        "cards": [{"name": n, "owed": round(o, 2)} for n, o, _ in sorted(cards, key=lambda c: -c[1])],
        "card_owed": round(card_owed, 2), "interest_90": interest_90,
        "robinhood": [{"name": n, "balance": round(b, 2)} for n, b in robin],
        "spikes": spikes, "spike_month": latest_complete.isoformat(), "spike_history_months": len(month_cats), "top_cut": top_cut,
        "fired": fired, "rule_order": order,
        "top15": sorted(positions, key=lambda p: -p["value"])[:15],
    }


def expected_rule(question: str, facts: dict[str, Any]) -> tuple[Optional[str], Optional[float]]:
    """Decision 4 focus + fallback: the lowest-priority-number fired rule in focus, else the top one."""
    focus = focus_rules(question)
    order = facts["rule_order"]
    pick = next((r for r in order if r in focus), None) or (order[0] if order else None)
    return pick, (facts["fired"].get(pick) if pick else None)


def _num(v, tag="fact", std="app", label=""):
    return {"kind": "num", "value": v, "tag": tag, "std": std, "label": label}


def _pct(v, tag="fact", std="app", label=""):
    return {"kind": "pct", "value": v, "tag": tag, "std": std, "label": label}


def _str(options, tag="fact", std="app", label=""):
    return {"kind": "str", "value": list(options), "tag": tag, "std": std, "label": label}


def build_v2(facts: dict[str, Any], extra: dict[str, Any]) -> list[dict[str, Any]]:
    f = facts
    Q: list[dict[str, Any]] = []

    def add(qid, category, text, items, advice=False):
        q = {"q": qid, "category": category, "question": text, "items": [i for i in items if i is not None], "advice": advice}
        if advice:
            q["expected_rule"], q["expected_amount"] = expected_rule(text, f)
        Q.append(q)

    sep, typical = extra["sep_expense"], extra["typical_month"]
    add(0, "summary", "What did I spend in September 2026, and is that more than my typical month?", [
        _num(sep, label="Sep expense"), _num(typical, std="plan", label="typical month"),
        _str(["more", "above", "higher", "over"] if sep > typical else ["less", "below", "lower", "under"], label="above/below"),
    ])
    add(1, "merchant", "How much did I spend on Amazon between January and September 2026?", [_num(extra["amazon"]["charges"], label="Amazon charges")])
    if MERCHANT_B:
        add(2, "merchant", f"How much did I spend at {MERCHANT_B} between January and September 2026?",
            [_num(extra["merchant_b"]["charges"], label=f"{MERCHANT_B} charges")])
    add(3, "account", "How much cash do I have in checking and savings combined?", [_num(f["checking_savings"], std="plan", label="checking+savings")])
    owing = [c for c in f["cards"] if c["owed"] > 0][:3]
    add(4, "account", "What do I owe on my credit cards right now?",
        [_num(f["card_owed"], label="card owed")] + [_str([c["name"]], label="card name") for c in owing])
    rh = f["robinhood"]
    add(5, "account", "What's the total across all my Robinhood accounts?",
        ([_num(round(sum(r["balance"] for r in rh), 2), label="Robinhood total")] + [_num(r["balance"], label="Robinhood account") for r in rh]) if rh else [])
    add(6, "account", "How many months of expenses could my cash cover?",
        [{"kind": "months", "value": f["months_covered"], "tag": "fact", "std": "plan", "label": "months covered"}] if f["months_covered"] is not None else [])

    fired = f["fired"]
    pol8 = (_num(fired["idle_cash"], "policy", "plan", "idle excess") if "idle_cash" in fired
            else _num(fired["cash_floor"], "policy", "plan", "floor target") if "cash_floor" in fired
            else _str(["about right", "not too much", "right amount", "in range", "within your"], "policy", "plan", "about right"))
    add(7, "advice", "Am I sitting on too much cash?", [
        _num(f["liquid_cash"], std="plan", label="liquid cash"),
        {"kind": "months", "value": f["months_covered"], "tag": "fact", "std": "plan", "label": "months covered"} if f["months_covered"] is not None else None,
        pol8,
    ], advice=True)
    add(8, "advice", "Am I carrying credit card debt?", [
        _num(f["card_owed"], label="card owed"),
        _num(f["interest_90"], label="90-day interest") if f["interest_90"] > 0 else _str(["no interest", "no card interest", "zero interest", "0.00 in interest"], label="no interest"),
    ], advice=True)
    tc = f["top_cut"]
    add(9, "advice", "Where am I wasting money?", [
        _str([tc["category"]], label="top category") if tc else None,
        _num(tc["amount"], std="plan", label="monthly amount") if tc else None,
        _num(tc["half"], "policy", "plan", "half") if tc else None,
    ], advice=True)
    sp = f["spikes"]
    no_spike = (["not enough history", "not enough data", "too little history", "only since", "not enough months"] if sp is None
                else ["nothing unusual", "no unusual", "no category", "none of"])
    add(10, "advice", "Did any of my spending categories jump unusually in September 2026?", [
        _str([sp[0]["category"]], label="spike category") if sp else _str(no_spike, label="no spike"),
        _num(sp[0]["amount"], label="Sep amount") if sp else None,
        _num(sp[0]["excess"], "policy", "plan", "excess") if sp else None,
    ], advice=True)
    yn = f["years_now"]
    add(11, "advice", "Am I on track to retire early?", [
        _num(yn, label="years to FI") if yn is not None else _str(["more than 60", "60 years", "not within"], label="years to FI"),
        _pct(f["savings_rate"], label="savings rate") if f["savings_rate"] is not None else None,
        _num(f["band_cut"], "policy", "plan", "band-gap cut") if f["band_cut"] is not None else None,
    ], advice=True)
    add(12, "advice", "How much more should I invest each month to reach FI sooner?", [
        _num(f["surplus_month"], std="plan", label="monthly surplus"),
        _num(f["years_plus_1000"], std="plan", label="years at +1,000") if f["years_plus_1000"] is not None else None,
        _num(f["band_cut"], "policy", "plan", "band-gap cut") if f["band_cut"] is not None else None,
    ], advice=True)
    add(13, "advice", "Is my savings rate high enough for early retirement?", [
        _pct(f["savings_rate"], label="savings rate") if f["savings_rate"] is not None else None,
        _pct(f["band_next"], "policy", "plan", "next band") if f["band_next"] is not None else _str(["65"], "policy", "plan", "top band"),
    ], advice=True)
    ls = f["largest_stock"]
    add(14, "advice", "Is too much of my portfolio in one stock?", ([
        _str([x for x in (ls["ticker"], (ls["name"] or "")[:20]) if x], label="largest stock"),
        _pct(ls["share"], label="largest stock share") if ls["share"] is not None else None,
        _num(fired["single_position"], "policy", "plan", "excess") if "single_position" in fired
        else _str(["under 10", "below 10", "below the 10", "under the 10"], "policy", "plan", "under cap"),
    ] if ls else [_str(["no individual", "no single", "none"], label="no stocks")]), advice=True)
    cs = f["crypto_share"]
    add(15, "advice", "How much of my portfolio is crypto, and is that too much?", [
        _pct(cs, label="crypto share") if cs else _str(["no crypto", "0%", "0.0%"], label="no crypto"),
        _num(fired["crypto_cap"], "policy", "plan", "excess") if "crypto_cap" in fired
        else _str(["under 5", "below 5", "below the 5", "under the 5"], "policy", "plan", "under cap"),
    ], advice=True)
    return Q


async def build_v1(session, user, agent):
    """The 2026-10-08 questions and truth, unchanged (frozen), plus v1c for q08."""
    from sqlalchemy import func, select

    from app.agents.services import digest_service
    from app.models.transaction import Transaction
    from app.services import account_service
    from mcp_server.auth import CallContext

    ws = agent.workspace_id
    ctx = CallContext(user_id=user.id, workspace_id=ws)
    today = date.today()

    async def summ(fd, td):
        return await tool(session, ctx, "get_transactions_summary", from_date=fd.isoformat(), to_date=td.isoformat())

    aug = await summ(date(2026, 8, 1), date(2026, 8, 31))
    jul = await summ(date(2026, 7, 1), date(2026, 7, 31))
    ytd = await summ(date(2026, 1, 1), today)
    cats_aug = await digest_service.expense_by_category(session, ws, date(2026, 8, 1), date(2026, 8, 31))
    cats_sep = await digest_service.expense_by_category(session, ws, date(2026, 9, 1), date(2026, 9, 30))
    top3 = sorted(cats_aug.items(), key=lambda kv: kv[1], reverse=True)[:3]
    amazon_sep = next((v for k, v in cats_sep.items() if "amazon" in (k or "").lower()), 0.0)
    hold = await tool(session, ctx, "get_holdings")
    positions = [(h, a) for a in hold["accounts"] for h in (a.get("holdings") or [])]
    aapl = sum((h.get("current_value") or 0) for h, _ in positions if (h.get("ticker") or "").upper() == "AAPL")
    tsla = sum((h.get("current_value") or 0) for h, _ in positions if (h.get("ticker") or "").upper() == "TSLA")
    largest = max(positions, key=lambda p: p[0].get("current_value") or 0)[0]
    robin = next((a for a in hold["accounts"] if "robinhood" in a["name"].lower()), None)
    nw = await tool(session, ctx, "get_net_worth", months=1, interval="monthly")
    nw_last = (nw.get("trend") or [{}])[-1].get("value") if isinstance(nw, dict) else None
    fire = await tool(session, ctx, "fire_projection")
    mm = await tool(session, ctx, "get_money_map", days=90)
    uber = await session.scalar(
        select(func.coalesce(func.sum(func.coalesce(Transaction.amount_primary, Transaction.amount)), 0)).where(
            Transaction.workspace_id == ws, Transaction.type == "debit", Transaction.is_ignored.is_(False),
            Transaction.date >= date(2026, 6, 1), Transaction.date <= date(2026, 6, 30),
            func.lower(Transaction.description).like("%uber%"),
        )
    )

    def pct(x):
        return ("pct", float(x)) if x is not None else None

    Q = [
        ("summary", "How much did I spend in August 2026?", [("num", aug["expense"])]),
        ("summary", "What was my income in July 2026?", [("num", jul["income"])]),
        ("compare", "How did my net compare between July and August 2026?", [("num", jul["net"]), ("num", aug["net"])]),
        ("summary", "What was my savings rate in August 2026?", [pct(aug["savings_rate"])]),
        ("breakdown", "What were my top 3 spending categories in August 2026?",
         [("str", [k]) for k, _ in top3] + [("num", v) for _, v in top3]),
        ("lookup", "Do I own Apple stock, and how much is it worth?", [("str", ["AAPL", "Apple"]), ("num", aapl)]),
        ("lookup", "Do I own any Tesla stock?",
         [("str", ["TSLA"]), ("num", tsla)] if tsla else [("str", ["no ", "don't", "do not", "not hold", "not own", "none"])]),
        ("lookup", "What is my single largest investment position?", [("str", [largest.get("ticker") or "", (largest.get("name") or "")[:20]]), ("num", largest.get("current_value"))]),
        ("lookup", "How much is in my Robinhood account?", [("num", robin["balance_primary"] or robin["balance"])] if robin else []),
        ("networth", "What is my net worth right now?", [("num", nw_last)] if nw_last else []),
        ("merchant", "How much did I spend on Amazon & online in September 2026?", [("num", amazon_sep)]),
        ("merchant", "How much did I pay Uber in June 2026?", [("num", float(uber or 0))]),
        ("fire", "What is my financial independence number?", [("num", fire.get("fi_number"))]),
        ("summary", "How much have I invested so far in 2026?", [("num", ytd["invested"])]),
        ("moneymap", "How much did I spend in total over the last 90 days?", [("num", (mm.get("totals") or {}).get("expenses"))]),
        ("analysis", "Looking at my holdings, how concentrated is my portfolio in my largest account?",
         [("str", [robin["name"].split(" (")[0]] if robin else ["Robinhood"])]),
    ]
    Q = [(c, q, [e for e in exp if e]) for c, q, exp in Q]
    truth_values = {"aug": aug, "jul": jul, "ytd": ytd, "cats_aug": cats_aug, "cats_sep": cats_sep, "aapl": aapl, "tsla": tsla,
                    "largest": largest, "robin": robin, "nw": nw_last, "fire": {k: fire.get(k) for k in ("fi_number", "gap", "progress", "years_to_fi")},
                    "mm": mm.get("totals"), "uber": float(uber or 0)}
    accounts = await account_service.get_accounts(session, ws)
    rh = [float(a["current_balance"]) for a in accounts if "robinhood" in (a.get("institution_name") or "").lower()]
    v1c = {"8": ([("num", round(sum(rh), 2))] + [("num", b) for b in rh]) if rh else []}
    return Q, truth_values, v1c


# ============================================================================ subcommands


async def cmd_truth(args) -> None:
    from mcp_server.tools.finance import _summary_window

    maker = await _app()
    async with maker() as s:
        user, agent = await _user_agent(s)
        today = date.today()
        v1, v1_truth, v1c = await build_v1(s, user, agent)
        facts = await advisor_facts(s, user, agent, today)
        v1c["12"] = [("num", facts["fi_number"])]
        ws = agent.workspace_id
        sep = await _summary_window(s, ws, user.id, date(2026, 9, 1), date(2026, 9, 30))
        extra = {
            "sep_expense": float(sep["expense"]), "typical_month": facts["monthly_spend"],
            "amazon": await merchant_rows(s, ws, facts["primary"], "amazon", date(2026, 1, 1), date(2026, 9, 30)),
        }
        if MERCHANT_B:
            extra["merchant_b"] = await merchant_rows(s, ws, facts["primary"], MERCHANT_B, date(2026, 1, 1), date(2026, 9, 30))
        v2 = build_v2(facts, extra)
        out = {
            "bench_version": BENCH_VERSION,
            "bench_sha": _file_sha(),
            "image": os.environ.get("BENCH_IMAGE"),
            "node": os.environ.get("NODE_NAME"),
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "today": today.isoformat(),
            "ollama": _ollama(agent.model or "gpt-oss:20b"),
            "fingerprint": await fingerprint(s, ws),
            "v1": [{"q": i, "category": c, "question": q, "expected": e} for i, (c, q, e) in enumerate(v1)],
            "v1c": v1c,
            "v1_truth": v1_truth,
            "facts": facts,
            "extra": extra,
            "v2": v2,
        }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, default=str, indent=1)
    # aggregate-only console summary
    print(f"truth written: {args.out}  v1={len(out['v1'])} v2={len(v2)} merchant_b={MERCHANT_B!r}")
    print("rules fired (priority order):", ", ".join(facts["rule_order"]) or "none")
    for q in v2:
        tags = Counter(f"{i['tag']}/{i['std']}" for i in q["items"])
        print(f"  v2 q{q['q']:02d} {q['category']:9} items={len(q['items'])} {dict(tags)}" + (f" expected_rule={q.get('expected_rule')}" if q["advice"] else ""))


def _file_sha() -> str:
    try:
        return hashlib.sha256(open(__file__, "rb").read()).hexdigest()[:16]
    except OSError:
        return "unknown"


def arm_settings(arm: str) -> dict[str, Any]:
    if arm.startswith("bare"):
        return dict(guided_mode=False, tool_result_max_chars=0, prompt_budget_tokens=0, show_tool_commentary=True, max_tool_iterations=6)
    if arm.startswith("loop"):
        return dict(guided_mode=False)
    if arm.startswith("harness"):
        return {}
    raise SystemExit(f"unknown arm {arm!r} (expected bare*/loop*/harness*)")


def _find_next_move_rule(tool_data: list) -> Optional[str]:
    for d in tool_data or []:
        if isinstance(d, dict):
            nm = d.get("next_move")
            if isinstance(nm, dict) and nm.get("rule"):
                return str(nm["rule"])
    return None


async def run_one(session, agent, user, arm: str, question: str, tag: str):
    from sqlalchemy import select

    from app.agents.models.conversation import Message
    from app.agents.runtime.executor import AgentExecutor
    from app.agents.services import conversation_service

    ex = AgentExecutor()
    ex.settings = ex.settings.model_copy(update=arm_settings(arm))
    conv = await conversation_service.create_conversation(session, workspace_id=agent.workspace_id, user_id=user.id,
                                                          agent_id=agent.id, channel="web", title=f"{tag} {arm}")
    t0 = time.monotonic()
    errors: list[str] = []
    try:
        async for ev in ex.run(session=session, agent=agent, user_id=user.id, workspace_id=agent.workspace_id,
                               conversation_id=conv.id, user_message=question, channel="web"):
            if ev.type == "error":
                errors.append(ev.error_code)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"crash:{type(exc).__name__}")
    wall = time.monotonic() - t0
    msgs = (await session.execute(select(Message).where(Message.conversation_id == conv.id).order_by(Message.ordinal))).scalars().all()
    answers = [m.content for m in msgs if m.role == "assistant" and m.content]
    tool_data = [m.tool_result.get("data") for m in msgs if m.role == "tool" and m.tool_result]
    tool_names = [tc.get("name") for m in msgs if m.role == "assistant" for tc in (m.tool_calls or [])]
    return (answers[-1] if answers else ""), tool_data, tool_names, errors, wall


def _check_window(truth: dict, fp_now: dict, ollama_now: dict) -> list[str]:
    problems = []
    fp0 = truth["fingerprint"]
    for k in ("utc_date", "connections", "tx_count", "tx_max_created", "tx_amount_sum", "asset_value_max_date", "asset_last_price_at"):
        if fp0.get(k) != fp_now.get(k):
            problems.append(f"fingerprint.{k} changed")
    for k in ("version", "digest"):
        if (truth.get("ollama") or {}).get(k) != ollama_now.get(k):
            problems.append(f"ollama.{k} changed")
    return problems


async def cmd_run(args) -> None:
    truth = json.load(open(args.truth))
    maker = await _app()
    arms = args.arms.split(",")
    sets = args.set.split(",")
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(os.path.join(args.out, "answers"), exist_ok=True)
    results = open(os.path.join(args.out, "results.jsonl"), "a")
    tag = f"{TITLE_PREFIX}{uuid.uuid4().hex[:6]}"

    def log(**row):
        results.write(json.dumps(row, default=str) + "\n")
        results.flush()

    all_figures = {"v1": truth["v1_truth"], "facts": truth["facts"], "extra": truth["extra"]}
    async with maker() as s:
        user, agent = await _user_agent(s)
        header = {
            "kind": "header", "bench_version": BENCH_VERSION, "bench_sha": _file_sha(), "git_sha": os.environ.get("BENCH_GIT_SHA"),
            "image": os.environ.get("BENCH_IMAGE"), "mcp_image": os.environ.get("BENCH_MCP_IMAGE"), "node": os.environ.get("NODE_NAME"),
            "arms": arms, "sets": sets, "reps": args.reps, "rep_offset": args.rep_offset, "interleave": args.interleave,
            "prompt_hash": _sha((agent.system_prompt or "") + "\n" + (agent.description or "")),
            "agent_model": agent.model, "truth_computed_at": truth["computed_at"], "truth_image": truth.get("image"),
            "started_at": datetime.now(timezone.utc).isoformat(), "ollama": _ollama(agent.model or "gpt-oss:20b"), "tag": tag,
        }
        log(**header)
        problems = _check_window(truth, await fingerprint(s, agent.workspace_id), header["ollama"])
        if problems and not args.force:
            log(kind="abort", problems=problems)
            raise SystemExit("window changed since truth: " + "; ".join(problems))

        jobs: list[tuple[str, dict]] = []
        for set_name in sets:
            if set_name == "v1":
                for q in truth["v1"]:
                    jobs.append(("v1", q))
            elif set_name == "v2":
                for q in truth["v2"]:
                    jobs.append(("v2", q))
        try:
            for rep in range(args.rep_offset, args.rep_offset + args.reps):
                if rep > args.rep_offset:
                    problems = _check_window(truth, await fingerprint(s, agent.workspace_id), _ollama(agent.model or "gpt-oss:20b"))
                    if problems and not args.force:
                        log(kind="abort", rep=rep, problems=problems)
                        raise SystemExit("window changed mid-run: " + "; ".join(problems))
                for ji, (set_name, q) in enumerate(jobs):
                    run_arms = [a for a in arms if not (set_name == "v1" and a.startswith("loop") and args.loop_v2_only)]
                    if args.interleave and len(run_arms) > 1:
                        k = (ji + rep) % len(run_arms)
                        run_arms = run_arms[k:] + run_arms[:k]
                    for arm in run_arms:
                        ans, tool_data, tool_names, errors, wall = await run_one(s, agent, user, arm, q["question"], tag)
                        run_id = f"{set_name}-q{q['q']:02d}-{arm}-r{rep}-{uuid.uuid4().hex[:4]}"
                        with open(os.path.join(args.out, "answers", run_id + ".md"), "w") as fh:
                            fh.write(f"<!-- {set_name} q{q['q']} {arm} rep{rep} -->\n{q['question']}\n\n---\n{ans}\n")
                        row: dict[str, Any] = {
                            "kind": "run", "part": "qa", "set": set_name, "q": q["q"], "category": q["category"], "arm": arm, "rep": rep,
                            "errors": errors, "wall": round(wall, 1), "tools": len(tool_names), "tool_names": tool_names,
                            "answer_chars": len(ans or ""), "run_id": run_id,
                            "numbers_written": len(extract_numbers(ans)),
                            "unverifiable": len(ungrounded(ans, {"truth": all_figures, "tools": tool_data})),
                            **lint(ans),
                        }
                        if set_name == "v1":
                            row["acc"] = score_v1(ans, q["expected"])
                            corrected = (truth.get("v1c") or {}).get(str(q["q"]))
                            if corrected:
                                row["acc_corrected"] = score_v1(ans, corrected)
                        else:
                            hits = score_items(ans, q["items"])
                            row["items"] = [{"label": it["label"], "tag": it["tag"], "std": it["std"], "hit": h} for it, h in zip(q["items"], hits)]
                            row["acc"] = (sum(hits) / len(hits)) if hits else None
                            fact = [h for it, h in zip(q["items"], hits) if it["tag"] == "fact"]
                            app_fact = [h for it, h in zip(q["items"], hits) if it["tag"] == "fact" and it["std"] == "app"]
                            row["acc_fact"] = (sum(fact) / len(fact)) if fact else None
                            row["acc_fact_app"] = (sum(app_fact) / len(app_fact)) if app_fact else None
                            if q.get("advice"):
                                rule = _find_next_move_rule(tool_data)
                                row["rule"] = rule
                                row["expected_rule"] = q.get("expected_rule")
                                amt = q.get("expected_amount")
                                nm = row.get("next_move_text")
                                row["next_move_amount_ok"] = bool(nm and amt is not None and _num_match(extract_numbers(nm), amt))
                        if args.keep_snippets:
                            row["answer_head"] = (ans or "")[:240]
                        log(**row)
                        print(f"rep{rep} {set_name} q{q['q']:02d} {arm:12} acc={row.get('acc')} err={errors} {wall:.0f}s", flush=True)
                log(kind="rep_done", rep=rep, at=datetime.now(timezone.utc).isoformat())
        finally:
            await _cleanup(s, tag)
    log(kind="done", at=datetime.now(timezone.utc).isoformat())


async def _cleanup(session, tag_prefix: str) -> int:
    from sqlalchemy import delete, select

    from app.agents.models.conversation import Conversation, Message

    conv_ids = (await session.execute(select(Conversation.id).where(Conversation.title.like(f"{tag_prefix}%")))).scalars().all()
    if conv_ids:
        await session.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
        await session.commit()
    print(f"cleaned {len(conv_ids)} bench conversations", flush=True)
    return len(conv_ids)


async def cmd_cleanup(args) -> None:
    maker = await _app()
    async with maker() as s:
        await _cleanup(s, TITLE_PREFIX)


# ---------------------------------------------------------------------------- probe

_GENERIC_TOKENS = {
    # payment rails and banking words, fixed before the probe ran
    "payment", "transfer", "deposit", "purchase", "debit", "credit", "card", "online", "recurring", "check",
    "withdrawal", "interest", "zelle", "venmo", "paypal", "square", "cash", "mobile", "bill", "autopay",
    "store", "market", "retail", "from", "with", "ending", "account", "bank", "save", "saving", "savings",
    "checking", "direct", "payroll", "visa", "mastercard", "amex", "apple", "google", "fee",
    # already in the benchmark or in router few-shots
    "uber", "amazon", "amzn", "lyft", "fidelity", "netflix", "grocer", "groceries", "hood",
}


async def _merchant_b_candidates(session, ws, primary: str):
    """Pre-registered: a merchant token is the first alphabetic word (>= 4 letters) of
    merchant_key(description). Candidates have >= 5 posted P&L debits Jan-Sep 2026 led by that
    token and >= 2 distinct merchant_key spellings among rows the decision-9 matcher accepts.
    Excluded: generic banking words, benchmark/few-shot entities, words of the user's account
    and institution names. Highest debit count wins; ties alphabetical."""
    from sqlalchemy import select

    from app.models.account import Account
    from app.models.payee import Payee
    from app.models.transaction import Transaction
    from app.services._query_filters import counts_as_pnl, is_split_parent
    from mcp_server.tools._helpers import merchant_key

    names = (await session.execute(select(Account.name, Account.display_name).where(Account.workspace_id == ws))).all()
    own_words = {w.lower() for n, d in names for w in re.findall(r"[A-Za-z]{4,}", f"{n} {d or ''}")}
    rows = (await session.execute(
        select(Transaction.description, Transaction.payee, Transaction.notes, Payee.name)
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(Transaction.workspace_id == ws, Transaction.type == "debit", Transaction.status == "posted",
               Transaction.date >= date(2026, 1, 1), Transaction.date <= date(2026, 9, 30),
               Transaction.source != "opening_balance", counts_as_pnl(), ~is_split_parent())
    )).all()
    lead = Counter()
    for desc, *_ in rows:
        words = re.findall(r"[A-Za-z]{4,}", merchant_key(desc or ""))
        if words:
            lead[words[0].lower()] += 1
    cands = []
    for tok, n in lead.items():
        if n < 5 or tok in _GENERIC_TOKENS or tok in own_words:
            continue
        pats = merchant_patterns(tok)
        spellings = {merchant_key(d or "") for d, p, no, pn in rows if merchant_hit([d, p, no, pn], pats)}
        if len(spellings) >= 2:
            cands.append((n, tok, len(spellings)))
    cands.sort(key=lambda c: (-c[0], c[1]))
    return cands


async def cmd_probe(args) -> None:
    from sqlalchemy import func, select

    from app.models.account import Account
    from app.models.category import Category
    from app.models.transaction import Transaction
    from app.services import account_service

    maker = await _app()
    async with maker() as s:
        user, agent = await _user_agent(s)
        ws = agent.workspace_id
        primary = user.primary_currency or "USD"
        today = date.today()

        print("== merchant B candidates (token, debits Jan-Sep 2026, spellings) ==")
        cands = await _merchant_b_candidates(s, ws, primary)
        for n, tok, sp in cands[:10]:
            print(f"  {tok:20} debits={n:4} spellings={sp}")
        print(f"  -> merchant B = {cands[0][1] if cands else None!r}")

        print("== accounts typed checking/savings (no balances) ==")
        accounts = await account_service.get_accounts(s, ws)
        cp_cat = select(Category.id).where(func.lower(Category.name) == "card payment")
        for a in accounts:
            if a["type"] not in ("checking", "savings"):
                continue
            cb = float(a["current_balance"])
            n_cp = await s.scalar(select(func.count()).where(Transaction.account_id == a["id"], Transaction.type == "credit",
                                                             Transaction.category_id.in_(cp_cat), Transaction.date >= today - timedelta(days=365)))
            lbl = a.get("display_name") or a["name"]
            print(f"  {lbl[:34]:34} inst={(a.get('institution_name') or '-')[:18]:18} type={a['type']:8} sign={'+' if cb > 0 else '-' if cb < 0 else '0'} "
                  f"retirement_like={bool(RET_RE.search(lbl))} crypto_like={bool(CRYPTO_ACCT_RE.search(lbl))} "
                  f"credit_limit={a.get('credit_limit') is not None} card_payment_credits_365d={n_cp}")
        print("== account types ==", dict(Counter(a["type"] for a in accounts)))
        a0990 = [a for a in accounts if (a.get("masked_number") or "").endswith("0990")]
        print("== ····0990 ==", [(a.get("display_name") or a["name"], a["type"]) for a in a0990])

        print("== history depth (earliest posted date per account) ==")
        firsts = (await s.execute(select(Transaction.account_id, func.min(Transaction.date)).where(
            Transaction.workspace_id == ws, Transaction.status == "posted", Transaction.source != "opening_balance").group_by(Transaction.account_id))).all()
        first_by = {str(k): v for k, v in firsts}
        short = [(a.get("display_name") or a["name"], first_by.get(str(a["id"]))) for a in accounts
                 if first_by.get(str(a["id"])) and first_by[str(a["id"])] > today - timedelta(days=365)]
        print(f"  accounts with < 365 days of history: {len(short)} of {len(accounts)}")
        for n, d in sorted(short, key=lambda x: x[1]):
            print(f"    {n[:34]:34} since {d.isoformat()[:7]}")

        print("== interest charges ==")
        for days in (90, 365):
            rows = (await s.execute(select(Account.type, Transaction.description).join(Account, Account.id == Transaction.account_id).where(
                Transaction.workspace_id == ws, Transaction.type == "debit", Transaction.date >= today - timedelta(days=days),
                Transaction.description.ilike("%interest%") | Transaction.description.ilike("%finance charge%")))).all()
            hits = Counter(t for t, d in rows if INTEREST_RE.search(d or ""))
            print(f"  last {days} days: {dict(hits)}")

        print("== top 15 positions by value (bench classifier, share of invested assets) ==")
        facts = await advisor_facts(s, user, agent, today)
        inv = facts["invested_assets"] or 1
        for p in facts["top15"]:
            print(f"  {(p['ticker'] or '-'):8} {(p['name'] or '')[:32]:32} {p['cls']:10} {100 * p['value'] / inv:5.1f}%  acct={(p['account'] or 'unlinked')[:24]}")
        print("  classes (% of invested):", {k: round(100 * v / inv, 1) for k, v in facts["classes"].items()})

        print("== v1 q11 (Uber June 2026): substring truth vs decision-9 matcher ==")
        sub = (await s.execute(select(Transaction.description).where(
            Transaction.workspace_id == ws, Transaction.type == "debit", Transaction.is_ignored.is_(False),
            Transaction.date >= date(2026, 6, 1), Transaction.date <= date(2026, 6, 30), func.lower(Transaction.description).like("%uber%")))).scalars().all()
        pats = merchant_patterns("uber")
        only_sub = Counter((d or "")[:30] for d in sub if not merchant_hit([d], pats))
        mr = await merchant_rows(s, ws, primary, "uber", date(2026, 6, 1), date(2026, 6, 30))
        print(f"  substring debits={len(sub)}  matcher charges={mr['n_charges']} refunds={mr['n_refunds']} pending={mr['n_pending']}")
        print(f"  substring-only descriptions: {dict(only_sub)}")
        print("== rules the bench sees firing ==", facts["rule_order"])


# ---------------------------------------------------------------------------- summarize


def _load(paths: list[str]) -> list[dict]:
    rows = []
    for p in paths:
        for line in open(p):
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _runs(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("kind") in (None, "run") and r.get("part") == "qa":
            r.setdefault("set", "v1")
            out.append(r)
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _fmt(x, pct=True):
    return "n/a" if x is None else (f"{100 * x:5.1f}%" if pct else f"{x:.2f}")


def _kdist(rows: list[dict], arm: str, set_name: str, key: str = "acc") -> dict[int, int]:
    """Per question: how many reps scored full marks."""
    by_q: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r["arm"] == arm and r["set"] == set_name and r.get(key) is not None:
            by_q[r["q"]].append(r[key])
    return {q: sum(1 for v in vs if v >= 0.999) for q, vs in by_q.items()}


def cmd_summarize(args) -> None:
    rows = _load(args.jsonl)
    runs = _runs(rows)
    arms = list(dict.fromkeys(r["arm"] for r in runs))
    out: list[str] = []
    for set_name in ("v1", "v2"):
        rs = [r for r in runs if r["set"] == set_name]
        if not rs:
            continue
        out.append(f"== {set_name}: {len(rs)} runs ==")
        out.append(f"{'arm':14} {'accuracy':>9} {'fact':>7} {'err%':>5} {'unv/num':>8} {'median s':>9} {'reps':>14}")
        for arm in arms:
            ar = [r for r in rs if r["arm"] == arm]
            if not ar:
                continue
            reps = sorted({r["rep"] for r in ar})
            per_rep = ", ".join(f"{100 * _mean([r['acc'] for r in ar if r['rep'] == k]):.1f}" for k in reps)
            nums = sum(r.get("numbers_written") or 0 for r in ar)
            unv = sum(r.get("unverifiable") or 0 for r in ar)
            out.append(f"{arm:14} {_fmt(_mean([r['acc'] for r in ar])):>9} {_fmt(_mean([r.get('acc_fact') for r in ar])):>7} "
                       f"{100 * sum(1 for r in ar if r['errors']) / len(ar):4.0f}% {(unv / nums if nums else 0):8.3f} "
                       f"{statistics.median(r['wall'] for r in ar):9.0f} {per_rep:>14}")
            if set_name == "v1" and any("acc_corrected" in r for r in ar):
                for qn in sorted({r["q"] for r in ar if "acc_corrected" in r}):
                    fz = _mean([r["acc"] for r in ar if r["q"] == qn])
                    cz = _mean([r.get("acc_corrected") for r in ar if r["q"] == qn])
                    out.append(f"{'':14} q{qn:02d} frozen={_fmt(fz)} corrected={_fmt(cz)}")
            k = _kdist(rs, arm, set_name)
            dist = Counter(k.values())
            out.append(f"{'':14} questions by full-mark reps: " + "  ".join(f"{n}/{max(reps) - min(reps) + 1}:{dist.get(n, 0)}" for n in sorted(dist, reverse=True)))
    if args.stage:
        out.extend(_criteria(runs, args))
    text = "\n".join(out)
    print(text)
    if args.out:
        open(args.out, "w").write(text + "\n")


def _criteria(runs: list[dict], args) -> list[str]:
    base, new = args.base, args.new
    out = [f"== stage {args.stage} criteria: base={base} new={new} =="]

    def regression(set_name: str, exclude: set[int]) -> tuple[bool, list[int]]:
        kb, kn = _kdist(runs, base, set_name), _kdist(runs, new, set_name)
        reps = len({r["rep"] for r in runs if r["arm"] == new and r["set"] == set_name}) or 3
        bad = [q for q in kb if q not in exclude and kb[q] == reps and kn.get(q, 0) <= 1]
        return (not bad), bad

    if args.stage == "A":
        k_new = _kdist(runs, new, "v1")
        k_cor = _kdist(runs, new, "v1", key="acc_corrected")
        a1 = k_new.get(8, 0) >= 2 and k_cor.get(8, 0) >= 2 and k_new.get(11, 0) >= 2
        # q12 (FI number) is excluded: its truth definition changes on purpose with the coverage fix
        a2, bad = regression("v1", {8, 11, 12})
        k12f, k12c = _kdist(runs, new, "v1").get(12, 0), _kdist(runs, new, "v1", key="acc_corrected").get(12, 0)
        errs = sum(1 for r in runs if r["arm"] == new and r["errors"])
        out.append(f"A1 q08 frozen={k_new.get(8, 0)}/3 corrected={k_cor.get(8, 0)}/3 q11={k_new.get(11, 0)}/3 -> {'PASS' if a1 else 'FAIL'}")
        out.append(f"A2 no 3/3 -> <=1/3 regressions on the other 13 -> {'PASS' if a2 else 'FAIL ' + str(bad)}")
        out.append(f"   q12 FI number (excluded, reported): frozen={k12f}/3 coverage-corrected={k12c}/3")
        out.append(f"A3 errors in {new}: {errs} -> {'PASS' if errs == 0 else 'FAIL'}")
    else:
        b1, bad = regression("v1", set())
        out.append(f"B1 v1 per-question regressions -> {'PASS' if b1 else 'FAIL ' + str(bad)}")
        v2 = [r for r in runs if r["set"] == "v2"]
        fb = _mean([r.get("acc_fact") for r in v2 if r["arm"] == base])
        fn = _mean([r.get("acc_fact") for r in v2 if r["arm"] == new])
        loop = args.loop
        fa_new = _mean([r.get("acc_fact_app") for r in v2 if r["arm"] == new])
        fa_loop = _mean([r.get("acc_fact_app") for r in v2 if r["arm"] == loop]) if loop else None
        out.append(f"B2 (informational) v2 fact: {base}={_fmt(fb)} {new}={_fmt(fn)} diff={_fmt((fn - fb) if fn is not None and fb is not None else None)}; "
                   f"app-standard facts {new}={_fmt(fa_new)} {loop}={_fmt(fa_loop)}")
        adv = [r for r in v2 if r["arm"] == new and r.get("expected_rule") is not None]
        by_q: dict[int, list[dict]] = defaultdict(list)
        for r in adv:
            by_q[r["q"]].append(r)
        agree_q = sum(1 for q, rs in by_q.items()
                      if sum(1 for r in rs if r.get("rule") == r.get("expected_rule") and r.get("next_move_lines") and r.get("next_move_amount_ok")) >= 2)
        distinct = len({(r.get("next_move_text") or "")[:60] for r in adv if r.get("next_move_text")})
        out.append(f"B3 rule-engine agreement: {agree_q}/{len(by_q)} advice questions (need >= 8 of 9); distinct next-move lines={distinct} -> "
                   f"{'PASS' if agree_q >= 8 else 'FAIL'}")
        new_all = [r for r in runs if r["arm"] == new]
        base_all = [r for r in runs if r["arm"] == base]
        sell = sum(1 for r in new_all if r.get("sell_lint"))
        hedge_new = sum(1 for r in new_all if r.get("hedges")) / max(len(new_all), 1)
        hedge_base = sum(1 for r in base_all if r.get("hedges")) / max(len(base_all), 1)
        out.append(f"B4 sell/buy-ticker hits={sell}; hedge rate {new}={_fmt(hedge_new)} {base}={_fmt(hedge_base)} -> "
                   f"{'PASS' if sell == 0 and hedge_new <= 0.05 and hedge_new <= hedge_base else 'FAIL'}")

        def unv_rate(rs):
            n = sum(r.get("numbers_written") or 0 for r in rs)
            return (sum(r.get("unverifiable") or 0 for r in rs) / n) if n else 0.0

        ub, un = unv_rate(base_all), unv_rate(new_all)
        out.append(f"B5 unverifiable rate {new}={_fmt(un)} {base}={_fmt(ub)} -> {'PASS' if un <= ub + 0.02 else 'FAIL'}")
        med = statistics.median([r["wall"] for r in v2 if r["arm"] == new]) if any(r["arm"] == new for r in v2) else None
        out.append(f"B7 (secondary) median v2 latency {new}={med if med is None else round(med)} s -> {'PASS' if med is not None and med <= 30 else 'FAIL'}")
        out.append("B6 user review: manual (fact table + 8 user questions)")
    return out


# ============================================================================ entry


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    t = sub.add_parser("truth")
    t.add_argument("--out", default="/tmp/bench/truth.json")
    r = sub.add_parser("run")
    r.add_argument("--truth", default="/tmp/bench/truth.json")
    r.add_argument("--arms", required=True)
    r.add_argument("--set", default="v1")
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--rep-offset", type=int, default=0)
    r.add_argument("--interleave", action="store_true")
    r.add_argument("--loop-v2-only", action="store_true", help="loop* arms skip v1")
    r.add_argument("--keep-snippets", action="store_true")
    r.add_argument("--force", action="store_true", help="run even if the window fingerprint changed")
    r.add_argument("--out", default="/tmp/bench")
    sm = sub.add_parser("summarize")
    sm.add_argument("jsonl", nargs="+")
    sm.add_argument("--stage", choices=["A", "B"])
    sm.add_argument("--base")
    sm.add_argument("--new")
    sm.add_argument("--loop")
    sm.add_argument("--out")
    sub.add_parser("cleanup")
    args = ap.parse_args()
    if args.cmd == "summarize":
        cmd_summarize(args)
        return
    fn = {"probe": cmd_probe, "truth": cmd_truth, "run": cmd_run, "cleanup": cmd_cleanup}[args.cmd]
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
