"""Guided mode: route common finance questions to deterministic handlers.

Instead of letting a small local model orchestrate tool calls, guided mode
does three bounded things per message:

1. **Route** — one schema-constrained model call classifies the message into
   an intent plus closed-grammar slots (periods are expressions, never dates;
   dates are resolved here, in the user's timezone).
2. **Compute** — a handler calls the finance tools in-process, builds a
   "figures pack" with every number precomputed, renders a markdown table and
   a deterministic `securo-chart` block.
3. **Narrate** — one short model call writes 2-4 sentences about the pack.
   Every number in the narration is checked against the pack
   (`grounding.ungrounded`); an ungrounded draft is regenerated once, then
   replaced by a templated sentence.

The executor wires this in (stage 3): `is_eligible(...)` → `route(...)` →
for `categorize_review` run the categorization workflow, for `freeform` or low
confidence fall through to the tool loop, otherwise `answer(...)`. `answer`
does all fetching before its first yield and raises `GuidedFallthrough` if
anything fails before then, so the UI never sees a half answer.

Import this module lazily from the executor (inside `run()`), as it imports
`ExecutorEvent` from there.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

import mcp_server.tools  # noqa: F401  (registers the tool handlers once per process)
from app.agents.config import AgentSettings
from app.agents.models.agent import Agent
from app.agents.providers.base import ChatMessage, LLMProvider
from app.agents.runtime.executor import ExecutorEvent, _safe_json
from app.agents.services import conversation_service, digest_service, usage_service
from app.agents.services.digest_service import _money
from app.models.user import User
from mcp_server.auth import CallContext
from mcp_server.registry import REGISTRY, coerce_arguments
from mcp_server.tools._helpers import resolve_workspace_id

logger = logging.getLogger(__name__)

INTENTS: tuple[str, ...] = (
    "compare_periods",
    "spending_breakdown",
    "money_map",
    "net_worth_trend",
    "fire_progress",
    "holdings",
    "holding_lookup",
    "categorize_review",
    "freeform",
)

MAX_GUIDED_MESSAGE_CHARS = 600


class GuidedFallthrough(Exception):
    """Raised before the first event when guided mode cannot answer; the
    executor then runs the ordinary tool loop."""


@dataclass
class RouteDecision:
    intent: str
    confidence: float
    period_a: Optional[str] = None
    period_b: Optional[str] = None
    days: Optional[int] = None
    months: Optional[int] = None
    category: Optional[str] = None
    query: Optional[str] = None      # a specific entity: stock/ticker, merchant, account
    analysis: bool = False           # the user wants interpretation, not just the figures
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class GuidedContext:
    session: AsyncSession
    agent: Agent
    user: Optional[User]
    user_id: uuid.UUID
    workspace_id: Optional[uuid.UUID]
    conversation_id: uuid.UUID
    provider: LLMProvider
    model: str
    settings: AgentSettings
    today: date
    tz: str = "UTC"
    language: str = "en"
    currency: str = "USD"


@dataclass
class Prepared:
    pack: dict[str, Any]
    table_md: str
    chart: Optional[dict[str, Any]]
    fallback_sentence: str
    tool_trace: list[tuple[str, dict[str, Any]]]
    narrate: bool = True             # False: the deterministic sentence IS the answer (no model call)


# --- eligibility -------------------------------------------------------------


def is_eligible(*, agent: Agent, settings: AgentSettings, channel: str, user_message: str) -> bool:
    if not getattr(settings, "guided_mode", True):
        return False
    if (getattr(agent, "extra", None) or {}).get("mode", "guided") == "freeform":
        return False
    if channel == "digest":
        return False
    text = (user_message or "").strip()
    if not text or text.startswith("/"):
        return False
    return len(text) <= MAX_GUIDED_MESSAGE_CHARS


# --- dates -------------------------------------------------------------------


def local_today(tz_name: Optional[str], now: Optional[datetime] = None) -> date:
    """Today's date in the user's timezone (invalid or missing tz → UTC)."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


_PERIOD_RE = re.compile(
    r"^(this_month|last_month|this_year|last_year|ytd|month:\d{4}-\d{2}|year:\d{4}"
    r"|last_n_days:\d{1,3}|last_n_months:\d{1,2}|custom:\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2})$"
)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _month_label(d: date) -> str:
    return d.strftime("%b %Y")


def resolve_period(expr: str, today: date) -> tuple[date, date, str]:
    """Resolve a closed-grammar period expression to (from, to, label).

    Windows never extend past `today`; a future month or year raises ValueError.
    """
    expr = (expr or "").strip()
    if not _PERIOD_RE.match(expr):
        raise ValueError(f"unknown period expression: {expr!r}")
    if expr == "this_month":
        return today.replace(day=1), today, _month_label(today)
    if expr == "last_month":
        y, m = _shift_month(today.year, today.month, -1)
        fd, td = _month_bounds(y, m)
        return fd, td, _month_label(fd)
    if expr in ("this_year", "ytd"):
        return date(today.year, 1, 1), today, f"{today.year} YTD"
    if expr == "last_year":
        y = today.year - 1
        return date(y, 1, 1), date(y, 12, 31), str(y)
    if expr.startswith("month:"):
        y, m = (int(x) for x in expr[6:].split("-"))
        fd, td = _month_bounds(y, m)
        if fd > today:
            raise ValueError(f"period is in the future: {expr}")
        return fd, min(td, today), _month_label(fd)
    if expr.startswith("year:"):
        y = int(expr[5:])
        fd, td = date(y, 1, 1), date(y, 12, 31)
        if fd > today:
            raise ValueError(f"period is in the future: {expr}")
        return fd, min(td, today), str(y)
    if expr.startswith("last_n_days:"):
        n = max(7, min(int(expr.split(":")[1]), 730))
        return today - timedelta(days=n - 1), today, f"last {n} days"
    if expr.startswith("last_n_months:"):
        n = max(1, min(int(expr.split(":")[1]), 24))
        y, m = _shift_month(today.year, today.month, -(n - 1))
        return date(y, m, 1), today, f"last {n} months"
    if expr.startswith("custom:"):
        a, b = expr[7:].split("..")
        fd, td = date.fromisoformat(a), date.fromisoformat(b)
        if fd > td:
            raise ValueError("custom period start is after its end")
        td = min(td, today)
        return fd, td, f"{fd.isoformat()} to {td.isoformat()}"
    raise ValueError(f"unhandled period expression: {expr!r}")  # pragma: no cover


def _previous_period(expr: str, today: date) -> str:
    """The natural comparison period for `expr` (the one just before it)."""
    if expr == "this_month":
        return "last_month"
    if expr == "last_month":
        y, m = _shift_month(today.year, today.month, -2)
        return f"month:{y:04d}-{m:02d}"
    if expr in ("this_year", "ytd"):
        return "last_year"
    if expr == "last_year":
        return f"year:{today.year - 2}"
    if expr.startswith("month:"):
        y, m = (int(x) for x in expr[6:].split("-"))
        py, pm = _shift_month(y, m, -1)
        return f"month:{py:04d}-{pm:02d}"
    if expr.startswith("year:"):
        return f"year:{int(expr[5:]) - 1}"
    if expr.startswith("last_n_days:"):
        n = max(7, min(int(expr.split(":")[1]), 730))
        end = today - timedelta(days=n)
        start = end - timedelta(days=n - 1)
        return f"custom:{start.isoformat()}..{end.isoformat()}"
    if expr.startswith("last_n_months:"):
        n = max(1, min(int(expr.split(":")[1]), 24))
        ey, em = _shift_month(today.year, today.month, -n)
        sy, sm = _shift_month(ey, em, -(n - 1))
        return f"custom:{date(sy, sm, 1).isoformat()}..{_month_bounds(ey, em)[1].isoformat()}"
    if expr.startswith("custom:"):
        a, b = expr[7:].split("..")
        fd, td = date.fromisoformat(a), date.fromisoformat(b)
        span = (td - fd).days + 1
        end = fd - timedelta(days=1)
        return f"custom:{(end - timedelta(days=span - 1)).isoformat()}..{end.isoformat()}"
    return "last_month"


# --- router ------------------------------------------------------------------


ROUTER_SCHEMA: dict[str, Any] = {
    "title": "guided_route",
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "period_a": {"type": ["string", "null"]},
        "period_b": {"type": ["string", "null"]},
        "days": {"type": ["integer", "null"]},
        "months": {"type": ["integer", "null"]},
        "category": {"type": ["string", "null"]},
        "query": {"type": ["string", "null"]},
        "analysis": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["intent", "period_a", "period_b", "days", "months", "category", "query", "analysis", "confidence"],
    "additionalProperties": False,
}

_ROUTER_STATIC = """You classify one personal-finance question into an intent and slots. Output JSON only, matching the schema.

Intents:
- compare_periods: compare income/expenses/net/savings between two periods ("this month vs last month", "how did August compare to July").
- spending_breakdown: where money went by category in one period ("what did I spend on in March", "biggest expenses this month").
- money_map: the money-map lanes over a rolling window ("where did my money go in the last 90 days", "savings rate including 401k").
- net_worth_trend: net worth over time ("how is my net worth trending", "net worth last 2 years").
- fire_progress: financial-independence / FIRE progress, FI number, years to FI, when can I retire.
- holdings: the investment accounts and positions as a whole ("what do I hold", "how much is invested", "any patterns in my portfolio").
- holding_lookup: whether/how much of ONE specific stock, fund, crypto or ticker the user owns ("do I own Apple?", "how much NVDA do I have and where?").
- categorize_review: the user wants help categorizing uncategorized transactions or creating categorization rules.
- freeform: anything else — a specific merchant, payee, account or transaction; budgets, goals, recurring bills; follow-ups that depend on an earlier answer ("and the month before?", "why?"); any request to add, change or delete data; anything ambiguous.

Slots:
- period_a, period_b: a period expression from this closed grammar, or null:
  this_month | last_month | this_year | last_year | ytd | month:YYYY-MM | year:YYYY | last_n_days:N | last_n_months:N | custom:YYYY-MM-DD..YYYY-MM-DD
  Do not compute dates yourself; pick the expression. For compare_periods, period_a is the later/asked-about period and period_b the one it is compared with.
- days / months: rolling-window length for money_map (days 7-730 or months 1-24) and net_worth_trend (months 1-60), or null.
- category: a category name the user mentioned, or null.
- query: the specific entity the question is about (a company or fund name, a ticker, a merchant, an account), or null. Required for holding_lookup.
- analysis: true when the user wants interpretation rather than just the figures — patterns, what stands out, risks, concentration, advice, "what do you notice", "should I…". Otherwise false.
- confidence: 0-1. Use 0.9+ only when the question plainly matches one intent and needs no slot you could not fill. Below 0.7 means freeform will be used.

Examples:
"How did this month compare to last month?" -> {"intent":"compare_periods","period_a":"this_month","period_b":"last_month","days":null,"months":null,"category":null,"query":null,"analysis":false,"confidence":0.95}
"Compara {m1_name} com {m2_name}" -> {"intent":"compare_periods","period_a":"month:{m1}","period_b":"month:{m2}","days":null,"months":null,"category":null,"query":null,"analysis":false,"confidence":0.92}
"What did I spend the most on in {m2_name}?" -> {"intent":"spending_breakdown","period_a":"month:{m2}","period_b":null,"days":null,"months":null,"category":null,"query":null,"analysis":false,"confidence":0.93}
"What stands out in my spending this year?" -> {"intent":"spending_breakdown","period_a":"ytd","period_b":null,"days":null,"months":null,"category":null,"query":null,"analysis":true,"confidence":0.9}
"Where did my money go over the last 90 days?" -> {"intent":"money_map","period_a":null,"period_b":null,"days":90,"months":null,"category":null,"query":null,"analysis":false,"confidence":0.9}
"Como está meu patrimônio líquido nos últimos 2 anos?" -> {"intent":"net_worth_trend","period_a":null,"period_b":null,"days":null,"months":24,"category":null,"query":null,"analysis":false,"confidence":0.92}
"When can I retire? / Quanto falta para o FIRE?" -> {"intent":"fire_progress","period_a":null,"period_b":null,"days":null,"months":null,"category":null,"query":null,"analysis":false,"confidence":0.9}
"What do I hold in my brokerage accounts?" -> {"intent":"holdings","period_a":null,"period_b":null,"days":null,"months":null,"category":null,"query":null,"analysis":false,"confidence":0.9}
"Looking at my current holdings, do you see any patterns emerge?" -> {"intent":"holdings","period_a":null,"period_b":null,"days":null,"months":null,"category":null,"query":null,"analysis":true,"confidence":0.92}
"Am I too concentrated?" -> {"intent":"holdings","period_a":null,"period_b":null,"days":null,"months":null,"category":null,"query":null,"analysis":true,"confidence":0.88}
"Do I own Apple stocks?" -> {"intent":"holding_lookup","period_a":null,"period_b":null,"days":null,"months":null,"category":null,"query":"Apple","analysis":false,"confidence":0.95}
"How much NVDA do I have and where?" -> {"intent":"holding_lookup","period_a":null,"period_b":null,"days":null,"months":null,"category":null,"query":"NVDA","analysis":false,"confidence":0.94}
"Help me categorize the uncategorized stuff" -> {"intent":"categorize_review","period_a":null,"period_b":null,"days":null,"months":null,"category":null,"query":null,"analysis":false,"confidence":0.9}
"How much did I pay Uber in June?" -> {"intent":"freeform","period_a":null,"period_b":null,"days":null,"months":null,"category":"","query":"Uber","analysis":false,"confidence":0.3}
"And the month before that?" -> {"intent":"freeform","period_a":null,"period_b":null,"days":null,"months":null,"category":null,"query":null,"analysis":false,"confidence":0.2}
"""


def router_system(*, today: date, tz: str, language: str, prior_user_message: Optional[str]) -> str:
    y1, m1 = _shift_month(today.year, today.month, -1)
    y2, m2 = _shift_month(today.year, today.month, -2)
    static = (
        _ROUTER_STATIC.replace("{m1_name}", date(y1, m1, 1).strftime("%B").lower())
        .replace("{m2_name}", date(y2, m2, 1).strftime("%B"))
        .replace("{m1}", f"{y1:04d}-{m1:02d}")
        .replace("{m2}", f"{y2:04d}-{m2:02d}")
    )
    tail = f"\nToday is {today.isoformat()} in timezone {tz}. The user's language is {language}."
    if prior_user_message:
        prior = prior_user_message.strip().replace("\n", " ")[:200]
        tail += f'\nThe user\'s previous message (context only): "{prior}"'
    return static + tail


_THINK_TAIL_RE = re.compile(r"</think>|</reasoning>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    """Tolerant JSON extraction: drop reasoning prefixes and code fences, then
    fall back to the outermost `{...}` slice."""
    if not text:
        return None
    parts = _THINK_TAIL_RE.split(text)
    body = parts[-1] if parts else text
    body = _FENCE_RE.sub("", body.strip()).strip()
    for candidate in (body, body[body.find("{"): body.rfind("}") + 1] if "{" in body and "}" in body else ""):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _clamp_int(value: Any, lo: int, hi: int) -> Optional[int]:
    try:
        if value is None:
            return None
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return None


def _decision_from(obj: dict[str, Any]) -> RouteDecision:
    intent = obj.get("intent")
    if intent not in INTENTS:
        return RouteDecision(intent="freeform", confidence=0.0, raw=obj)
    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    def period(v: Any) -> Optional[str]:
        return v if isinstance(v, str) and _PERIOD_RE.match(v.strip()) else None

    category = obj.get("category")
    query = obj.get("query")
    analysis_raw = obj.get("analysis")
    if isinstance(analysis_raw, str):
        analysis = analysis_raw.strip().lower() in ("true", "yes", "1")
    else:
        analysis = bool(analysis_raw)
    return RouteDecision(
        intent=intent,
        confidence=confidence,
        period_a=period(obj.get("period_a")),
        period_b=period(obj.get("period_b")),
        days=_clamp_int(obj.get("days"), 7, 730),
        months=_clamp_int(obj.get("months"), 1, 60),
        category=str(category).strip()[:80] if isinstance(category, str) and category.strip() else None,
        query=(str(query).strip()[:80] if isinstance(query, str) and 0 < len(str(query).strip()) <= 80 else None),
        analysis=analysis,
        raw=obj,
    )


async def route(ctx: GuidedContext, *, user_message: str, prior_user_message: Optional[str] = None) -> RouteDecision:
    """Classify the message. Never raises: any failure is a `freeform` decision."""
    system = router_system(today=ctx.today, tz=ctx.tz, language=ctx.language, prior_user_message=prior_user_message)
    started = time.monotonic()
    try:
        resp = await ctx.provider.chat(
            [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user_message)],
            model=ctx.model,
            tools=None,
            temperature=0.0,
            max_tokens=200,
            response_format=ROUTER_SCHEMA,
            reasoning="low",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("guided router call failed; using freeform: %s", exc)
        return RouteDecision(intent="freeform", confidence=0.0)
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        await usage_service.record_usage(
            ctx.session,
            user_id=ctx.user_id,
            agent_id=ctx.agent.id,
            conversation_id=ctx.conversation_id,
            message_id=None,
            provider=ctx.provider.name,
            model=ctx.model,
            kind="router",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            latency_ms=latency_ms,
        )
    except Exception:  # noqa: BLE001
        logger.exception("router usage row failed; continuing")
    obj = _parse_json_object(resp.content or "")
    if obj is None:
        logger.info("guided router returned no JSON; using freeform")
        return RouteDecision(intent="freeform", confidence=0.0)
    decision = _decision_from(obj)
    logger.info("guided route: %s (%.2f)", decision.intent, decision.confidence)
    return decision


# --- in-process tools --------------------------------------------------------


def _call_ctx(ctx: GuidedContext) -> CallContext:
    return CallContext(
        user_id=ctx.user_id,
        workspace_id=ctx.workspace_id,
        conversation_id=ctx.conversation_id,
        agent_id=ctx.agent.id,
    )


async def call_local_tool(session: AsyncSession, ctx: GuidedContext, name: str, **args: Any) -> dict[str, Any]:
    """Run a registered MCP tool handler in-process. A tool-level `{"error": ...}`
    becomes a `GuidedFallthrough` so the free-form loop gets a chance."""
    spec = REGISTRY.get(name)
    if spec is None:
        raise GuidedFallthrough(f"unknown tool {name}")
    result = await spec.handler(session=session, ctx=_call_ctx(ctx), **coerce_arguments(spec, args))
    if isinstance(result, dict) and result.get("error"):
        raise GuidedFallthrough(f"{name}: {result['error']}")
    return result if isinstance(result, dict) else {"value": result}


async def _workspace_id(ctx: GuidedContext) -> uuid.UUID:
    if ctx.workspace_id is not None:
        return ctx.workspace_id
    if getattr(ctx.agent, "workspace_id", None) is not None:
        return ctx.agent.workspace_id
    return await resolve_workspace_id(ctx.session, _call_ctx(ctx))


# --- localisation ------------------------------------------------------------


_L: dict[str, dict[str, str]] = {
    "en": {
        "figure": "Figure", "current": "Current", "previous": "Previous", "change": "Change", "change_pct": "Change %",
        "income": "Income", "expenses": "Expenses", "net": "Net", "invested": "Invested", "savings_rate": "Savings rate",
        "category": "Category", "amount": "Amount", "share": "Share", "total": "Total", "other": "Other",
        "lane": "Lane", "top_per_lane": "Top per lane", "period": "Period",
        "first": "First", "last": "Last", "lowest": "Lowest", "highest": "Highest",
        "input": "Input", "value": "Value", "source": "Source", "given": "given by you",
        "fi_number": "FI number", "gap": "Gap to FI", "progress": "Progress", "years_to_fi": "Years to FI",
        "not_reached": "not reached within the projection",
        "account": "Account", "balance": "Balance", "holdings": "Holdings", "unlinked": "Holdings not linked to an account",
        "position": "Position", "units": "Units", "top_positions": "Top positions", "matches": "Matching positions",
        "accounts_with_positions": "Accounts that expose positions", "accounts_without_positions": "do not expose positions",
        "cap_lookup": "Positions matched by ticker or name across all investment accounts; share = value ÷ total of the investment accounts.",
        "lk_match": "Yes — you hold {tickers} in {n} account(s): {where}; worth {total} ({share}% of your investment accounts).",
        "lk_none": "No position matching '{query}' in any account that exposes holdings ({with_names}); {k} account(s) do not expose positions ({without_names}).",
        "cap_compare": "Income, expenses and net from the Transactions summary; invested = money moved into investments.",
        "cap_spending": "P&L debits by category (sum of categories); transfers and card payments excluded.",
        "cap_money_map": "Money map lanes for {label}; net = income + direct contributions + transfers in − expenses − investments − transfers out.",
        "cap_net_worth": "Net worth at month end, last {n} months.",
        "cap_fire": "FI number = annual spend ÷ withdrawal rate; the projection assumes the same contribution and real return every year.",
        "cap_holdings": "Investment accounts (holdings inside an account are already in its balance).",
        "fb_compare": "Net was {a_net} in {a} versus {b_net} in {b} ({delta} change).",
        "fb_spending": "You spent {total} in {label}; the largest category was {top} ({top_amount}).",
        "fb_money_map": "Over {label}, income was {income} and expenses {expenses}; the net is {net}.",
        "fb_net_worth": "Net worth went from {first} to {last} over the last {n} months ({delta}).",
        "fb_fire": "Your FI number is {fi}; you are at {progress}% of it and, at the current pace, FI is {years}.",
        "fb_holdings": "Your investment accounts total {total} across {n} accounts; the largest is {top_account} ({top_account_share}%) and the largest single position is {top_position} ({top_position_share}%).",
        "years": "{n} years away",
        "cash_rate": "Cash savings rate", "contrib_rate": "Contribution-aware savings rate",
        "lane_income": "Income", "lane_direct_contributions": "Direct contributions", "lane_transfers_in": "Transfers in",
        "lane_expenses": "Expenses", "lane_investments": "Investments", "lane_transfers_out": "Transfers out",
    },
    "pt-BR": {
        "figure": "Indicador", "current": "Atual", "previous": "Anterior", "change": "Variação", "change_pct": "Variação %",
        "income": "Receitas", "expenses": "Despesas", "net": "Saldo", "invested": "Investido", "savings_rate": "Taxa de poupança",
        "category": "Categoria", "amount": "Valor", "share": "Participação", "total": "Total", "other": "Outros",
        "lane": "Fluxo", "top_per_lane": "Maiores por fluxo", "period": "Período",
        "first": "Início", "last": "Fim", "lowest": "Mínimo", "highest": "Máximo",
        "input": "Entrada", "value": "Valor", "source": "Origem", "given": "informado por você",
        "fi_number": "Número FI", "gap": "Falta para FI", "progress": "Progresso", "years_to_fi": "Anos até FI",
        "not_reached": "não alcançado na projeção",
        "account": "Conta", "balance": "Saldo", "holdings": "Posições", "unlinked": "Posições sem conta vinculada",
        "position": "Posição", "units": "Unidades", "top_positions": "Maiores posições", "matches": "Posições encontradas",
        "accounts_with_positions": "Contas que expõem posições", "accounts_without_positions": "não expõem posições",
        "cap_lookup": "Posições encontradas por ticker ou nome em todas as contas de investimento; participação = valor ÷ total das contas de investimento.",
        "lk_match": "Sim — você tem {tickers} em {n} conta(s): {where}; no total {total} ({share}% das suas contas de investimento).",
        "lk_none": "Nenhuma posição correspondente a '{query}' nas contas que expõem posições ({with_names}); {k} conta(s) não expõem posições ({without_names}).",
        "cap_compare": "Receitas, despesas e saldo do resumo de Transações; investido = dinheiro movido para investimentos.",
        "cap_spending": "Débitos do resultado por categoria (soma das categorias); transferências e pagamentos de cartão excluídos.",
        "cap_money_map": "Fluxos do mapa do dinheiro para {label}; saldo = receitas + contribuições diretas + transferências recebidas − despesas − investimentos − transferências enviadas.",
        "cap_net_worth": "Patrimônio líquido no fim de cada mês, últimos {n} meses.",
        "cap_fire": "Número FI = gasto anual ÷ taxa de retirada; a projeção assume a mesma contribuição e o mesmo retorno real todo ano.",
        "cap_holdings": "Contas de investimento (posições dentro de uma conta já estão no saldo dela).",
        "fb_compare": "O saldo foi {a_net} em {a} contra {b_net} em {b} (variação de {delta}).",
        "fb_spending": "Você gastou {total} em {label}; a maior categoria foi {top} ({top_amount}).",
        "fb_money_map": "Em {label}, as receitas foram {income} e as despesas {expenses}; o saldo é {net}.",
        "fb_net_worth": "O patrimônio líquido foi de {first} para {last} nos últimos {n} meses ({delta}).",
        "fb_fire": "Seu número FI é {fi}; você está em {progress}% dele e, no ritmo atual, a FI está {years}.",
        "fb_holdings": "Suas contas de investimento somam {total} em {n} contas; a maior é {top_account} ({top_account_share}%) e a maior posição isolada é {top_position} ({top_position_share}%).",
        "years": "a {n} anos",
        "cash_rate": "Taxa de poupança em caixa", "contrib_rate": "Taxa de poupança com contribuições",
        "lane_income": "Receitas", "lane_direct_contributions": "Contribuições diretas", "lane_transfers_in": "Transferências recebidas",
        "lane_expenses": "Despesas", "lane_investments": "Investimentos", "lane_transfers_out": "Transferências enviadas",
    },
    "es": {
        "figure": "Indicador", "current": "Actual", "previous": "Anterior", "change": "Variación", "change_pct": "Variación %",
        "income": "Ingresos", "expenses": "Gastos", "net": "Neto", "invested": "Invertido", "savings_rate": "Tasa de ahorro",
        "category": "Categoría", "amount": "Importe", "share": "Participación", "total": "Total", "other": "Otros",
        "lane": "Flujo", "top_per_lane": "Mayores por flujo", "period": "Período",
        "first": "Inicio", "last": "Fin", "lowest": "Mínimo", "highest": "Máximo",
        "input": "Entrada", "value": "Valor", "source": "Origen", "given": "indicado por ti",
        "fi_number": "Número FI", "gap": "Falta para FI", "progress": "Progreso", "years_to_fi": "Años hasta FI",
        "not_reached": "no alcanzado en la proyección",
        "account": "Cuenta", "balance": "Saldo", "holdings": "Posiciones", "unlinked": "Posiciones sin cuenta vinculada",
        "position": "Posición", "units": "Unidades", "top_positions": "Mayores posiciones", "matches": "Posiciones encontradas",
        "accounts_with_positions": "Cuentas que exponen posiciones", "accounts_without_positions": "no exponen posiciones",
        "cap_lookup": "Posiciones encontradas por ticker o nombre en todas las cuentas de inversión; participación = valor ÷ total de las cuentas de inversión.",
        "lk_match": "Sí — tienes {tickers} en {n} cuenta(s): {where}; en total {total} ({share}% de tus cuentas de inversión).",
        "lk_none": "Ninguna posición que coincida con '{query}' en las cuentas que exponen posiciones ({with_names}); {k} cuenta(s) no exponen posiciones ({without_names}).",
        "cap_compare": "Ingresos, gastos y neto del resumen de Transacciones; invertido = dinero movido a inversiones.",
        "cap_spending": "Débitos del resultado por categoría (suma de categorías); transferencias y pagos de tarjeta excluidos.",
        "cap_money_map": "Flujos del mapa del dinero para {label}; neto = ingresos + aportes directos + transferencias recibidas − gastos − inversiones − transferencias enviadas.",
        "cap_net_worth": "Patrimonio neto a fin de mes, últimos {n} meses.",
        "cap_fire": "Número FI = gasto anual ÷ tasa de retiro; la proyección asume el mismo aporte y el mismo retorno real cada año.",
        "cap_holdings": "Cuentas de inversión (las posiciones dentro de una cuenta ya están en su saldo).",
        "fb_compare": "El neto fue {a_net} en {a} frente a {b_net} en {b} (variación de {delta}).",
        "fb_spending": "Gastaste {total} en {label}; la mayor categoría fue {top} ({top_amount}).",
        "fb_money_map": "En {label}, los ingresos fueron {income} y los gastos {expenses}; el neto es {net}.",
        "fb_net_worth": "El patrimonio neto pasó de {first} a {last} en los últimos {n} meses ({delta}).",
        "fb_fire": "Tu número FI es {fi}; estás al {progress}% y, al ritmo actual, la FI está {years}.",
        "fb_holdings": "Tus cuentas de inversión suman {total} en {n} cuentas; la mayor es {top_account} ({top_account_share}%) y la mayor posición individual es {top_position} ({top_position_share}%).",
        "years": "a {n} años",
        "cash_rate": "Tasa de ahorro en efectivo", "contrib_rate": "Tasa de ahorro con aportes",
        "lane_income": "Ingresos", "lane_direct_contributions": "Aportes directos", "lane_transfers_in": "Transferencias recibidas",
        "lane_expenses": "Gastos", "lane_investments": "Inversiones", "lane_transfers_out": "Transferencias enviadas",
    },
}


def _lang(language: Optional[str]) -> str:
    base = (language or "en").split("-")[0].lower()
    return {"pt": "pt-BR", "es": "es"}.get(base, "en")


def _t(language: Optional[str], key: str, **fmt: Any) -> str:
    text = _L[_lang(language)].get(key) or _L["en"].get(key) or key
    return text.format(**fmt) if fmt else text


# --- packs, tables, charts -----------------------------------------------------


_CHART_KEYS = frozenset({"type", "title", "subtitle", "currency", "x_label", "y_label", "data", "series"})


def _chart(
    chart_type: str,
    title: str,
    data: list[dict[str, Any]],
    *,
    series: Optional[list[dict[str, str]]] = None,
    currency: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Build a `securo-chart` spec (keys mirror ChartSpec in agent-chart.tsx)."""
    if chart_type not in ("line", "bar", "area", "pie") or len(data) < 2:
        return None
    spec: dict[str, Any] = {"type": chart_type, "title": title, "data": data}
    if currency:
        spec["currency"] = currency
    if series:
        spec["series"] = series
    assert set(spec) <= _CHART_KEYS
    return spec


def _chart_fence(chart: Optional[dict[str, Any]]) -> str:
    return "" if chart is None else "```securo-chart\n" + json.dumps(chart, default=str) + "\n```"


def _r(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _pct_points(fraction: Optional[float]) -> Optional[float]:
    return None if fraction is None else round(float(fraction) * 100.0, 1)


def _delta_pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / abs(b) * 100.0, 1)


def _fmt_pct_value(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.1f}%"


def _signed_money(v: Optional[float], currency: str) -> str:
    if v is None:
        return "n/a"
    return ("+" if v > 0 else "") + _money(v, currency)


def _cell(text: Any) -> str:
    """A markdown table cell: pipes escaped, newlines collapsed (account names may contain `|`)."""
    return str("" if text is None else text).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(_cell(h) for h in headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _periods_for_compare(decision: RouteDecision, today: date) -> tuple[str, str]:
    a = decision.period_a or "this_month"
    b = decision.period_b or _previous_period(a, today)
    return a, b


async def _prepare_compare_periods(ctx: GuidedContext, decision: RouteDecision) -> Prepared:
    a_expr, b_expr = _periods_for_compare(decision, ctx.today)
    a_from, a_to, a_label = resolve_period(a_expr, ctx.today)
    b_from, b_to, b_label = resolve_period(b_expr, ctx.today)
    args_a = {"from_date": a_from.isoformat(), "to_date": a_to.isoformat()}
    args_b = {"from_date": b_from.isoformat(), "to_date": b_to.isoformat()}
    wa, wb = await asyncio.gather(
        call_local_tool(ctx.session, ctx, "get_transactions_summary", **args_a),
        call_local_tool(ctx.session, ctx, "get_transactions_summary", **args_b),
    )
    currency = wa.get("currency") or ctx.currency

    def side(label: str, w: dict[str, Any]) -> dict[str, Any]:
        return {
            "label": label,
            "from": w.get("from_date"),
            "to": w.get("to_date"),
            "income": _r(w.get("income")),
            "expense": _r(w.get("expense")),
            "net": _r(w.get("net")),
            "invested": _r(w.get("invested")),
            "savings_rate_pct": _pct_points(w.get("savings_rate")),
        }

    a, b = side(a_label, wa), side(b_label, wb)
    delta = {k: _r((a[k] or 0.0) - (b[k] or 0.0)) for k in ("income", "expense", "net", "invested")}
    if a["savings_rate_pct"] is not None and b["savings_rate_pct"] is not None:
        delta["savings_rate_pts"] = round(a["savings_rate_pct"] - b["savings_rate_pct"], 1)
    else:
        delta["savings_rate_pts"] = None
    delta_pct = {k: _delta_pct(a[k], b[k]) for k in ("income", "expense", "net", "invested")}
    pack = {"kind": "compare_periods", "currency": currency, "a": a, "b": b, "delta": delta, "delta_pct": delta_pct}

    lang = ctx.language
    rows = []
    for key, label_key in (("income", "income"), ("expense", "expenses"), ("net", "net"), ("invested", "invested")):
        rows.append([
            _t(lang, label_key), _money(a[key], currency), _money(b[key], currency),
            _signed_money(delta[key], currency), _fmt_pct_value(delta_pct[key]),
        ])
    rows.append([
        _t(lang, "savings_rate"), _fmt_pct_value(a["savings_rate_pct"]), _fmt_pct_value(b["savings_rate_pct"]),
        ("n/a" if delta["savings_rate_pts"] is None else f"{delta['savings_rate_pts']:+.1f} pts"), "",
    ])
    table = _md_table([_t(lang, "figure"), a_label, b_label, _t(lang, "change"), _t(lang, "change_pct")], rows)
    table += "\n\n_" + _t(lang, "cap_compare") + "_"
    chart = _chart(
        "bar",
        f"{a_label} vs {b_label}",
        [
            {"x": _t(lang, "income"), "a": a["income"], "b": b["income"]},
            {"x": _t(lang, "expenses"), "a": a["expense"], "b": b["expense"]},
            {"x": _t(lang, "net"), "a": a["net"], "b": b["net"]},
            {"x": _t(lang, "invested"), "a": a["invested"], "b": b["invested"]},
        ],
        series=[{"key": "a", "name": a_label}, {"key": "b", "name": b_label}],
        currency=currency,
    )
    fallback = _t(
        lang, "fb_compare",
        a_net=_money(a["net"], currency), a=a_label, b_net=_money(b["net"], currency), b=b_label,
        delta=_signed_money(delta["net"], currency),
    )
    return Prepared(pack, table, chart, fallback, [("get_transactions_summary", args_a), ("get_transactions_summary", args_b)])


async def _prepare_spending_breakdown(ctx: GuidedContext, decision: RouteDecision) -> Prepared:
    fd, td, label = resolve_period(decision.period_a or "this_month", ctx.today)
    ws_id = await _workspace_id(ctx)
    by_cat = await digest_service.expense_by_category(ctx.session, ws_id, fd, td)
    ordered = sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)
    total = _r(sum(v for _, v in ordered)) or 0.0
    top = ordered[:10]
    rest = ordered[10:]
    items = [{"category": name, "amount": _r(v), "share_pct": (round(v / total * 100, 1) if total else None)} for name, v in top]
    if rest:
        other = sum(v for _, v in rest)
        items.append({"category": _t(ctx.language, "other"), "amount": _r(other), "share_pct": (round(other / total * 100, 1) if total else None)})
    currency = ctx.currency
    pack: dict[str, Any] = {
        "kind": "spending_breakdown",
        "currency": currency,
        "period": {"label": label, "from": fd.isoformat(), "to": td.isoformat()},
        "total": total,
        "items": items,
        "category_count": len(ordered),
    }
    if decision.category:
        wanted = decision.category.strip().lower()
        for name, v in ordered:
            if name.lower() == wanted:
                pack["focus"] = {"category": name, "amount": _r(v), "share_pct": (round(v / total * 100, 1) if total else None)}
                break
    lang = ctx.language
    rows = [[it["category"], _money(it["amount"], currency), _fmt_pct_value(it["share_pct"])] for it in items]
    rows.append([f"**{_t(lang, 'total')}**", f"**{_money(total, currency)}**", "100.0%" if total else "n/a"])
    table = f"**{label}**\n\n" + _md_table([_t(lang, "category"), _t(lang, "amount"), _t(lang, "share")], rows)
    table += "\n\n_" + _t(lang, "cap_spending") + "_"
    chart = _chart("pie", f"{_t(lang, 'expenses')} — {label}", [{"name": it["category"], "value": it["amount"]} for it in items if it["amount"]], currency=currency)
    top_name, top_amount = (top[0][0], _r(top[0][1])) if top else ("—", 0.0)
    fallback = _t(lang, "fb_spending", total=_money(total, currency), label=label, top=top_name, top_amount=_money(top_amount, currency))
    return Prepared(pack, table, chart, fallback, [("get_transactions_summary", {"from_date": fd.isoformat(), "to_date": td.isoformat(), "group_by": "category"})])


async def _prepare_money_map(ctx: GuidedContext, decision: RouteDecision) -> Prepared:
    if decision.months and not decision.days:
        args: dict[str, Any] = {"months": max(1, min(decision.months, 24))}
    else:
        args = {"days": decision.days or 30}
    res = await call_local_tool(ctx.session, ctx, "get_money_map", **args)
    currency = res.get("currency") or ctx.currency
    totals = res.get("totals") or {}
    window = res.get("window") or {}
    lanes_in = res.get("lanes") or {}
    lanes = {
        lane: {
            "total": _r((lanes_in.get(lane) or {}).get("total")),
            "top": [{"label": it.get("label"), "value": _r(it.get("value"))} for it in ((lanes_in.get(lane) or {}).get("items") or [])[:5]],
        }
        for lane in ("income", "direct_contributions", "transfers_in", "expenses", "investments", "transfers_out")
    }
    label = f"{_t(ctx.language, 'period')} {window.get('from_date')} → {window.get('to_date')}"
    pack = {
        "kind": "money_map",
        "currency": currency,
        "window": {"from": window.get("from_date"), "to": window.get("to_date"), **({"days": window["days"]} if "days" in window else {"months": window.get("months")})},
        "totals": {
            **{k: _r(totals.get(k)) for k in ("income", "direct_contributions", "transfers_in", "expenses", "investments", "transfers_out", "net")},
            "cash_savings_rate_pct": _pct_points(totals.get("cash_savings_rate")),
            "contribution_aware_savings_rate_pct": _pct_points(totals.get("contribution_aware_savings_rate")),
        },
        "lanes": lanes,
    }
    lang = ctx.language
    rows = [[_t(lang, f"lane_{lane}"), _money(pack["totals"][lane], currency)] for lane in ("income", "direct_contributions", "transfers_in", "expenses", "investments", "transfers_out")]
    rows.append([f"**{_t(lang, 'net')}**", f"**{_signed_money(pack['totals']['net'], currency)}**"])
    rows.append([_t(lang, "cash_rate"), _fmt_pct_value(pack["totals"]["cash_savings_rate_pct"])])
    rows.append([_t(lang, "contrib_rate"), _fmt_pct_value(pack["totals"]["contribution_aware_savings_rate_pct"])])
    table = f"**{label}**\n\n" + _md_table([_t(lang, "lane"), _t(lang, "total")], rows)
    tops = []
    for lane, data in lanes.items():
        if data["top"]:
            tops.append(f"- {_t(lang, f'lane_{lane}')}: " + "; ".join(f"{it['label']} {_money(it['value'], currency)}" for it in data["top"]))
    if tops:
        table += f"\n\n**{_t(lang, 'top_per_lane')}**\n" + "\n".join(tops)
    table += "\n\n_" + _t(lang, "cap_money_map", label=label) + "_"
    chart = _chart(
        "bar", f"Money map — {window.get('from_date')} → {window.get('to_date')}",
        [{"x": _t(lang, f"lane_{lane}"), "y": pack["totals"][lane]} for lane in ("income", "direct_contributions", "transfers_in", "expenses", "investments", "transfers_out")],
        currency=currency,
    )
    fallback = _t(lang, "fb_money_map", label=label, income=_money(pack["totals"]["income"], currency), expenses=_money(pack["totals"]["expenses"], currency), net=_signed_money(pack["totals"]["net"], currency))
    return Prepared(pack, table, chart, fallback, [("get_money_map", args)])


async def _prepare_net_worth_trend(ctx: GuidedContext, decision: RouteDecision) -> Prepared:
    months = max(1, min(decision.months or 12, 60))
    args = {"months": months, "interval": "monthly"}
    res = await call_local_tool(ctx.session, ctx, "get_net_worth", **args)
    trend = res.get("trend") or []
    points = [{"date": str(p.get("date")), "value": _r(p.get("value"))} for p in trend if p.get("value") is not None]
    if not points:
        raise GuidedFallthrough("net worth report returned no points")
    currency = ((res.get("meta") or {}).get("currency")) or ctx.currency
    first, last = points[0], points[-1]
    lowest = min(points, key=lambda p: p["value"])
    highest = max(points, key=lambda p: p["value"])
    delta = _r(last["value"] - first["value"])
    pack = {
        "kind": "net_worth_trend",
        "currency": currency,
        "months": months,
        "first": first,
        "last": last,
        "delta": delta,
        "delta_pct": _delta_pct(last["value"], first["value"]),
        "min": lowest,
        "max": highest,
        "points": points,
    }
    lang = ctx.language
    rows = [
        [_t(lang, "first"), first["date"], _money(first["value"], currency)],
        [_t(lang, "last"), last["date"], _money(last["value"], currency)],
        [_t(lang, "change"), "", f"{_signed_money(delta, currency)} ({_fmt_pct_value(pack['delta_pct'])})"],
        [_t(lang, "lowest"), lowest["date"], _money(lowest["value"], currency)],
        [_t(lang, "highest"), highest["date"], _money(highest["value"], currency)],
    ]
    table = _md_table([_t(lang, "figure"), _t(lang, "period"), _t(lang, "value")], rows)
    table += "\n\n_" + _t(lang, "cap_net_worth", n=months) + "_"
    chart = _chart("line", f"Net worth — {months}m", [{"x": p["date"], "y": p["value"]} for p in points], currency=currency)
    fallback = _t(lang, "fb_net_worth", first=_money(first["value"], currency), last=_money(last["value"], currency), n=months, delta=_signed_money(delta, currency))
    return Prepared(pack, table, chart, fallback, [("get_net_worth", args)])


async def _prepare_fire_progress(ctx: GuidedContext, decision: RouteDecision) -> Prepared:
    res = await call_local_tool(ctx.session, ctx, "fire_projection")
    currency = res.get("currency") or ctx.currency
    inputs = res.get("inputs") or {}
    derived = res.get("derived") or {}
    lang = ctx.language
    sources = {k: (derived.get(k) or {}).get("source") or _t(lang, "given") for k in ("annual_spend", "invested_assets", "annual_contribution")}
    years = res.get("years_to_fi")
    trajectory = [{"year": int(p.get("year")), "end": _r(p.get("end"))} for p in (res.get("trajectory") or [])][:30]
    pack = {
        "kind": "fire_progress",
        "currency": currency,
        "inputs": {
            "annual_spend": _r(inputs.get("annual_spend")),
            "invested_assets": _r(inputs.get("invested_assets")),
            "annual_contribution": _r(inputs.get("annual_contribution")),
            "real_return_pct": _pct_points(inputs.get("real_return")),
            "withdrawal_rate_pct": _pct_points(inputs.get("withdrawal_rate")),
        },
        "sources": sources,
        "fi_number": _r(res.get("fi_number")),
        "gap": _r(res.get("gap")),
        "progress_pct": _pct_points(res.get("progress")),
        "years_to_fi": (round(float(years), 1) if years is not None else None),
        "trajectory": trajectory,
        "trajectory_truncated": bool(res.get("trajectory_truncated")) or len(res.get("trajectory") or []) > 30,
    }
    rows = [
        [_t(lang, "input") + ": annual spend", _money(pack["inputs"]["annual_spend"], currency), sources["annual_spend"]],
        [_t(lang, "input") + ": invested assets", _money(pack["inputs"]["invested_assets"], currency), sources["invested_assets"]],
        [_t(lang, "input") + ": annual contribution", _money(pack["inputs"]["annual_contribution"], currency), sources["annual_contribution"]],
        [_t(lang, "input") + ": real return", _fmt_pct_value(pack["inputs"]["real_return_pct"]), ""],
        [_t(lang, "input") + ": withdrawal rate", _fmt_pct_value(pack["inputs"]["withdrawal_rate_pct"]), ""],
        [f"**{_t(lang, 'fi_number')}**", f"**{_money(pack['fi_number'], currency)}**", ""],
        [_t(lang, "gap"), _money(pack["gap"], currency), ""],
        [_t(lang, "progress"), _fmt_pct_value(pack["progress_pct"]), ""],
        [_t(lang, "years_to_fi"), (f"{pack['years_to_fi']:.1f}" if pack["years_to_fi"] is not None else _t(lang, "not_reached")), ""],
    ]
    table = _md_table([_t(lang, "figure"), _t(lang, "value"), _t(lang, "source")], rows)
    table += "\n\n_" + _t(lang, "cap_fire") + "_"
    chart = _chart("area", "Projected portfolio (real terms)", [{"x": f"Y{p['year']}", "y": p["end"]} for p in trajectory], currency=currency)
    years_text = _t(lang, "years", n=f"{pack['years_to_fi']:.1f}") if pack["years_to_fi"] is not None else _t(lang, "not_reached")
    fallback = _t(lang, "fb_fire", fi=_money(pack["fi_number"], currency), progress=(f"{pack['progress_pct']:.1f}" if pack["progress_pct"] is not None else "n/a"), years=years_text)
    return Prepared(pack, table, chart, fallback, [("fire_projection", {})])


_RETIREMENT_RE = re.compile(r"401\s*\(?k\)?|401k|403\s*\(?b\)?|\b457\b|\bIRA\b|\bRoth\b|Retirement|Pension|Previd|Aposentad", re.IGNORECASE)
_CRYPTO_RE = re.compile(r"Crypto|Bitcoin|Coinbase|Ethereum", re.IGNORECASE)
_MAX_POSITIONS_IN_PACK = 40
_TOP_POSITIONS_IN_TABLE = 15

# Common names → tickers, for lookups like "do I own apple?" when the position's name is just the ticker.
_TICKER_ALIASES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"\bapple\b", re.I), ("AAPL",)),
    (re.compile(r"\bnvidia\b", re.I), ("NVDA",)),
    (re.compile(r"\bmicrosoft\b", re.I), ("MSFT",)),
    (re.compile(r"\bamazon\b", re.I), ("AMZN",)),
    (re.compile(r"\b(google|alphabet)\b", re.I), ("GOOGL", "GOOG")),
    (re.compile(r"\btesla\b", re.I), ("TSLA",)),
    (re.compile(r"\b(meta|facebook)\b", re.I), ("META",)),
    (re.compile(r"\bbitcoin\b", re.I), ("BTC", "BTC-USD", "BTCUSD")),
    (re.compile(r"\bethereum\b", re.I), ("ETH", "ETH-USD", "ETHUSD")),
    (re.compile(r"s\s*&\s*p\s*500|\bspy\b|\bvoo\b|\bivv\b", re.I), ("SPY", "VOO", "IVV")),
]


def _account_bucket(name: str) -> str:
    if _CRYPTO_RE.search(name or ""):
        return "crypto"
    if _RETIREMENT_RE.search(name or ""):
        return "retirement"
    return "taxable"


def _share(value: Optional[float], total: Optional[float]) -> Optional[float]:
    if value is None or not total:
        return None
    return round(float(value) / float(total) * 100.0, 1)


def _position_label(pos: dict[str, Any]) -> str:
    name, ticker = pos.get("name") or "", pos.get("ticker") or ""
    if ticker and name and ticker.upper() != name.upper():
        return f"{name} ({ticker})"
    return name or ticker or "—"


async def _holdings_pack(ctx: GuidedContext) -> dict[str, Any]:
    """The enriched holdings figures pack shared by `holdings` and `holding_lookup`."""
    res = await call_local_tool(ctx.session, ctx, "get_holdings")
    currency = res.get("currency") or ctx.currency
    total = _r(res.get("investment_accounts_total_primary"))
    accounts: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    split_totals = {"retirement": 0.0, "taxable": 0.0, "crypto": 0.0}
    for a in res.get("accounts") or []:
        balance_primary = _r(a.get("balance_primary"))
        if balance_primary is None:
            balance_primary = _r(a.get("balance"))
        holdings = a.get("holdings") or []
        accounts.append({
            "name": a.get("name"),
            "currency": a.get("currency"),
            "balance": _r(a.get("balance")),
            "balance_primary": balance_primary,
            "share_pct": _share(balance_primary, total),
            "holdings_count": len(holdings),
        })
        split_totals[_account_bucket(a.get("name") or "")] += balance_primary or 0.0
        for h in holdings:
            positions.append({
                "name": h.get("name"),
                "ticker": h.get("ticker"),
                "account": a.get("name"),
                "units": h.get("units"),
                "value": _r(h.get("current_value")),
                "share_pct": _share(h.get("current_value"), total),
                "gain_loss": _r(h.get("gain_loss")),
            })
    unlinked = []
    for h in res.get("unlinked_holdings") or []:
        item = {"name": h.get("name"), "ticker": h.get("ticker"), "currency": h.get("currency"), "current_value": _r(h.get("current_value"))}
        unlinked.append(item)
        positions.append({
            "name": h.get("name"), "ticker": h.get("ticker"), "account": _t(ctx.language, "unlinked"),
            "units": h.get("units"), "value": _r(h.get("current_value")), "share_pct": _share(h.get("current_value"), total),
            "gain_loss": _r(h.get("gain_loss")),
        })
    positions.sort(key=lambda p: (p["value"] or 0.0), reverse=True)
    top5 = sum((p["value"] or 0.0) for p in positions[:5])
    largest_pos = positions[0] if positions else None
    largest_acc = max(accounts, key=lambda a: (a["balance_primary"] or 0.0), default=None)
    split_sum = sum(split_totals.values())
    pack: dict[str, Any] = {
        "kind": "holdings",
        "currency": currency,
        "accounts": accounts,
        "accounts_total_primary": total,
        "accounts_with_positions": sum(1 for a in accounts if a["holdings_count"]),
        "zero_balance_accounts": [a["name"] for a in accounts if not a["balance_primary"]],
        "zero_balance_count": sum(1 for a in accounts if not a["balance_primary"]),
        "positions": positions[:_MAX_POSITIONS_IN_PACK],
        "positions_total_count": len(positions),
        "concentration": {
            "largest_position": (
                {"name": largest_pos["name"], "ticker": largest_pos["ticker"], "account": largest_pos["account"],
                 "value": largest_pos["value"], "share_pct": largest_pos["share_pct"]} if largest_pos else None
            ),
            "top5_share_pct": _share(top5, total) if positions else None,
            "largest_account": ({"name": largest_acc["name"], "share_pct": largest_acc["share_pct"]} if largest_acc else None),
        },
        "split": {
            "retirement_pct": _share(split_totals["retirement"], split_sum) if split_sum else None,
            "taxable_pct": _share(split_totals["taxable"], split_sum) if split_sum else None,
            "crypto_pct": _share(split_totals["crypto"], split_sum) if split_sum else None,
        },
        "unlinked": unlinked,
        "unlinked_total_by_currency": {k: _r(v) for k, v in (res.get("unlinked_holdings_total_by_currency") or {}).items()},
        "unconverted_count": len(res.get("unconverted_accounts") or []),
    }
    return pack


def _positions_table(ctx: GuidedContext, positions: list[dict[str, Any]], currency: str, *, with_units: bool = False) -> str:
    lang = ctx.language
    headers = [_t(lang, "position"), _t(lang, "account")] + ([_t(lang, "units")] if with_units else []) + [_t(lang, "value"), _t(lang, "share")]
    rows = []
    for p in positions:
        row = [_position_label(p), p.get("account") or "—"]
        if with_units:
            row.append(_fmt_units(p.get("units")))
        row += [_money(p.get("value"), currency), _fmt_pct_value(p.get("share_pct"))]
        rows.append(row)
    return _md_table(headers, rows)


def _fmt_units(units: Any) -> str:
    if units is None:
        return ""
    try:
        text = f"{float(units):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(units)
    return text or "0"


async def _prepare_holdings(ctx: GuidedContext, decision: RouteDecision) -> Prepared:
    pack = await _holdings_pack(ctx)
    currency = pack["currency"]
    accounts = pack["accounts"]
    lang = ctx.language
    rows = [
        [a["name"] or "—", a["currency"] or "", _money(a["balance"], a["currency"] or currency), _money(a["balance_primary"], currency),
         _fmt_pct_value(a["share_pct"]), str(a["holdings_count"])]
        for a in accounts
    ]
    rows.append([f"**{_t(lang, 'total')}**", "", "", f"**{_money(pack['accounts_total_primary'], currency)}**", "100.0%" if pack["accounts_total_primary"] else "n/a", ""])
    table = _md_table([_t(lang, "account"), "Ccy", _t(lang, "balance"), f"{_t(lang, 'balance')} ({currency})", _t(lang, "share"), _t(lang, "holdings")], rows)
    if pack["positions"]:
        table += f"\n\n**{_t(lang, 'top_positions')}**\n\n" + _positions_table(ctx, pack["positions"][:_TOP_POSITIONS_IN_TABLE], currency)
    if pack["unlinked"]:
        table += f"\n\n**{_t(lang, 'unlinked')}**\n\n" + _md_table(
            [_t(lang, "holdings"), "Ticker", _t(lang, "value")],
            [[h["name"] or "—", h["ticker"] or "", _money(h["current_value"], h["currency"] or currency)] for h in pack["unlinked"]],
        )
    table += "\n\n_" + _t(lang, "cap_holdings") + "_"
    chart = _chart("pie", f"{_t(lang, 'holdings')} — {currency}", [{"name": a["name"], "value": a["balance_primary"]} for a in accounts if a["balance_primary"]], currency=currency)
    conc = pack["concentration"]
    top_account = conc["largest_account"] or {}
    top_position = conc["largest_position"] or {}
    fallback = _t(
        lang, "fb_holdings",
        total=_money(pack["accounts_total_primary"], currency), n=len(accounts),
        top_account=top_account.get("name") or "—", top_account_share=_fmt_pct_value(top_account.get("share_pct")).rstrip("%"),
        top_position=_position_label(top_position) if top_position else "—",
        top_position_share=_fmt_pct_value(top_position.get("share_pct")).rstrip("%") if top_position else "n/a",
    )
    return Prepared(pack, table, chart, fallback, [("get_holdings", {})])


def _match_positions(query: str, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ticker equality first, then name/ticker containment, then common-name aliases."""
    q = (query or "").strip().lower()
    if not q:
        return []
    q_upper = q.upper()
    alias_tickers: set[str] = set()
    for pattern, tickers in _TICKER_ALIASES:
        if pattern.search(q):
            alias_tickers.update(tickers)
    matched: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(pos: dict[str, Any]) -> None:
        if id(pos) not in seen:
            seen.add(id(pos))
            matched.append(pos)

    for pos in positions:
        ticker = (pos.get("ticker") or "").upper()
        if ticker and ticker == q_upper:
            add(pos)
    for pos in positions:
        name = (pos.get("name") or "").lower()
        ticker = (pos.get("ticker") or "").lower()
        if (name and q in name) or (ticker and q in ticker):
            add(pos)
    for pos in positions:
        ticker = (pos.get("ticker") or "").upper()
        name = (pos.get("name") or "").upper()
        if ticker in alias_tickers or (not ticker and name in alias_tickers):
            add(pos)
    return matched


async def _prepare_holding_lookup(ctx: GuidedContext, decision: RouteDecision) -> Prepared:
    if not decision.query:
        return await _prepare_holdings(ctx, decision)
    base = await _holdings_pack(ctx)
    currency = base["currency"]
    total = base["accounts_total_primary"]
    # match against every position, not just the 40 kept in the summary pack
    all_positions = base["positions"]
    if base["positions_total_count"] > len(all_positions):
        res = await call_local_tool(ctx.session, ctx, "get_holdings")
        all_positions = []
        for a in res.get("accounts") or []:
            for h in a.get("holdings") or []:
                all_positions.append({
                    "name": h.get("name"), "ticker": h.get("ticker"), "account": a.get("name"), "units": h.get("units"),
                    "value": _r(h.get("current_value")), "share_pct": _share(h.get("current_value"), total), "gain_loss": _r(h.get("gain_loss")),
                })
        for h in res.get("unlinked_holdings") or []:
            all_positions.append({
                "name": h.get("name"), "ticker": h.get("ticker"), "account": _t(ctx.language, "unlinked"), "units": h.get("units"),
                "value": _r(h.get("current_value")), "share_pct": _share(h.get("current_value"), total), "gain_loss": _r(h.get("gain_loss")),
            })
    matches = _match_positions(decision.query, all_positions)
    matches.sort(key=lambda p: (p["value"] or 0.0), reverse=True)
    matched_value = _r(sum((m["value"] or 0.0) for m in matches)) if matches else 0.0
    with_names = [a["name"] for a in base["accounts"] if a["holdings_count"]]
    without_names = [a["name"] for a in base["accounts"] if not a["holdings_count"]]
    pack = {
        "kind": "holding_lookup",
        "currency": currency,
        "query": decision.query,
        "matches": matches,
        "matches_total_value": matched_value,
        "matches_share_pct": _share(matched_value, total) if matches else None,
        "accounts_total_primary": total,
        "accounts_with_positions": with_names,
        "accounts_without_positions": without_names,
    }
    lang = ctx.language
    if matches:
        tickers = ", ".join(dict.fromkeys((m.get("ticker") or m.get("name") or "—") for m in matches))
        by_account: dict[str, float] = {}
        for m in matches:
            by_account[m["account"] or "—"] = by_account.get(m["account"] or "—", 0.0) + (m["value"] or 0.0)
        where = "; ".join(f"{acct} {_money(v, currency)}" for acct, v in by_account.items())
        sentence = _t(lang, "lk_match", tickers=tickers, n=len(by_account), where=where, total=_money(matched_value, currency),
                      share=_fmt_pct_value(pack["matches_share_pct"]).rstrip("%"))
        table = f"**{sentence}**\n\n" + _positions_table(ctx, matches, currency, with_units=True)
        table += "\n\n_" + _t(lang, "cap_lookup") + "_"
        return Prepared(pack, table, None, sentence, [("get_holdings", {})])
    sentence = _t(lang, "lk_none", query=decision.query, with_names=", ".join(with_names) or "—", k=len(without_names), without_names=", ".join(without_names) or "—")
    table = _md_table([_t(lang, "accounts_with_positions"), _t(lang, "holdings")], [[name, str(next((a["holdings_count"] for a in base["accounts"] if a["name"] == name), 0))] for name in with_names])
    return Prepared(pack, table, None, sentence, [("get_holdings", {})], narrate=False)

HANDLERS: dict[str, Callable[[GuidedContext, RouteDecision], Awaitable[Prepared]]] = {
    "compare_periods": _prepare_compare_periods,
    "spending_breakdown": _prepare_spending_breakdown,
    "money_map": _prepare_money_map,
    "net_worth_trend": _prepare_net_worth_trend,
    "fire_progress": _prepare_fire_progress,
    "holdings": _prepare_holdings,
    "holding_lookup": _prepare_holding_lookup,
}


# --- narration -----------------------------------------------------------------


NARRATION_SYSTEM = (
    "You are {agent_name}, inside Securo. Write a short narration in {language} (at most 120 words, 2-4 plain "
    "sentences) about the figures below, which Securo computed. Write natural prose for the user, the way an "
    "analyst would sum up a table: compare, point out the biggest change, and stop. Rules: use ONLY these figures; "
    "copy each number exactly as written (same digits, decimals and sign); never add, subtract, average, annualize "
    "or compute a percentage or any new number; do not mention a figure that is not listed; never repeat the figure "
    "labels or field names verbatim (say 'income rose to 1,200.00 USD in Aug 2026 from 900.00 USD in Jul 2026', "
    "not 'Income (Aug 2026): 1,200.00 USD'); no headings, bullets, tables, code fences or charts; name each period "
    "once. Answer the user's question directly in the first sentence. If a figure is n/a, say it is unavailable."
    "\n\nFigures:\n{figures}"
)

ANALYSIS_SYSTEM = (
    "You are {agent_name}, inside Securo. The user asked for interpretation, not just numbers. Write in {language}, at most "
    "220 words, as one or two short paragraphs or up to five bullets. Answer the user's question directly first, then interpret "
    "the figures below (which Securo computed): concentrations, imbalances, diversification, trends, what looks healthy, what "
    "deserves a second look next, framed for someone aiming to retire early. You may make qualitative observations without "
    "numbers. Rules: every number you write must be one of the listed figures, copied exactly (same digits, decimals and sign); "
    "never add, subtract, average, annualize or compute a percentage or any new number; do not mention a figure that is not "
    "listed; never repeat the figure labels verbatim; no headings, tables, code fences or charts; no generic disclaimers."
    "\n\nFigures:\n{figures}"
)

_COUNT_KEYS = frozenset({
    "count", "months", "days", "category_count", "holdings_count", "year", "unconverted_count",
    "years_to_fi", "trajectory_truncated", "positions_total_count", "accounts_with_positions", "zero_balance_count", "units",
})
_MAX_LIST_LINES = 12


def _fmt_leaf(key: str, value: Any, currency: str) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if key.endswith("_pct") or key.endswith("_pts"):
            return f"{value:.1f}%" if key.endswith("_pct") else f"{value:+.1f} pts"
        if key in _COUNT_KEYS:
            return _fmt_units(value) if key == "units" else f"{value}"
        return _money(float(value), currency)
    return None


_LABEL_WORDS = {
    "savings_rate_pct": "Savings rate", "savings_rate_pts": "Savings rate (pts)", "cash_savings_rate": "Cash savings rate",
    "contribution_aware_savings_rate": "Savings rate incl. contributions", "progress_pct": "Progress to FI",
    "fi_number": "FI number", "years_to_fi": "Years to FI", "balance_primary": "Balance (primary currency)", "accounts_total_primary": "Total across accounts",
    "share_pct": "Share", "matches_total_value": "Matched value", "matches_share_pct": "Matched share",
    "positions_total_count": "Positions", "accounts_with_positions": "Accounts with positions", "zero_balance_count": "Zero-balance accounts",
    "top5_share_pct": "Top 5 positions share", "retirement_pct": "Retirement share", "taxable_pct": "Taxable share", "crypto_pct": "Crypto share",
    "gain_loss": "Gain/loss", "units": "Units",
}
# containers whose children are figures ABOUT the parent's name: "income" under "delta" -> "Income change"
_SUFFIX_CONTAINERS = {"delta": "change", "delta_pct": "change %", "deltas": "change"}
# containers that only scope their children (period a/b, current/previous, ...) and add no words
_SCOPE_CONTAINERS = {"a", "b", "current", "previous", "period", "window", "inputs", "totals", "lanes", "sources",
                     "items", "top", "trajectory", "points", "accounts", "unlinked", "holdings", "first", "last", "min", "max",
                     "positions", "matches", "concentration", "split"}


def _humanize_key(key: str) -> str:
    if key in _LABEL_WORDS:
        return _LABEL_WORDS[key]
    words = key.replace("_pct", " %").replace("_pts", " (pts)").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else key


def _render_pack_lines(pack: dict[str, Any]) -> str:
    """One bullet per numeric leaf with a human label ("Income (Aug 2026): 1,200.00 USD",
    "Income change %: 32.5%"), so the narrator sees every figure exactly as it may
    quote it and has nothing machine-looking to parrot back."""
    currency = str(pack.get("currency") or "")
    lines: list[str] = []

    def emit(head: str, scope: str, text: str) -> None:
        head = head[:1].upper() + head[1:]
        lines.append(f"- {head} ({scope}): {text}" if scope else f"- {head}: {text}")

    def walk(node: Any, prefix: str, suffix: str, scope: str, key: str, pct: bool) -> None:
        if isinstance(node, dict):
            label = node.get("label") or node.get("category") or node.get("name") or node.get("date")
            if isinstance(label, str) and label and isinstance(node.get("account"), str) and node.get("account") and node.get("account") != label:
                label = f"{label}, {node['account']}"
            own_scope = str(label) if isinstance(label, str) and label else scope
            for k, v in node.items():
                if k in ("kind", "currency", "label"):
                    continue
                if isinstance(v, (dict, list)):
                    if k in _SUFFIX_CONTAINERS:
                        walk(v, prefix, _SUFFIX_CONTAINERS[k], own_scope, k, pct or k.endswith("_pct"))
                    elif k in _SCOPE_CONTAINERS:
                        walk(v, prefix, suffix, own_scope, k, pct)
                    else:
                        walk(v, f"{prefix} {_humanize_key(k).lower()}".strip() if prefix else _humanize_key(k), suffix, own_scope, k, pct)
                else:
                    walk(v, prefix, suffix, own_scope, k, pct or k.endswith("_pct"))
            return
        if isinstance(node, list):
            for item in node[:_MAX_LIST_LINES]:
                walk(item, prefix, suffix, scope, key, pct)
            if len(node) > _MAX_LIST_LINES:
                lines.append(f"- {_humanize_key(key) if key else 'Items'}: {len(node)} entries (first {_MAX_LIST_LINES} listed)")
            return
        if key in ("from", "to", "date"):
            if isinstance(node, str):
                emit({"from": "From", "to": "To", "date": "Date"}[key], scope, node)
            return
        if pct and isinstance(node, (int, float)) and not isinstance(node, bool):
            text: Optional[str] = f"{float(node):.1f}%"
        else:
            text = _fmt_leaf(key, node, currency)
        if text is None:
            return
        name = _humanize_key(key)
        if suffix:
            head = f"{name} {suffix}" if key not in _LABEL_WORDS or not name.lower().endswith(suffix.split()[0]) else name
        else:
            head = f"{prefix} {name.lower()}".strip() if prefix else name
        emit(head, scope, text)

    walk(pack, "", "", "", "", False)
    return "\n".join(lines)


def _strip_think(text: str) -> str:
    parts = _THINK_TAIL_RE.split(text or "")
    return (parts[-1] if parts else text or "").strip()


async def _narrate(ctx: GuidedContext, prep: Prepared, *, user_message: str, analysis: bool = False) -> tuple[str, int, int, bool]:
    """Returns (text, input_tokens, output_tokens, used_fallback). `analysis` switches to the
    longer, interpretive prompt at reasoning medium; grounding applies either way."""
    from app.agents.runtime.grounding import ungrounded

    template = ANALYSIS_SYSTEM if analysis else NARRATION_SYSTEM
    system = template.format(agent_name=ctx.agent.name, language=ctx.language, figures=_render_pack_lines(prep.pack))
    usage_in = usage_out = 0
    offenders: list[str] = []
    last_text = ""
    for attempt in range(2):
        sys_text = system
        if offenders:
            sys_text += (
                "\n\nYour previous draft used numbers that are not in the figures: "
                + ", ".join(offenders)
                + ". Rewrite without them."
            )
        try:
            resp = await ctx.provider.chat(
                [ChatMessage(role="system", content=sys_text), ChatMessage(role="user", content=user_message)],
                model=ctx.model,
                tools=None,
                temperature=0.2,
                # Ollama counts thinking tokens against num_predict: leave room for the reasoning
                max_tokens=1600 if analysis else 500,
                reasoning="medium" if analysis else "low",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("guided narration call failed (attempt %d): %s", attempt + 1, exc)
            break
        usage_in += resp.usage.input_tokens
        usage_out += resp.usage.output_tokens
        text = _strip_think(resp.content)
        if not text:
            offenders = offenders or ["(empty)"]
            continue
        offenders = ungrounded(text, prep.pack)
        if not offenders:
            return text, usage_in, usage_out, False
        logger.info("guided.narration.rejected intent=%s offenders=%s", prep.pack.get("kind"), offenders)
        last_text = text
    # Both drafts carried a number that is not in the figures. Keep whatever sentences
    # are clean instead of throwing the whole analysis away; fall back to the template
    # only when nothing usable is left.
    redacted = _redact_offending_sentences(last_text, offenders) if offenders and last_text else ""
    if redacted and not ungrounded(redacted, prep.pack):
        logger.info("guided.narration.redacted intent=%s dropped=%s", prep.pack.get("kind"), offenders)
        return redacted, usage_in, usage_out, False
    return prep.fallback_sentence, usage_in, usage_out, True


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u00c0-\u00dd*\-•\d])|\n+")


def _redact_offending_sentences(text: str, offenders: list[str]) -> str:
    """Drop every sentence or bullet that contains one of the offending number
    tokens; return the remaining text (empty when too little is left)."""
    if not text or not offenders:
        return text or ""
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p is not None]
    kept = [p for p in parts if p.strip() and not any(o in p for o in offenders)]
    out = " ".join(s.strip() for s in kept).strip()
    # collapse a bullet list that lost its members into plain prose spacing
    out = re.sub(r"\s{2,}", " ", out)
    return out if len(out) >= 40 else ""


# --- answer --------------------------------------------------------------------


async def answer(ctx: GuidedContext, decision: RouteDecision, *, user_message: str) -> AsyncIterator[ExecutorEvent]:
    """Compute, persist and stream a guided answer.

    Everything that can fail runs before the first `yield`; failures raise
    `GuidedFallthrough` so the executor can run the ordinary tool loop instead.
    """
    handler = HANDLERS.get(decision.intent)
    if handler is None:
        raise GuidedFallthrough(f"no guided handler for {decision.intent}")
    try:
        prep = await handler(ctx, decision)
    except GuidedFallthrough:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GuidedFallthrough(f"{decision.intent}: {exc}") from exc

    tool_calls = [
        {"id": f"guided_{uuid.uuid4().hex[:8]}", "name": f"securo__{name}", "arguments": args}
        for name, args in prep.tool_trace
    ]
    await conversation_service.append_message(
        ctx.session, conversation_id=ctx.conversation_id, role="assistant", content=None, tool_calls=tool_calls
    )
    pack_json = _safe_json(prep.pack)
    for tc in tool_calls:
        await conversation_service.append_message(
            ctx.session,
            conversation_id=ctx.conversation_id,
            role="tool",
            content=pack_json,
            tool_result={"tool_call_id": tc["id"], "name": tc["name"], "data": prep.pack, "ok": True},
        )

    # From here on nothing is allowed to raise out of the generator.
    for tc in tool_calls:
        yield ExecutorEvent(type="tool_call", tool_name=tc["name"], tool_args=tc["arguments"])
    for tc in tool_calls:
        yield ExecutorEvent(type="tool_result", tool_name=tc["name"], tool_result={"ok": True, "data": prep.pack, "text": "figures pack"})

    head = prep.table_md
    fence = _chart_fence(prep.chart)
    if fence:
        head += "\n\n" + fence
    yield ExecutorEvent(type="text_delta", text=head)

    started = time.monotonic()
    try:
        if prep.narrate:
            narration, usage_in, usage_out, used_fallback = await _narrate(ctx, prep, user_message=user_message, analysis=decision.analysis)
        else:
            # the deterministic sentence is the whole answer (e.g. a lookup that found nothing)
            narration, usage_in, usage_out, used_fallback = prep.fallback_sentence, 0, 0, False
    except Exception:  # noqa: BLE001
        logger.exception("guided narration crashed; using the fallback sentence")
        narration, usage_in, usage_out, used_fallback = prep.fallback_sentence, 0, 0, True
    latency_ms = int((time.monotonic() - started) * 1000)
    yield ExecutorEvent(type="text_delta", text="\n\n" + narration)

    try:
        final = await conversation_service.append_message(
            ctx.session,
            conversation_id=ctx.conversation_id,
            role="assistant",
            content=head + "\n\n" + narration,
            input_tokens=usage_in or None,
            output_tokens=usage_out or None,
        )
        if usage_in or usage_out:
            await usage_service.record_usage(
                ctx.session,
                user_id=ctx.user_id,
                agent_id=ctx.agent.id,
                conversation_id=ctx.conversation_id,
                message_id=final.id,
                provider=ctx.provider.name,
                model=ctx.model,
                kind="guided",
                input_tokens=usage_in,
                output_tokens=usage_out,
                latency_ms=latency_ms,
            )
    except Exception:  # noqa: BLE001
        logger.exception("guided answer persistence failed; the stream already delivered it")
    if used_fallback:
        logger.info("guided answer for %s used the templated fallback sentence", decision.intent)
