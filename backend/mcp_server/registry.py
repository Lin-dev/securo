"""Tool registry for the MCP server.

Each tool is a Python coroutine registered via the @tool decorator. The
registry holds (name → ToolSpec) for /mcp's `tools/list` and `tools/call`.
"""
from __future__ import annotations

import logging

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.auth import CallContext


ToolHandler = Callable[..., Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)
    handler: ToolHandler
    # Optional. When True, the tool produces a preview (no DB writes); the
    # frontend asks the user to confirm before applying. Drives UI hints.
    is_proposal: bool = False
    tags: list[str] = field(default_factory=list)


REGISTRY: dict[str, ToolSpec] = {}
logger = logging.getLogger(__name__)


def tool(
    name: str,
    *,
    description: str,
    parameters: dict[str, Any],
    is_proposal: bool = False,
    tags: list[str] | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator. The handler must be an async function with signature
    `async def handler(session: AsyncSession, ctx: CallContext, **kwargs)`.
    """
    def deco(fn: ToolHandler) -> ToolHandler:
        if name in REGISTRY:
            raise RuntimeError(f"duplicate tool registration: {name}")
        REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            handler=fn,
            is_proposal=is_proposal,
            tags=list(tags or []),
        )
        return fn
    return deco


def list_tools() -> list[dict[str, Any]]:
    """MCP-compatible tool list payload."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "inputSchema": s.parameters,
            "_securo": {"is_proposal": s.is_proposal, "tags": s.tags},
        }
        for s in REGISTRY.values()
    ]


async def call_tool(
    session: AsyncSession,
    ctx: CallContext,
    name: str,
    arguments: dict[str, Any] | None,
) -> Any:
    spec = REGISTRY.get(name)
    if spec is None:
        raise KeyError(f"unknown tool: {name}")
    return await spec.handler(session=session, ctx=ctx, **coerce_arguments(spec, arguments))


def coerce_arguments(spec: ToolSpec, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the arguments the tool's JSON schema declares.

    Local models pad calls with plausible-looking extras (`status`, `currency`,
    …) copied from other tools' schemas. Failing the whole call over one stray
    key sends an error back into the conversation and often derails the next
    call too; dropping the key and logging it is the useful behaviour. Schemas
    without a `properties` map (or with `additionalProperties` allowed) pass
    everything through unchanged.
    """
    args = dict(arguments or {})
    props = spec.parameters.get("properties") if isinstance(spec.parameters, dict) else None
    if not isinstance(props, dict) or spec.parameters.get("additionalProperties", False) is not False:
        return args
    unknown = [k for k in args if k not in props]
    for k in unknown:
        args.pop(k, None)
    if unknown:
        logger.warning("tool %s: dropped unknown argument(s) %s", spec.name, unknown)
    return args
