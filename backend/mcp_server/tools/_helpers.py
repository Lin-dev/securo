"""Shared helpers for serializing model rows into LLM-friendly dicts.

Keep payloads small and stable: a transaction returned to the LLM should
have a small set of obviously-named fields, not the full SQLAlchemy row.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional


def parse_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    return date.fromisoformat(str(v))


def parse_uuid(v: Any) -> Optional[uuid.UUID]:
    if v is None or v == "":
        return None
    if isinstance(v, uuid.UUID):
        return v
    return uuid.UUID(str(v))


def parse_uuid_list(v: Any) -> Optional[list[uuid.UUID]]:
    if v is None:
        return None
    values = v if isinstance(v, (list, tuple)) else [v]
    return [
        u
        for x in values
        if (u := parse_uuid(x)) is not None
    ] or None


def num(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return float(x)
    return float(x)


async def resolve_workspace_id(session, ctx) -> uuid.UUID:
    """Return the workspace the call operates in.

    Prefer the explicit `ws_id` claim from the JWT. Fall back to the
    caller's default (first) workspace — supports tokens minted before
    the workspace migration AND keeps single-workspace callers free of
    having to specify a workspace.
    """
    if ctx.workspace_id is not None:
        return ctx.workspace_id
    from app.services.workspace_service import get_default_workspace

    ws = await get_default_workspace(session, ctx.user_id)
    if ws is None:
        raise ValueError("No workspace available for this user")
    return ws.id


# --- merchant grouping (fork addition, qc7) ---------------------------------

# Card references, dates and ids: "#10234", "*2K3JD", "1234", "12/03", "07-11".
_MERCHANT_NOISE = re.compile(r"[#*]|\b\d[\d/.\-:]*\b")
_SPACES = re.compile(r"\s+")
# Where the stable part of a provider description ends: a `#`/`*` marker or the
# first whitespace-separated token that carries a digit before any marker
# ("US*2K3JD" is cut at its `*`, not dropped whole).
_PATTERN_CUT = re.compile(r"[#*]|\s+[^\s#*]*\d")


def merchant_key(description: str) -> str:
    """Grouping key for "the same merchant" across noisy descriptions.

    Same normalization the rule engine matches with (upper case, accents
    stripped), then card references, dates and ids dropped and spaces
    collapsed. Not guaranteed to be a substring of the description — use
    `suggest_pattern` for a value that a `contains` rule can carry.
    """
    from app.services.rule_engine import _normalize

    s = _normalize(description or "")
    s = _MERCHANT_NOISE.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    return s[:60] or "(blank)"


def suggest_pattern(description: str) -> str:
    """A `contains` value that matches this description under rule matching.

    The leading run of the normalized description before the first `*`, `#`
    or digit-bearing token — the part providers keep stable ("UBER *TRIP
    1234" → "UBER", "AMZN MKTP US*2K3JD" → "AMZN MKTP US"). It is a prefix of
    the normalized description, and rules normalize both sides the same way,
    so a rule built on it matches the row it came from. Falls back to the
    first token when the stable part is too short to be distinctive.
    """
    from app.services.rule_engine import _normalize

    s = _SPACES.sub(" ", _normalize(description or "")).strip()
    if not s:
        return "(blank)"
    head = _PATTERN_CUT.split(s, maxsplit=1)[0].strip(" -_,.:")
    if len(head) >= 3:
        return head[:60]
    first = s.split(" ", 1)[0].strip(" -_,.:")
    return (first or s)[:60]
