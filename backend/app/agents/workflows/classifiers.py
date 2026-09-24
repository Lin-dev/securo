"""Deterministic pieces of the categorization review.

Pure functions over plain dataclasses — no DB, no model — so every rule that
decides a category without asking the LLM is unit-testable on its own: own-
account transfers ("… ACCOUNT ENDING IN 3154" where 3154 is one of the user's
accounts), credit-card payments, merchants an existing rule already covers,
the sign check that keeps a credit-only merchant out of expense categories,
the confidence thresholds, de-duplication and the per-run cap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal, Optional

from app.services.category_group_service import DEFAULT_GROUPS_I18N
from app.services.rule_engine import _normalize, evaluate_conditions

Source = Literal["transfer", "card_payment", "rule", "llm", "budget"]
Status = Literal["propose", "review", "unknown", "covered", "skipped"]

PROPOSE_AT = 0.85
REVIEW_AT = 0.60

# "ENDING IN 3154", "ACCT #3154", "ACCOUNT NO. 3154", "CONTA 3154", "XXXX3154".
ACCOUNT_DIGITS_RE = re.compile(
    r"(?:ENDING(?:\s+IN)?|ACCT(?:\.|OUNT)?(?:\s*(?:#|NO\.?|NUMBER))?|CONTA|A/C)\s*[X*#]*\s*(\d{4})\b"
)
TRANSFER_WORDS_RE = re.compile(r"\b(?:XFER|TRANSFER|TRANSFERENCIA|ONLINE BANKING TRANSFER)\b")
CARD_PAYMENT_RE = re.compile(
    r"PAYMENT\s*-\s*THANK YOU|AUTOPAY\s*PAYMENT|AUTOMATIC PAYMENT|ONLINE PAYMENT|MOBILE PAYMENT"
    r"|PAYMENT RECEIVED|PGTO\.?\s*FATURA|PAGAMENTO (?:DE )?FATURA|CARD PAYMENT|CREDIT CARD PMT"
)
CARD_WORDS_RE = re.compile(r"\b(?:CARD|CARTAO|VISA|MASTERCARD|AMEX|AMERICAN EXPRESS|DISCOVER)\b")
REFUND_RE = re.compile(r"refund|reembolso|estorno|cashback|\bcredits?\b", re.I)
INCOME_NAME_RE = re.compile(r"income|salary|wages|renda|sal[aá]rio|receita|ingresos|revenu", re.I)
INTERNAL_TRANSFER_RE = r"internal transfer|transfer|transferencia|transferência"
CARD_PAYMENT_CATEGORY_RE = r"card payment|credit card|pagamento de cart|fatura"


@dataclass(frozen=True)
class AccountFacts:
    name: str
    type: str
    masked_number: Optional[str] = None

    @property
    def last4(self) -> Optional[str]:
        """Last four digits, from the provider's mask or from a "(3154)" /
        trailing-digits suffix in the account name."""
        if self.masked_number and self.masked_number.strip():
            return self.masked_number.strip()[-4:]
        m = re.search(r"\(?(\d{4})\)?\s*$", self.name or "")
        return m.group(1) if m else None


@dataclass(frozen=True)
class CategoryFacts:
    id: str
    name: str
    group_name: Optional[str] = None
    treat_as_transfer: bool = False


@dataclass
class Merchant:
    merchant: str
    pattern: str
    count: int
    total: float
    type_mix: dict[str, int] = field(default_factory=dict)
    accounts: list[str] = field(default_factory=list)
    sample_descriptions: list[str] = field(default_factory=list)
    sample_transaction_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "Merchant":
        return cls(
            merchant=str(item.get("merchant") or ""),
            pattern=str(item.get("pattern_suggestion") or item.get("merchant") or ""),
            count=int(item.get("count") or 0),
            total=float(item.get("total") or 0.0),
            type_mix={str(k): int(v) for k, v in (item.get("type_mix") or {}).items()},
            accounts=[str(a) for a in (item.get("accounts") or [])],
            sample_descriptions=[str(d) for d in (item.get("sample_descriptions") or [])],
            sample_transaction_ids=[str(i) for i in (item.get("sample_transaction_ids") or [])],
        )

    @property
    def debits(self) -> int:
        return int(self.type_mix.get("debit", 0))

    @property
    def credits(self) -> int:
        return int(self.type_mix.get("credit", 0))

    @property
    def credit_only(self) -> bool:
        return self.credits > 0 and self.debits == 0

    @property
    def debit_only(self) -> bool:
        return self.debits > 0 and self.credits == 0

    @property
    def average(self) -> float:
        return round(self.total / self.count, 2) if self.count else 0.0

    def normalized_samples(self) -> list[str]:
        texts = self.sample_descriptions or [self.merchant]
        return [_normalize(t) for t in texts if t]


@dataclass
class Decision:
    merchant: Merchant
    category: Optional[CategoryFacts]
    confidence: float
    source: Source
    status: Status
    reason: str
    covered_by: Optional[str] = None
    alternatives: list[tuple[str, float]] = field(default_factory=list)


# --- category helpers --------------------------------------------------------------


def income_group_names() -> set[str]:
    entry = DEFAULT_GROUPS_I18N.get("income", {})
    return {str(v).lower() for k, v in entry.items() if k not in {"icon", "color", "position"}}


def is_income_like(cat: CategoryFacts) -> bool:
    group = (cat.group_name or "").strip().lower()
    if group and (group in income_group_names() or INCOME_NAME_RE.search(group)):
        return True
    return bool(INCOME_NAME_RE.search(cat.name or ""))


def find_transfer_category(cats: list[CategoryFacts], pattern: str) -> Optional[CategoryFacts]:
    rx = re.compile(pattern, re.I)
    for cat in cats:
        if cat.treat_as_transfer and rx.search(cat.name or ""):
            return cat
    return None


def find_category_by_name(cats: list[CategoryFacts], name: str) -> Optional[CategoryFacts]:
    key = (name or "").strip().lower()
    for cat in cats:
        if cat.name.strip().lower() == key:
            return cat
    return None


# --- deterministic classifiers ---------------------------------------------------------


def _own_last4(accounts: list[AccountFacts]) -> dict[str, AccountFacts]:
    out: dict[str, AccountFacts] = {}
    for acc in accounts:
        last4 = acc.last4
        if last4 and last4 not in out:
            out[last4] = acc
    return out


def classify_own_account_transfer(
    m: Merchant, accounts: list[AccountFacts], cats: list[CategoryFacts]
) -> Optional[Decision]:
    """Money moving between the user's own accounts is never income or expense.

    Digits that name one of the user's accounts decide it; digits that do not,
    or transfer words without digits, are left for the user to confirm.
    """
    own = _own_last4(accounts)
    samples = m.normalized_samples()
    transfer_cat = find_transfer_category(cats, INTERNAL_TRANSFER_RE)
    seen_digits: list[str] = []
    for text in samples:
        for match in ACCOUNT_DIGITS_RE.finditer(text):
            digits = match.group(1)
            if digits not in seen_digits:
                seen_digits.append(digits)
    ours = [d for d in seen_digits if d in own]
    if ours:
        acc = own[ours[0]]
        reason = f"transfer with your account ····{ours[0]} ({acc.name})"
        if transfer_cat is None:
            return Decision(m, None, 0.98, "transfer", "review", reason + "; no transfer-type category named like 'Internal transfer' exists")
        return Decision(m, transfer_cat, 0.98, "transfer", "propose", reason)
    if seen_digits:
        return Decision(
            m, transfer_cat, 0.60, "transfer", "review",
            f"transfer with an account ending in {seen_digits[0]} that is not in Securo",
        )
    if any(TRANSFER_WORDS_RE.search(text) for text in samples):
        return Decision(m, transfer_cat, 0.70, "transfer", "review", "transfer keywords but no account of yours matched")
    return None


def classify_card_payment(
    m: Merchant, accounts: list[AccountFacts], cats: list[CategoryFacts]
) -> Optional[Decision]:
    samples = m.normalized_samples()
    if not any(CARD_PAYMENT_RE.search(text) for text in samples):
        return None
    by_name = {a.name: a for a in accounts}
    on_card = any((by_name.get(name) or AccountFacts(name, "")).type == "credit_card" for name in m.accounts)
    if not on_card and not any(CARD_WORDS_RE.search(text) for text in samples):
        return None
    cat = find_transfer_category(cats, CARD_PAYMENT_CATEGORY_RE)
    reason = "payment to your own credit card"
    if cat is None:
        return Decision(m, None, 0.95, "card_payment", "review", reason + "; no transfer-type category named like 'Card payment' exists")
    return Decision(m, cat, 0.95, "card_payment", "propose", reason)


def _leaves(conditions: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in conditions or []:
        if isinstance(node, dict) and isinstance(node.get("conditions"), list):
            out.extend(c for c in node["conditions"] if isinstance(c, dict))
        elif isinstance(node, dict):
            out.append(node)
    return out


def covered_by_rule(m: Merchant, rules: list[dict[str, Any]]) -> Optional[str]:
    """Name of an active description-only rule that already categorizes every
    sample of this merchant (same engine semantics as a real run)."""
    texts = m.sample_descriptions or [m.merchant]
    for rule in rules or []:
        if not rule.get("is_active", True):
            continue
        actions = rule.get("actions") or []
        if not any(isinstance(a, dict) and a.get("op") == "set_category" for a in actions):
            continue
        conditions = rule.get("conditions") or []
        leaves = _leaves(conditions)
        if not leaves or any(leaf.get("field") != "description" for leaf in leaves):
            continue
        op = rule.get("conditions_op") or "and"
        if all(evaluate_conditions(op, conditions, SimpleNamespace(description=t)) for t in texts):
            return str(rule.get("name") or "rule")
    return None


# --- validation, thresholds, shaping --------------------------------------------------------


def validate_sign(m: Merchant, cat: CategoryFacts) -> Optional[str]:
    """None when the category is compatible with the money direction, else why not."""
    if m.credit_only and not (is_income_like(cat) or cat.treat_as_transfer):
        return "credit-only merchant cannot be an expense category"
    if m.debit_only and is_income_like(cat) and not REFUND_RE.search(cat.name or ""):
        return "debit-only merchant cannot be income"
    return None


def apply_thresholds(confidence: float, propose_at: float = PROPOSE_AT, review_at: float = REVIEW_AT) -> Status:
    if confidence >= propose_at:
        return "propose"
    if confidence >= review_at:
        return "review"
    return "unknown"


def dedupe_by_pattern(decisions: list[Decision]) -> tuple[list[Decision], list[str]]:
    """One decision per pattern (the one with the most rows wins); patterns too
    short to be a safe `contains` rule are skipped."""
    notes: list[str] = []
    kept: dict[str, Decision] = {}
    out: list[Decision] = []
    for d in decisions:
        key = (d.merchant.pattern or "").strip().upper()
        if key in ("", "(BLANK)") or len(key) < 3:
            d.status = "skipped"
            d.reason = "pattern too short for a safe rule"
            out.append(d)
            continue
        prev = kept.get(key)
        if prev is None:
            kept[key] = d
            out.append(d)
            continue
        if d.merchant.count > prev.merchant.count:
            notes.append(f"`{prev.merchant.merchant}` merged into `{d.merchant.merchant}` (same pattern)")
            prev.status = "skipped"
            prev.reason = f"merged into {d.merchant.merchant} (same pattern)"
            kept[key] = d
            out.append(d)
        else:
            notes.append(f"`{d.merchant.merchant}` merged into `{prev.merchant.merchant}` (same pattern)")
            d.status = "skipped"
            d.reason = f"merged into {prev.merchant.merchant} (same pattern)"
            out.append(d)
    return out, notes


def cap_proposals(decisions: list[Decision], limit: int = 25) -> tuple[list[Decision], list[Decision]]:
    proposals = sorted(
        (d for d in decisions if d.status == "propose"),
        key=lambda d: (d.merchant.total, d.merchant.count),
        reverse=True,
    )
    keep, overflow = proposals[:limit], proposals[limit:]
    for d in overflow:
        d.status = "review"
        d.reason = f"over the {limit}-per-run cap; {d.reason}"
    return keep, overflow
