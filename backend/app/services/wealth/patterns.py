"""Name patterns for accounts and categories, shared by the wealth figures and the advisor."""
from __future__ import annotations

import re

RETIREMENT_RE = re.compile(r"401\s*\(?k\)?|401k|403\s*\(?b\)?|\b457\b|\bIRA\b|\bRoth\b|Retirement|Pension|Previd|Aposentad", re.IGNORECASE)
CRYPTO_RE = re.compile(r"crypto|bitcoin|coinbase|ethereum", re.IGNORECASE)
HSA_RE = re.compile(r"\bHSA\b|health savings", re.IGNORECASE)

INTEREST_RE = re.compile(r"interest charge|finance charge|purchase interest", re.IGNORECASE)
ESSENTIAL_RE = re.compile(
    r"\b(rent|mortgages?|housing|utilit(?:y|ies)|insurance|tax(?:es)?|health(?:care)?|medical|childcare|loans?|debts?)\b",
    re.IGNORECASE,
)
NON_CATEGORY_RE = re.compile(r"^\s*(uncategori[sz]ed|other\b.*)$", re.IGNORECASE)
