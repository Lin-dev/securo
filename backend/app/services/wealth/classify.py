"""Asset classes for holdings, and which categories read as income."""
from __future__ import annotations

import re
from typing import Optional, Protocol

from app.services.category_group_service import DEFAULT_GROUPS_I18N
from app.services.wealth.patterns import CRYPTO_RE

BROAD_TICKERS = frozenset({"VTI", "VOO", "SPY", "IVV", "VT", "VXUS", "ITOT", "SCHB", "FXAIX", "FSKAX", "FZROX",
                           "VTSAX", "VFIAX", "SWTSX", "SWPPX"})
_BROAD_NAME_RE = re.compile(r"\b(total (stock )?market|s&p 500|target (date|retirement)|total world|all.?world|total international)\b", re.IGNORECASE)
_INDEX_RE = re.compile(r"\bindex\b", re.IGNORECASE)
_SECTOR_RE = re.compile(r"\b(technology|health|energy|financial|real estate|utilities|semiconductor|dividend|growth|value)\b", re.IGNORECASE)
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


# --- income categories ------------------------------------------------------------------

INCOME_NAME_RE = re.compile(r"income|salary|wages|renda|sal[aá]rio|receita|ingresos|revenu", re.I)


class NamedCategory(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def group_name(self) -> Optional[str]: ...


def income_group_names() -> set[str]:
    entry = DEFAULT_GROUPS_I18N.get("income", {})
    return {str(v).lower() for k, v in entry.items() if k not in {"icon", "color", "position"}}


def is_income_like(cat: NamedCategory) -> bool:
    group = (cat.group_name or "").strip().lower()
    if group and (group in income_group_names() or INCOME_NAME_RE.search(group)):
        return True
    return bool(INCOME_NAME_RE.search(cat.name or ""))
