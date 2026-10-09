"""Figures for the advisor's guided answers: which accounts a question names, what one
merchant cost. Code only — no model call; the guided handlers turn these into tables.

Definitions are pinned in the plan (decisions 2, 9 and 10 of the qc19 advisor plan):

- Accounts are matched by type words ("cash", "cards", "brokerage", "retirement") and by
  the remaining words against the institution, the account's names and the last four
  digits, each at a word start, so "Robinhood" finds every Robinhood account.
- A merchant query matches a word that *starts* with it ("UBER" → "UBER *EATS", "Uber
  Trip", "UBEREATS", never "NEUBERGER"); tokens of three letters or fewer must also end
  there. Charges are posted P&L debits (transfers and card payments are not spending),
  refunds are credits, pending rows are counted but not added.
"""
from __future__ import annotations

import difflib
import re
import uuid
from collections import defaultdict
from datetime import date
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.payee import Payee
from app.models.transaction import Transaction
from app.services._query_filters import counts_as_pnl, is_split_parent

RETIREMENT_RE = re.compile(r"401\s*\(?k\)?|401k|403\s*\(?b\)?|\b457\b|\bIRA\b|\bRoth\b|Retirement|Pension|Previd|Aposentad", re.IGNORECASE)

# words that pick account types; "retirement" is a name test on investment accounts
_TYPE_WORDS: dict[str, frozenset[str]] = {
    "cash": frozenset({"checking", "savings"}),
    "checking": frozenset({"checking"}),
    "savings": frozenset({"savings"}),
    "saving": frozenset({"savings"}),
    "card": frozenset({"credit_card"}),
    "cards": frozenset({"credit_card"}),
    "credit": frozenset({"credit_card"}),
    "debt": frozenset({"credit_card"}),
    "brokerage": frozenset({"investment"}),
    "investment": frozenset({"investment"}),
    "investments": frozenset({"investment"}),
    "retirement": frozenset({"investment"}),
}
_STOP_WORDS = frozenset({
    "my", "the", "all", "account", "accounts", "total", "balance", "balances", "in", "of", "and", "combined",
    "on", "at", "for", "what", "whats", "how", "much", "do", "i", "have", "owe", "is", "are", "money", "right", "now",
    "across", "both", "every", "each", "current", "currently",
})


def _tokens(text: str) -> list[str]:
    cleaned = (text or "").lower().replace("'", "").replace("’", "")
    return [t for t in re.split(r"[^a-z0-9&]+", cleaned) if t]


def _word_start(token: str) -> re.Pattern[str]:
    pattern = r"(?<![a-z0-9])" + re.escape(token)
    if len(token) <= 3:
        pattern += r"(?![a-z0-9])"
    return re.compile(pattern, re.IGNORECASE)


def account_label(account: dict[str, Any]) -> str:
    return account.get("display_name") or account.get("name") or "?"


def match_accounts(query: str, accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """`accounts` are `account_service.get_accounts` rows. Returns the matches, the type
    filter that applied, whether the query was only about types, and close names when
    nothing matched."""
    words = [t for t in _tokens(query) if t not in _STOP_WORDS]
    types: set[str] = set()
    retirement = False
    entity: list[str] = []
    for w in words:
        if w in _TYPE_WORDS:
            types |= _TYPE_WORDS[w]
            retirement = retirement or w == "retirement"
        else:
            entity.append(w)
    if not types and not entity:
        return {"matches": [], "types": [], "type_only": False, "candidates": []}
    pats = [_word_start(t) for t in entity]

    def entity_hit(acc: dict[str, Any]) -> bool:
        fields = [acc.get("institution_name") or "", acc.get("display_name") or "", acc.get("name") or ""]
        masked = acc.get("masked_number") or ""
        return all(any(p.search(f) for f in fields) or (tok.isdigit() and masked.endswith(tok))
                   for tok, p in zip(entity, pats))

    matches = []
    for acc in accounts:
        if types and acc.get("type") not in types:
            continue
        if retirement and not RETIREMENT_RE.search(f"{acc.get('name') or ''} {acc.get('display_name') or ''}"):
            continue
        if entity and not entity_hit(acc):
            continue
        matches.append(acc)
    candidates: list[str] = []
    if not matches:
        names = sorted({n for a in accounts for n in (account_label(a), a.get("institution_name")) if n})
        candidates = difflib.get_close_matches(query or "", names, n=3, cutoff=0.5)
    return {"matches": matches, "types": sorted(types), "type_only": bool(types) and not entity, "candidates": candidates}


# --- merchants ------------------------------------------------------------------

_MERCHANT_ALIASES: dict[str, tuple[str, ...]] = {"amazon": ("amazon", "amzn")}


def merchant_patterns(query: str) -> list[re.Pattern[str]]:
    """One pattern per query word (all must match); a word may carry spelling aliases."""
    pats = []
    for tok in _tokens(query):
        alts = []
        for alias in _MERCHANT_ALIASES.get(tok, (tok,)):
            pattern = r"(?<![a-z0-9])" + re.escape(alias)
            if len(alias) <= 3:
                pattern += r"(?![a-z0-9])"
            alts.append(pattern)
        pats.append(re.compile("(?:" + "|".join(alts) + ")", re.IGNORECASE))
    return pats


def merchant_hit(columns: list[Optional[str]], pats: list[re.Pattern[str]]) -> bool:
    return bool(pats) and any(c and all(p.search(c) for p in pats) for c in columns)


async def category_named(session: AsyncSession, workspace_id: uuid.UUID, query: str, message: str = "") -> Optional[str]:
    """The category the user means, only on an exact match: the whole query equals a
    category name (case-insensitive), or a full category name appears in the message.
    "Amazon" stays a merchant; "Amazon & online" is a category."""
    names = (await session.execute(select(Category.name).where(Category.workspace_id == workspace_id))).scalars().all()
    q = (query or "").strip().lower()
    for name in names:
        if name and name.strip().lower() == q:
            return name
    text = (message or "").lower()
    for name in sorted((n for n in names if n), key=len, reverse=True):
        if len(name) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", text):
            return name
    return None


def _amount(amount: Any, amount_primary: Any, currency: Optional[str], primary: str) -> float:
    if currency != primary and amount_primary is not None:
        return float(amount_primary)
    return float(amount)


async def merchant_rows(
    session: AsyncSession, workspace_id: uuid.UUID, primary: str, query: str, from_date: date, to_date: date
) -> dict[str, Any]:
    """Charges, refunds and pending rows for one merchant between two dates (Transaction.date)."""
    from mcp_server.tools._helpers import merchant_key

    pats = merchant_patterns(query)
    toks = _tokens(query)
    if not pats or not toks:
        return {"charges": 0.0, "refunds": 0.0, "charge_count": 0, "refund_count": 0, "pending_count": 0,
                "transfer_matches": 0, "months": {}, "variants": []}
    likes = _MERCHANT_ALIASES.get(toks[0], (toks[0],))
    columns = (Transaction.description, Transaction.payee, Transaction.notes, Payee.name)
    prefilter = or_(*[c.ilike(f"%{a}%") for a in likes for c in columns])
    base = (
        select(Transaction.type, Transaction.status, Transaction.amount, Transaction.amount_primary, Transaction.currency,
               Transaction.date, Transaction.description, Transaction.payee, Transaction.notes, Payee.name)
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(Transaction.workspace_id == workspace_id, Transaction.date >= from_date, Transaction.date <= to_date,
               Transaction.source != "opening_balance", ~is_split_parent(), prefilter)
    )
    rows = (await session.execute(base.where(counts_as_pnl()))).all()
    transfer_rows = (await session.execute(base.where(~counts_as_pnl()))).all()
    charges = refunds = 0.0
    charge_count = refund_count = pending_count = 0
    months: dict[str, float] = defaultdict(float)
    variants: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for typ, status, amt, amt_p, cur, when, desc, payee, notes, pname in rows:
        if not merchant_hit([desc, payee, notes, pname], pats):
            continue
        if status != "posted":
            pending_count += 1
            continue
        value = _amount(amt, amt_p, cur, primary)
        if typ == "debit":
            charges += value
            charge_count += 1
            months[when.strftime("%Y-%m")] += value
            v = variants[merchant_key(desc or "")[:40] or "(blank)"]
            v[0] += 1
            v[1] += value
        else:
            refunds += value
            refund_count += 1
    transfer_matches = sum(1 for r in transfer_rows if merchant_hit([r[6], r[7], r[8], r[9]], pats))
    top_variants = sorted(variants.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    return {
        "charges": round(charges, 2),
        "refunds": round(refunds, 2),
        "charge_count": charge_count,
        "refund_count": refund_count,
        "pending_count": pending_count,
        "transfer_matches": transfer_matches,
        "months": {k: round(v, 2) for k, v in sorted(months.items())},
        "variants": [{"description": k, "count": int(c), "total": round(t, 2)} for k, (c, t) in top_variants],
    }


# --- asset classes (plan decision 3, with D2) -----------------------------------------

BROAD_TICKERS = frozenset({"VTI", "VOO", "SPY", "IVV", "VT", "VXUS", "ITOT", "SCHB", "FXAIX", "FSKAX", "FZROX",
                           "VTSAX", "VFIAX", "SWTSX", "SWPPX"})
_BROAD_NAME_RE = re.compile(r"\b(total (stock )?market|s&p 500|target (date|retirement)|total world|all.?world|total international)\b", re.IGNORECASE)
_INDEX_RE = re.compile(r"\bindex\b", re.IGNORECASE)
_SECTOR_RE = re.compile(r"\b(technology|health|energy|financial|real estate|utilities|semiconductor|dividend|growth|value)\b", re.IGNORECASE)
CRYPTO_RE = re.compile(r"crypto|bitcoin|coinbase|ethereum", re.IGNORECASE)
_CRYPTO_TICKERS = frozenset({"BTC", "ETH", "SOL", "DOGE", "ADA", "XRP", "LTC"})
_CRYPTO_NAME_RE = re.compile(r"bitcoin|ethereum", re.IGNORECASE)
_CASH_NAME_RE = re.compile(r"\b(cash|money market|sweep)\b", re.IGNORECASE)
_CASH_TICKERS = frozenset({"SPAXX", "FDRXX", "VMFXX", "SWVXX", "SPRXX", "FZFXX"})
_BOND_RE = re.compile(r"\b(bond|treasury|fixed income)\b", re.IGNORECASE)
_BOND_TICKERS = frozenset({"BND", "AGG", "BNDX"})
_FUND_RE = re.compile(r"\b(fund|etf|trust|portfolio)\b", re.IGNORECASE)
# SimpleFIN names positions "189.745 shares of PLTR": nothing to read beyond the ticker
_SHARES_OF_RE = re.compile(r"^\s*[\d.,]+\s+shares?\s+of\s+\S+\s*$", re.IGNORECASE)

ASSET_CLASS_LABELS = {
    "broad": "Broad index funds", "fund_other": "Other funds (sector, dividend, factor)", "stock": "Single stocks",
    "crypto": "Crypto", "cash": "Cash and money market", "bond": "Bonds", "other": "Other", "unknown": "Not classified",
    "unreported": "Accounts that report no positions",
}


def classify_asset_class(name: str, ticker: Optional[str], security_type: Optional[str], account_label: str = "") -> str:
    """broad | fund_other | stock | crypto | cash | bond | other | unknown, in the plan's order:
    the provider's security type, then broad funds, crypto, cash, bonds, other funds, stocks."""
    st = (security_type or "").strip().lower().replace("_", " ")
    t = (ticker or "").strip().upper()
    n = "" if _SHARES_OF_RE.match(name or "") else (name or "")
    broad = t in BROAD_TICKERS or bool(_BROAD_NAME_RE.search(n)) or (bool(_INDEX_RE.search(n)) and not _SECTOR_RE.search(n))
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
    if CRYPTO_RE.search(account_label or "") or t in _CRYPTO_TICKERS or _CRYPTO_NAME_RE.search(n):
        return "crypto"
    if _CASH_NAME_RE.search(n) or t in _CASH_TICKERS:
        return "cash"
    if _BOND_RE.search(n) or t in _BOND_TICKERS:
        return "bond"
    if _FUND_RE.search(n):
        return "fund_other"
    return "stock" if t else "unknown"


# --- spending patterns ------------------------------------------------------------------

_INTEREST_RE = re.compile(r"interest charge|finance charge|purchase interest", re.IGNORECASE)
ESSENTIAL_RE = re.compile(
    r"\b(rent|mortgages?|housing|utilit(?:y|ies)|insurance|tax(?:es)?|health(?:care)?|medical|childcare|loans?|debts?)\b",
    re.IGNORECASE,
)
_NON_CATEGORY_RE = re.compile(r"^\s*(uncategori[sz]ed|other\b.*)$", re.IGNORECASE)


def _month_start(d: date, back: int) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _month_end(d: date) -> date:
    import calendar

    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def category_spikes(month_cats: dict[date, dict[str, float]], month: date) -> Optional[list[dict[str, Any]]]:
    """Categories in `month` above 1.5x their usual level and at least 200 over it. The usual
    level is the mean of up to 6 complete months before, inside the data coverage; fewer than
    3 such months means too little history (None), never a guess."""
    from app.agents.runtime import advice

    base_months = [bm for bm in (_month_start(month, k) for k in range(1, 7)) if bm in month_cats]
    if len(base_months) < 3 or month not in month_cats:
        return None
    out = []
    for cat, value in month_cats[month].items():
        if _NON_CATEGORY_RE.match(cat or ""):
            continue
        series = [month_cats[bm].get(cat, 0.0) for bm in base_months]
        if sum(1 for x in series if x > 0) < 3:
            continue
        baseline = sum(series) / len(series)
        excess = value - baseline
        if value > advice.SPIKE_RATIO * baseline and excess >= advice.SPIKE_MIN_EXCESS:
            out.append({"category": cat, "amount": round(value, 2), "baseline": round(baseline, 2), "excess": round(excess, 2)})
    return sorted(out, key=lambda s: s["excess"], reverse=True)


def detect_recurring(rows: list[tuple[date, str, float]], today: date) -> list[dict[str, Any]]:
    """Charges from the same merchant in at least 3 of the last 4 complete months, at most 2 a
    month, each month within 15% (or 2.00) of the median. Figures only; no rule uses them."""
    from mcp_server.tools._helpers import merchant_key

    months = [_month_start(today, k) for k in range(1, 5)]
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


# --- the advisor's facts ---------------------------------------------------------------


def _capped_years(value: Optional[float]) -> Optional[float]:
    return value if value is not None and value <= 60 else None


async def advisor_facts(
    session: AsyncSession,
    tool: Any,
    *,
    workspace_id: uuid.UUID,
    primary: str,
    today: date,
) -> dict[str, Any]:
    """Every figure the advice rules read (advice.py facts contract) plus the tables' figures.

    `tool(name, **args)` runs a Securo tool in-process, so FIRE figures are exactly
    fire_projection's (spend and contributions annualized over the days of complete data)
    and the savings rate is the Money Map's over the same window."""
    from sqlalchemy import select as _select

    from app.agents.runtime import advice
    from app.agents.services import digest_service
    from app.models.account import Account
    from app.models.asset import Asset
    from app.services import account_service, fire_service
    from mcp_server.tools.finance import coverage_window

    cov = await coverage_window(session, workspace_id, today)
    scale, days = float(cov["scale"]), int(cov["days"])

    fire = await tool("fire_projection")
    inputs = fire.get("inputs") or {}
    annual_spend = float(inputs.get("annual_spend") or 0.0)
    invested_assets = float(inputs.get("invested_assets") or 0.0)
    annual_contribution = float(inputs.get("annual_contribution") or 0.0)
    real_return = float(inputs.get("real_return") or 0.05)
    withdrawal = float(inputs.get("withdrawal_rate") or 0.04)
    fi_number = float(fire.get("fi_number") or 0.0)
    years_now = _capped_years(fire.get("years_to_fi"))
    monthly_spend = round(annual_spend / 12, 2) if annual_spend else None

    mm = (await tool("get_money_map", days=max(7, min(days, 730)))).get("totals") or {}
    income, expenses = float(mm.get("income") or 0.0), float(mm.get("expenses") or 0.0)
    direct, net = float(mm.get("direct_contributions") or 0.0), float(mm.get("net") or 0.0)
    rate = mm.get("contribution_aware_savings_rate")
    mm90 = (await tool("get_money_map", days=90)).get("totals") or {}
    surplus_month = round(net * scale / 12, 2)
    surplus_seasonal = round(float(mm90.get("net") or 0.0) / 3, 2)

    # holdings: classes, cash inside investment accounts, stocks
    hold = await tool("get_holdings")
    if not invested_assets:
        invested_assets = float(hold.get("investment_accounts_total_primary") or 0.0) + float(
            (hold.get("unlinked_holdings_total_by_currency") or {}).get(primary, 0.0))
    accounts = await account_service.get_accounts(session, workspace_id)
    by_id = {str(a["id"]): a for a in accounts}
    meta = {str(a.id): (a.external_metadata or {}) for a in (await session.execute(_select(Asset).where(Asset.workspace_id == workspace_id))).scalars()}
    classes: dict[str, float] = defaultdict(float)
    retirement_cash = brokerage_cash = 0.0
    positions: list[dict[str, Any]] = []
    known_gain, known_count = 0.0, 0
    for acct in hold.get("accounts") or []:
        name = acct.get("name") or ""
        label = f"{name} {(by_id.get(str(acct.get('account_id'))) or {}).get('institution_name') or ''}"
        balance = float(acct["balance_primary"] if acct.get("balance_primary") is not None else (acct.get("balance") or 0.0))
        is_retirement = bool(RETIREMENT_RE.search(name))
        held = acct.get("holdings") or []
        held_sum = 0.0
        for h in held:
            value = float(h.get("current_value") or 0.0)
            cls = classify_asset_class(h.get("name") or "", h.get("ticker"), meta.get(str(h.get("id")), {}).get("security_type"), label)
            classes[cls] += value
            held_sum += value
            positions.append({"name": h.get("name"), "ticker": h.get("ticker"), "account": name, "value": round(value, 2), "class": cls})
            if h.get("cost_basis_known") and h.get("gain_loss") is not None:
                known_gain += float(h["gain_loss"])
                known_count += 1
            if cls == "cash":
                if is_retirement:
                    retirement_cash += value
                else:
                    brokerage_cash += value
        if held:
            residual = balance - held_sum
            if residual > 0:
                classes["cash"] += residual
                if is_retirement:
                    retirement_cash += residual
                else:
                    brokerage_cash += residual
        else:
            classes["crypto" if CRYPTO_RE.search(label) else "unreported"] += balance
    for h in hold.get("unlinked_holdings") or []:
        if h.get("currency") not in (None, primary):
            continue
        value = float(h.get("current_value") or 0.0)
        cls = classify_asset_class(h.get("name") or "", h.get("ticker"), meta.get(str(h.get("id")), {}).get("security_type"))
        classes[cls] += value
        positions.append({"name": h.get("name"), "ticker": h.get("ticker"), "account": None, "value": round(value, 2), "class": cls})
    stocks: dict[str, dict[str, Any]] = {}
    for p in positions:
        if p["class"] == "stock":
            key = (p["ticker"] or p["name"] or "").upper()
            s = stocks.setdefault(key, {"name": p["name"], "ticker": p["ticker"], "value": 0.0})
            s["value"] = round(s["value"] + p["value"], 2)
    largest_stock = max(stocks.values(), key=lambda s: s["value"], default=None)
    stock_total = round(sum(s["value"] for s in stocks.values()), 2)

    # cash and cards
    checking_savings = round(sum(float(a["current_balance"]) for a in accounts
                                 if a["type"] in ("checking", "savings") and float(a["current_balance"]) > 0), 2)
    overdrawn = [{"name": account_label(a), "balance": round(float(a["current_balance"]), 2)} for a in accounts
                 if a["type"] in ("checking", "savings") and float(a["current_balance"]) < 0]
    liquid_cash = round(checking_savings + brokerage_cash, 2)
    cards = {str(a["id"]): {"card": account_label(a), "owed": round(max(-float(a["current_balance"]), 0.0), 2)}
             for a in accounts if a["type"] == "credit_card"}
    interest_by_card: dict[str, float] = defaultdict(float)
    if cards:
        rows = (await session.execute(
            _select(Transaction.account_id, Transaction.description, Transaction.amount, Transaction.amount_primary, Transaction.currency)
            .join(Account, Account.id == Transaction.account_id)
            .where(Transaction.workspace_id == workspace_id, Account.type == "credit_card", Transaction.type == "debit",
                   Transaction.is_ignored.is_(False), Transaction.date >= date.fromordinal(today.toordinal() - 90), Transaction.date <= today)
        )).all()
        for aid, desc, amt, amt_p, cur in rows:
            if _INTEREST_RE.search(desc or ""):
                interest_by_card[str(aid)] += _amount(amt, amt_p, cur, primary)
    cards_with_interest = [{"card": cards[k]["card"], "interest": round(v, 2), "owed": cards[k]["owed"]}
                           for k, v in interest_by_card.items() if k in cards and v > 0]

    # spending: complete months inside the coverage window, the window's categories, recurring charges
    first_full = cov["from_date"] if cov["from_date"].day == 1 else _month_start(cov["from_date"], -1)
    month_cats: dict[date, dict[str, float]] = {}
    for back in range(1, 13):
        ms = _month_start(today, back)
        if ms < first_full:
            break
        month_cats[ms] = await digest_service.expense_by_category(session, workspace_id, ms, _month_end(ms))
    latest_complete = _month_start(today, 1)
    spikes = category_spikes(month_cats, latest_complete)
    window_cats = await digest_service.expense_by_category(session, workspace_id, cov["from_date"], today)
    controllable = sorted(((c, v) for c, v in window_cats.items() if not ESSENTIAL_RE.search(c or "") and not _NON_CATEGORY_RE.match(c or "")),
                          key=lambda kv: kv[1], reverse=True)
    top_controllable = None
    if controllable:
        monthly = round(controllable[0][1] * scale / 12, 2)
        top_controllable = {"category": controllable[0][0], "amount": monthly, "half": round(monthly / 2, 2)}
    recurring_rows = (await session.execute(
        _select(Transaction.date, Transaction.description, Transaction.amount, Transaction.amount_primary, Transaction.currency)
        .where(Transaction.workspace_id == workspace_id, Transaction.type == "debit", Transaction.status == "posted",
               Transaction.date >= _month_start(today, 4), Transaction.date < date(today.year, today.month, 1),
               Transaction.source != "opening_balance", counts_as_pnl(), ~is_split_parent())
    )).all()
    recurring = detect_recurring([(d, desc, _amount(a, ap, c, primary)) for d, desc, a, ap, c in recurring_rows], today)

    # savings bands, the contribution ladder, the idle-cash lump sum (decision 5)
    def years(balance: float, contribution: float, target: float) -> Optional[float]:
        return _capped_years(fire_service.years_to_target(balance, contribution, real_return, target)) if target > 0 else None

    band_next = next((b for b in advice.SAVINGS_BANDS if rate is not None and rate < b), None)
    band_cut = fi_after_cut = years_after_cut = None
    if band_next is not None:
        band_cut = round((band_next * (income + direct) - (income - expenses + direct)) * scale / 12, 2)
        fi_after_cut = round(max(annual_spend - 12 * band_cut, 0.0) / withdrawal, 2)
        years_after_cut = years(invested_assets, annual_contribution + 12 * band_cut, fi_after_cut)
    ladder = []
    for extra in (500, 1000, 2000):
        needs_cut = round(max(extra - max(surplus_month, 0.0), 0.0), 2)
        ladder.append({"extra": float(extra), "years": years(invested_assets, annual_contribution + 12 * extra, fi_number),
                       "needs_cut": needs_cut})
    lump = None
    if monthly_spend:
        buffer = advice.CASH_BUFFER_MONTHS * monthly_spend + max(advice.IDLE_CASH_MIN, 0.5 * monthly_spend)
        if liquid_cash > buffer:
            idle = round(liquid_cash - buffer, 2)
            lump = {"amount": idle, "years": years(invested_assets + idle, annual_contribution, fi_number)}

    return {
        "coverage": {"from_date": cov["from_date"].isoformat(), "to_date": today.isoformat(), "days": days,
                     "annualized": bool(cov["annualized"]), "insufficient_history": bool(cov["insufficient_history"])},
        "monthly_spend": monthly_spend, "annual_spend": round(annual_spend, 2),
        "annual_contribution": round(annual_contribution, 2), "monthly_contribution": round(annual_contribution / 12, 2),
        "invested_assets": round(invested_assets, 2), "fi_number": round(fi_number, 2), "years_now": years_now,
        "income_annual": round(income * scale, 2), "expenses_annual": round(expenses * scale, 2),
        "direct_annual": round(direct * scale, 2), "savings_rate": rate,
        "surplus_month": surplus_month, "surplus_seasonal": surplus_seasonal,
        "band_next": band_next, "band_cut": band_cut, "fi_after_cut": fi_after_cut, "years_after_cut": years_after_cut,
        "ladder": ladder, "lump": lump,
        "classes": {k: round(v, 2) for k, v in classes.items() if v},
        "positions": sorted(positions, key=lambda p: p["value"], reverse=True),
        "crypto_value": round(classes.get("crypto", 0.0), 2), "stock_total": stock_total, "largest_stock": largest_stock,
        "retirement_cash": round(retirement_cash, 2), "brokerage_cash": round(brokerage_cash, 2),
        "known_gain": round(known_gain, 2), "known_gain_positions": known_count,
        "checking_savings": checking_savings, "liquid_cash": liquid_cash, "overdrawn": overdrawn,
        "months_covered": round(liquid_cash / monthly_spend, 1) if monthly_spend else None,
        "card_owed": round(sum(float(c["owed"]) for c in cards.values()), 2), "cards_with_interest": cards_with_interest,
        "interest_90": round(sum(interest_by_card.values()), 2),
        "spikes": spikes, "spike_month": latest_complete.isoformat(), "month_cats": {k.isoformat(): v for k, v in month_cats.items()},
        "top_controllable": top_controllable, "recurring": recurring,
    }
