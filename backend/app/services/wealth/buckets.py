"""Net-worth buckets per account (plan P3), and the user's overrides.

Defaults read the account type and name, in this order: an HSA by name, card
debt, other debt (loans, or any negative balance), crypto, retirement (Roth or
pre-tax by name), taxable investment, then cash. The user's override wins; it is
found by account id first, then by a fallback key (institution + mask + name) so
it survives a reconnect that creates new account ids.

Liquid = cash + taxable + crypto + card debt (card debt is negative). Loans and
mortgages are not subtracted: they are paid over years, and subtracting them
would make the cash cushion meaningless.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from app.schemas.wealth import AccountAnnotation, Bucket, SettingSource
from app.services.wealth.patterns import CRYPTO_RE, HSA_RE, RETIREMENT_RE

ROTH_RE = re.compile(r"\bRoth\b", re.IGNORECASE)
_NAME_NOISE_RE = re.compile(r"[^a-z0-9]+")

INVESTMENT_BUCKETS: frozenset[Bucket] = frozenset(
    {"taxable", "retirement_pretax", "retirement_roth", "retirement_hsa", "crypto"}
)
LIQUID_BUCKETS: frozenset[Bucket] = frozenset({"cash", "taxable", "crypto", "card_debt"})
DEBT_BUCKETS: frozenset[Bucket] = frozenset({"card_debt", "other_debt"})
_LOAN_TYPES = frozenset({"loan", "mortgage"})


@dataclass(frozen=True)
class AccountFacts:
    """What bucketing needs to know about an account."""

    id: str
    name: str
    type: str
    balance: float
    institution: Optional[str] = None
    masked_number: Optional[str] = None


@dataclass(frozen=True)
class BucketChoice:
    bucket: Bucket
    default_bucket: Bucket
    liquid: bool
    apr: Optional[float]
    source: SettingSource
    override_key: str  # the `accounts` key this choice reads from, or would be written to


def default_bucket(account: AccountFacts) -> Bucket:
    name = account.name or ""
    if HSA_RE.search(name):
        return "retirement_hsa"
    if account.type == "credit_card":
        return "card_debt"
    if account.type in _LOAN_TYPES or account.balance < 0:
        return "other_debt"
    if CRYPTO_RE.search(name):
        return "crypto"
    if RETIREMENT_RE.search(name):
        return "retirement_roth" if ROTH_RE.search(name) else "retirement_pretax"
    if account.type == "investment":
        return "taxable"
    return "cash"


def asset_bucket(asset_type: str) -> Bucket:
    """Manual assets not held in an account: an unlinked investment is taxable,
    everything else (real estate, vehicles, valuables) is property."""
    return "taxable" if asset_type == "investment" else "property_other"


def fallback_key(account: AccountFacts) -> str:
    name = _NAME_NOISE_RE.sub(" ", (account.name or "").lower()).strip()
    return f"{(account.institution or '').strip().lower()}|{account.masked_number or ''}|{name}"


def choose_bucket(account: AccountFacts, overrides: Mapping[str, AccountAnnotation]) -> BucketChoice:
    """The effective bucket for one account, and where it came from."""
    default = default_bucket(account)
    key = account.id
    annotation = overrides.get(account.id)
    if annotation is None:
        fb = fallback_key(account)
        for candidate_key, candidate in overrides.items():
            if candidate.fallback_key and candidate.fallback_key == fb:
                key, annotation = candidate_key, candidate
                break
    bucket: Bucket = annotation.bucket if annotation and annotation.bucket else default
    liquid = annotation.liquid if annotation and annotation.liquid is not None else bucket in LIQUID_BUCKETS
    user_set = annotation is not None and any(
        v is not None for v in (annotation.bucket, annotation.liquid, annotation.apr)
    )
    return BucketChoice(
        bucket=bucket,
        default_bucket=default,
        liquid=liquid,
        apr=annotation.apr if annotation else None,
        source="user" if user_set else "default",
        override_key=key,
    )
