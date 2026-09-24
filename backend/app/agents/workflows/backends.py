"""Decision backends: who answers the closed "which category?" question.

The workflow owns orchestration and validation whichever backend answers.
`OllamaStructuredBackend` (default) asks the agent's own model one grammar-
constrained question per batch. `KevBackend` asks a local Kev "System One"
server (small decision models with calibrated probabilities, trainable on the
household's own history) one request per merchant, and falls back to the
model backend when it is unreachable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.agents.config import AgentSettings
from app.agents.services import usage_service
from app.agents.workflows.base import WorkflowContext
from app.agents.workflows.classifiers import CategoryFacts, Merchant

logger = logging.getLogger(__name__)

UNKNOWN = "Unknown"


class BackendUnavailable(Exception):
    """The configured decision backend cannot be reached; callers fall back."""


# --- the closed question ------------------------------------------------------------------------


class ClassifiedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: str
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class ClassificationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ClassifiedItem]


@dataclass
class Classified:
    merchant_id: str
    category_name: str
    confidence: float
    reason: str = ""
    probabilities: Optional[dict[str, float]] = None


def batch_schema(ids: list[str], category_names: list[str]) -> dict[str, Any]:
    """Strict JSON schema with both enums patched in; the object shape mirrors
    `ClassificationBatch`, which validates the parsed reply."""
    return {
        "title": "classification_batch",
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": list(ids)},
                        "category": {"type": "string", "enum": list(category_names) + [UNKNOWN]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "category", "confidence", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


CLASSIFY_SYSTEM = """\
You classify bank-transaction merchants into the user's existing categories for Securo, a personal-finance app.

Categories (use the exact name; nothing else is allowed):
{categories}
- {unknown} — use this whenever you are not reasonably sure.

Household conventions (authoritative; they override your general knowledge):
{conventions}

Rules:
1. Pick exactly one category per merchant from the list above, or {unknown}.
2. A merchant that only ever shows credits (money in) can only be an income category or a transfer-type category. A merchant that only shows debits (money out) is never income unless it is a refund category.
3. Payments to the user's own credit card, transfers between the user's own accounts, and contributions to investment or retirement accounts are transfer-type categories, not income or expenses.
4. confidence is your probability that the category is right: 0.95 only for unmistakable merchants (airlines, supermarkets, pharmacies, streaming brands), 0.7-0.85 when the name is suggestive but ambiguous, below 0.6 when you are guessing. When in doubt prefer {unknown} over a wrong category.
5. reason: at most 120 characters, name the clue you used (brand, keyword, convention).
6. Reply with JSON only, matching the schema; no prose, no markdown."""


def category_meaning(cat: CategoryFacts) -> str:
    """One line per category, shared by the prompt and Kev's choice criteria."""
    group = f"group: {cat.group_name}" if cat.group_name else "group: none"
    if cat.treat_as_transfer:
        return f"{group}; transfer-type (money moved between the user's own accounts or into investments; never income or expense)"
    return group


def render_categories(cats: list[CategoryFacts]) -> str:
    return "\n".join(f"- {cat.name} — {category_meaning(cat)}" for cat in cats)


def render_merchant_facts(m: Merchant, currency: str) -> str:
    accounts = ", ".join(m.accounts) or "unknown account"
    samples = "; ".join(f'"{d}"' for d in m.sample_descriptions[:2]) or f'"{m.merchant}"'
    return (
        f"pattern: {m.pattern} | debits: {m.debits}, credits: {m.credits} "
        f"| avg: {m.average:.2f} {currency}, total: {m.total:.2f} {currency} | accounts: {accounts}\n"
        f"samples: {samples}"
    )


def render_batch(batch: list[tuple[str, Merchant]], currency: str) -> str:
    lines = [f"Classify these {len(batch)} merchants. Amounts are in {currency}.", ""]
    for mid, m in batch:
        accounts = ", ".join(m.accounts) or "unknown account"
        lines.append(
            f"{mid} | pattern: {m.pattern} | debits: {m.debits}, credits: {m.credits} "
            f"| avg: {m.average:.2f}, total: {m.total:.2f} | accounts: {accounts}"
        )
        samples = "; ".join(f'"{d}"' for d in m.sample_descriptions[:2]) or f'"{m.merchant}"'
        lines.append(f"     samples: {samples}")
    lines.append("")
    lines.append(f"Return one item per id ({batch[0][0]}..{batch[-1][0]}).")
    return "\n".join(lines)


# --- backends ---------------------------------------------------------------------------------------


class DecisionBackend(Protocol):
    name: str

    async def classify(
        self,
        ctx: WorkflowContext,
        batch: list[tuple[str, Merchant]],
        categories: list[CategoryFacts],
        conventions: str,
        *,
        currency: str,
    ) -> list[Classified]: ...


class OllamaStructuredBackend:
    """Default classifier: one schema-constrained model call per batch."""

    name = "ollama"

    async def classify(
        self,
        ctx: WorkflowContext,
        batch: list[tuple[str, Merchant]],
        categories: list[CategoryFacts],
        conventions: str,
        *,
        currency: str,
    ) -> list[Classified]:
        ids = [mid for mid, _ in batch]
        names = [c.name for c in categories]
        system = CLASSIFY_SYSTEM.format(
            categories=render_categories(categories),
            conventions=conventions.strip() or "none provided",
            unknown=UNKNOWN,
        )
        result = await ctx.llm_structured(
            system,
            render_batch(batch, currency),
            ClassificationBatch,
            reasoning="low",
            temperature=0.0,
            max_tokens=2500,
            retries=1,
            json_schema=batch_schema(ids, names),
        )
        return [
            Classified(merchant_id=item.id, category_name=item.category, confidence=float(item.confidence), reason=item.reason[:120])
            for item in result.items
        ]


KEV_CATEGORY_INSTRUCTIONS = (
    "Which of the user's categories does this bank-transaction merchant belong to? "
    "Transfer-type categories are for money moving between the user's own accounts or into "
    "investments. A merchant that only shows credits cannot be an expense; one that only shows "
    "debits cannot be income. Pick Unknown when no listed category fits."
)
KEV_TRANSFER_INSTRUCTIONS = "Is this money moving between the user's own accounts (a transfer, not income or spending)?"


class KevBackend:
    """One `POST /v1/systemone` per merchant against a local Kev server.

    `state` carries the merchant facts and the household conventions; the
    `category` question is a `choice` over every visible category plus Unknown,
    and `own_account_transfer` a yes/no. Confidence is the top probability, so
    the workflow's thresholds keep their meaning; the full distribution is kept
    for the "needs your call" alternatives.
    """

    name = "kev"

    def __init__(
        self,
        base_url: str,
        *,
        model: str = "kev-latest",
        timeout: float = 20.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5.0), transport=self._transport)

    def build_request(self, m: Merchant, categories: list[CategoryFacts], conventions: str, *, currency: str) -> dict[str, Any]:
        criteria = {cat.name: category_meaning(cat) for cat in categories}
        criteria[UNKNOWN] = "No listed category fits this merchant"
        state = render_merchant_facts(m, currency)
        if conventions.strip():
            state += "\n\nHousehold conventions:\n" + conventions.strip()
        return {
            "state": state,
            "model": self.model,
            "questions": {
                "category": {"type": "choice", "instructions": KEV_CATEGORY_INSTRUCTIONS, "criteria": criteria},
                "own_account_transfer": {"type": "noul", "instructions": KEV_TRANSFER_INSTRUCTIONS},
            },
        }

    async def classify(
        self,
        ctx: WorkflowContext,
        batch: list[tuple[str, Merchant]],
        categories: list[CategoryFacts],
        conventions: str,
        *,
        currency: str,
    ) -> list[Classified]:
        out: list[Classified] = []
        async with self._client() as client:
            for mid, m in batch:
                ctx.budget.check_time()
                body = self.build_request(m, categories, conventions, currency=currency)
                started = time.monotonic()
                try:
                    resp = await client.post(f"{self.base_url}/v1/systemone", json=body)
                except httpx.HTTPError as exc:
                    raise BackendUnavailable(f"kev unreachable: {exc}") from exc
                if resp.status_code >= 500:
                    raise BackendUnavailable(f"kev {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    logger.warning("kev rejected a request (%s): %s", resp.status_code, resp.text[:200])
                    out.append(Classified(mid, UNKNOWN, 0.0, f"kev rejected the request ({resp.status_code})"))
                    continue
                data = resp.json()
                answers = data.get("answers") or {}
                choice = answers.get("category") or {}
                probabilities = {str(k): float(v) for k, v in (choice.get("probabilities") or {}).items()}
                name = str(choice.get("choice") or UNKNOWN)
                p_max = max(probabilities.values()) if probabilities else float(choice.get("confidence") or 0.0)
                transfer_p = float((answers.get("own_account_transfer") or {}).get("noul") or 0.0)
                out.append(
                    Classified(
                        merchant_id=mid,
                        category_name=name,
                        confidence=round(p_max, 4),
                        reason=f"kev p={p_max:.2f}, own-account transfer p={transfer_p:.2f}",
                        probabilities=probabilities or None,
                    )
                )
                usage = data.get("usage") or {}
                try:
                    await usage_service.record_usage(
                        ctx.session,
                        user_id=ctx.user.id,
                        agent_id=ctx.agent.id,
                        conversation_id=ctx.conversation_id,
                        message_id=None,
                        provider="kev",
                        model=str(data.get("model") or self.model),
                        kind="workflow",
                        input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0),
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception:  # noqa: BLE001 — accounting must never fail a run
                    logger.exception("kev usage row failed")
        return out


def select_backend(settings: AgentSettings) -> DecisionBackend:
    """`AGENTS_CLASSIFIER_BACKEND=kev` with a base URL → Kev; anything else → the model."""
    choice = (getattr(settings, "classifier_backend", "") or "ollama").strip().lower()
    base_url = (getattr(settings, "kev_base_url", "") or "").strip()
    if choice == "kev" and base_url:
        return KevBackend(
            base_url,
            model=str(getattr(settings, "kev_model", "kev-latest") or "kev-latest"),
            timeout=float(getattr(settings, "kev_timeout_seconds", 20.0) or 20.0),
        )
    if choice == "kev":
        logger.warning("AGENTS_CLASSIFIER_BACKEND=kev but AGENTS_KEV_BASE_URL is empty; using the model")
    return OllamaStructuredBackend()
