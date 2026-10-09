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
