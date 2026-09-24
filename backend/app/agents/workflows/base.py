"""Workflow runtime: context, budget, structured model steps, event emission.

The executor (stage that wires `workflow__*` tools and slash commands) builds a
`WorkflowContext`, iterates `workflow.run(ctx, params)` for events, and reads
`ctx.result` / `ctx.pending_proposals` afterwards to persist the turn. Nothing
in this module writes chat rows; it only produces events and previews.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import mcp_server.tools  # noqa: F401  (registers the built-in tool handlers once per process)
from app.agents.config import AgentSettings, get_agent_settings
from app.agents.models.agent import Agent
from app.agents.providers.base import ChatMessage, ChatResponse, LLMProvider, LLMUnavailableError
from app.agents.runtime.executor import ExecutorEvent, _safe_json, _summarize_result
from app.agents.services import knowledge_service, usage_service
from app.models.user import User
from mcp_server.auth import CallContext
from mcp_server.registry import REGISTRY, coerce_arguments
from mcp_server.tools._helpers import resolve_workspace_id

logger = logging.getLogger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)


class WorkflowError(Exception):
    """Base class for workflow failures the executor turns into an error event."""


class WorkflowStepError(WorkflowError):
    """A structured model step did not produce valid output within its retries."""


class WorkflowBudgetExceeded(WorkflowError):
    """The run hit its wall-clock or model-call budget."""


@dataclass
class WorkflowBudget:
    """Hard limits for one run. Every model call reserves a slot first, so a
    workflow that loops can never exceed them by accident."""

    max_llm_calls: int = 8
    max_seconds: float = 240.0
    started: float = field(default_factory=time.monotonic)
    llm_calls: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check_time(self) -> None:
        if self.elapsed() > self.max_seconds:
            raise WorkflowBudgetExceeded(f"workflow exceeded {self.max_seconds:.0f}s")

    def reserve_llm_call(self) -> None:
        self.check_time()
        if self.llm_calls >= self.max_llm_calls:
            raise WorkflowBudgetExceeded(f"workflow exceeded {self.max_llm_calls} model calls")
        self.llm_calls += 1

    @property
    def remaining_llm_calls(self) -> int:
        return max(0, self.max_llm_calls - self.llm_calls)


@dataclass
class WorkflowResult:
    """What the run leaves behind.

    `summary` is the markdown the user reads (persisted as the assistant
    message when `final`); `data` is the compact dict the model sees as the
    tool result when the workflow was invoked as a tool and is not final.
    """

    summary: str
    data: dict[str, Any]
    final: bool = True
    ok: bool = True


@dataclass
class PendingProposal:
    """One proposal card to persist: an assistant `tool_calls` entry plus a
    matching `role="tool"` row sharing `tool_call_id`, which is exactly what
    the chat UI renders as an applyable card (live and on reload)."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


def new_proposal_id() -> str:
    return f"wf_{uuid.uuid4().hex[:8]}"


def extract_json(text: str) -> Any:
    """Parse the JSON a local model returned, tolerating the usual noise:
    a `<think>…</think>` / `<reasoning>` prefix, markdown fences, or prose
    around the object. Raises ValueError when no JSON object can be found."""
    s = text or ""
    for marker in ("</think>", "</reasoning>"):
        if marker in s:
            s = s.split(marker, 1)[1]
    s = s.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", s, flags=re.S)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc


@dataclass
class WorkflowContext:
    """Everything a workflow may touch. Built once per run by the executor."""

    session: AsyncSession
    user: User
    workspace_id: uuid.UUID
    agent: Agent
    conversation_id: Optional[uuid.UUID]
    provider: LLMProvider
    model: str
    settings: AgentSettings
    budget: WorkflowBudget
    language: str
    call_ctx: CallContext
    pending_proposals: list[PendingProposal] = field(default_factory=list)
    result: Optional[WorkflowResult] = None
    usage_input: int = 0
    usage_output: int = 0
    notes: list[str] = field(default_factory=list)

    # --- data access ---------------------------------------------------------

    async def tool(self, tool_name: str, /, **arguments: Any) -> Any:
        """Call a built-in MCP tool handler in-process (no HTTP, no JWT).

        `call_ctx.external` is False, so `propose_*` tools stay previews.
        Unknown arguments are dropped exactly as the MCP server does. The tool
        name is positional-only so tools with a `name` argument (rules) work.
        """
        spec = REGISTRY.get(tool_name)
        if spec is None:
            raise WorkflowStepError(f"unknown tool {tool_name}")
        self.budget.check_time()
        return await spec.handler(
            session=self.session, ctx=self.call_ctx, **coerce_arguments(spec, arguments)
        )

    async def pinned_text(self, max_chars: int = 3000) -> str:
        """The agent's pinned knowledge (household conventions), bounded."""
        if max_chars <= 0:
            return ""
        chunks = await knowledge_service.list_pinned_chunks(
            self.session, agent_id=self.agent.id, max_chunks=20
        )
        text = "\n---\n".join((c.get("content") or "").strip() for c in chunks if c.get("content"))
        return text[:max_chars]

    # --- model steps ---------------------------------------------------------

    async def llm_structured(
        self,
        system: str,
        user: str,
        schema: Type[TModel],
        *,
        reasoning: str = "low",
        temperature: float = 0.0,
        max_tokens: int = 2500,
        retries: int = 2,
        json_schema: Optional[dict[str, Any]] = None,
    ) -> TModel:
        """One closed question to the model, answered as `schema`.

        The JSON schema goes to the provider as `response_format` (grammar
        constrained on Ollama) and is also spelled out in the system prompt for
        providers without grammars. Tools are never sent alongside a schema.
        An invalid reply is sent back once per remaining retry with the
        validation error; a provider that rejects `response_format` (HTTP 400)
        gets one more attempt without it. Every attempt reserves budget and is
        logged as `kind="workflow"` usage.
        """
        schema_json = json_schema or schema.model_json_schema()
        system_full = (
            system.rstrip()
            + "\n\nReply with JSON only, matching this JSON schema exactly:\n"
            + json.dumps(schema_json, ensure_ascii=False)
        )
        messages = [
            ChatMessage(role="system", content=system_full),
            ChatMessage(role="user", content=user),
        ]
        use_format = True
        attempts_left = retries + 1
        last_error = "no attempt"
        while attempts_left > 0:
            attempts_left -= 1
            self.budget.reserve_llm_call()
            started = time.monotonic()
            try:
                resp = await self.provider.chat(
                    messages,
                    model=self.model,
                    tools=None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=schema_json if use_format else None,
                    reasoning=reasoning,
                )
            except LLMUnavailableError as exc:
                if use_format and exc.status == 400:
                    # OpenAI-compatible servers that do not know json_schema.
                    use_format = False
                    attempts_left += 1
                    self.notes.append(
                        "the model server rejected structured output; used prompt-only JSON"
                    )
                    logger.warning("structured output rejected (400); retrying without format")
                    continue
                raise
            await self._record_usage(resp, started)
            text = resp.content or ""
            try:
                obj = extract_json(text)
                return schema.model_validate(obj)
            except (ValueError, ValidationError) as exc:
                last_error = str(exc).splitlines()[0][:400] if str(exc) else exc.__class__.__name__
                logger.info("structured step invalid (%s); attempts left %d", last_error, attempts_left)
                messages = messages + [
                    ChatMessage(role="assistant", content=text[:4000]),
                    ChatMessage(
                        role="user",
                        content=(
                            f"Your previous reply was not valid: {last_error}. "
                            "Reply with JSON only, exactly matching the schema."
                        ),
                    ),
                ]
        raise WorkflowStepError(f"structured step failed after {retries + 1} attempt(s): {last_error}")

    async def _record_usage(self, resp: ChatResponse, started: float) -> None:
        usage = getattr(resp, "usage", None)
        in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.usage_input += in_tokens
        self.usage_output += out_tokens
        try:
            await usage_service.record_usage(
                self.session,
                user_id=self.user.id,
                agent_id=self.agent.id,
                conversation_id=self.conversation_id,
                message_id=None,
                provider=getattr(self.provider, "name", "unknown"),
                model=self.model,
                kind="workflow",
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:  # noqa: BLE001 — accounting must never fail a run
            logger.exception("workflow usage row failed")

    # --- events ----------------------------------------------------------------

    def emit_text(self, text: str) -> ExecutorEvent:
        return ExecutorEvent(type="text_delta", text=text)

    def emit_step(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> tuple[ExecutorEvent, ExecutorEvent]:
        """Progress chips for the UI (a tool_call/tool_result pair named
        `workflow.<step>`); they are not persisted."""
        tool_name = f"workflow.{name}"
        return (
            ExecutorEvent(type="tool_call", tool_name=tool_name, tool_args=dict(args)),
            ExecutorEvent(
                type="tool_result",
                tool_name=tool_name,
                tool_result=_summarize_result({"ok": True, "data": result}),
            ),
        )

    def emit_proposals(
        self, items: list[tuple[str, dict[str, Any], dict[str, Any]]]
    ) -> list[ExecutorEvent]:
        """Queue proposal cards and return their events: all `tool_call`s first,
        then the `tool_result`s in the same order (the UI pairs them FIFO by
        tool name)."""
        pending = [
            PendingProposal(tool_call_id=new_proposal_id(), tool_name=tool_name, arguments=dict(arguments), result=result)
            for tool_name, arguments, result in items
        ]
        self.pending_proposals.extend(pending)
        calls = [ExecutorEvent(type="tool_call", tool_name=p.tool_name, tool_args=p.arguments) for p in pending]
        results = [
            ExecutorEvent(
                type="tool_result",
                tool_name=p.tool_name,
                tool_result=_summarize_result({"ok": True, "data": p.result}),
            )
            for p in pending
        ]
        return calls + results

    def finish(self, summary: str, data: dict[str, Any], *, final: bool = True, ok: bool = True) -> None:
        self.result = WorkflowResult(summary=summary, data=data, final=final, ok=ok)

    def compact_json(self, obj: Any) -> str:
        return _safe_json(obj)


def language_for(user: Optional[User], agent: Optional[Agent] = None) -> str:
    extra = (getattr(agent, "extra", None) or {}) if agent is not None else {}
    prefs = (getattr(user, "preferences", None) or {}) if user is not None else {}
    return str(extra.get("language") or prefs.get("language") or "en")


async def build_context(
    session: AsyncSession,
    *,
    user: User,
    agent: Agent,
    conversation_id: Optional[uuid.UUID],
    provider: LLMProvider,
    model: str,
    workspace_id: Optional[uuid.UUID] = None,
    settings: Optional[AgentSettings] = None,
    budget: Optional[WorkflowBudget] = None,
) -> WorkflowContext:
    """Resolve the workspace and assemble a `WorkflowContext`."""
    settings = settings or get_agent_settings()
    call_ctx = CallContext(
        user_id=user.id,
        workspace_id=workspace_id or getattr(agent, "workspace_id", None),
        conversation_id=conversation_id,
        agent_id=agent.id,
        external=False,
    )
    ws_id = await resolve_workspace_id(session, call_ctx)
    call_ctx.workspace_id = ws_id
    return WorkflowContext(
        session=session,
        user=user,
        workspace_id=ws_id,
        agent=agent,
        conversation_id=conversation_id,
        provider=provider,
        model=model,
        settings=settings,
        budget=budget
        or WorkflowBudget(
            max_llm_calls=int(settings.workflow_max_llm_calls),
            max_seconds=float(settings.workflow_max_seconds),
        ),
        language=language_for(user, agent),
        call_ctx=call_ctx,
    )


async def collect(workflow: Any, ctx: WorkflowContext, params: dict[str, Any]) -> tuple[list[ExecutorEvent], WorkflowResult]:
    """Drive a workflow to completion headlessly; returns its events and result.

    Used by tests and by callers that want the result without streaming (a
    scheduled task, for instance). The executor streams instead of collecting.
    """
    events: list[ExecutorEvent] = []
    stream: AsyncIterator[ExecutorEvent] = workflow.run(ctx, params)
    async for ev in stream:
        events.append(ev)
    if ctx.result is None:
        ctx.finish(summary="", data={"ok": False, "error": "workflow ended without a result"}, ok=False)
    return events, ctx.result  # type: ignore[return-value]
