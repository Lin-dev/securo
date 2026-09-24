"""Workflow catalogue: registration, slash commands, and the synthetic tool
definitions the model can call as `workflow__<name>`."""
from __future__ import annotations

import re
from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable

from app.agents.mcp.client import ToolHandle
from app.agents.providers.base import ToolDefinition
from app.agents.runtime.executor import ExecutorEvent
from app.agents.workflows.base import WorkflowContext

SERVER_NAME = "workflow"
TOOL_PREFIX = "workflow__"
# `/ask <text>` is not a workflow: the executor strips it and forces the
# free-form loop. Exposed here so both sides agree on the spelling.
ASK_COMMAND = "ask"


@runtime_checkable
class Workflow(Protocol):
    name: str
    title: str
    description: str
    aliases: tuple[str, ...]
    params_schema: dict[str, Any]

    def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> AsyncIterator[ExecutorEvent]: ...


WORKFLOWS: dict[str, Workflow] = {}
_ALIASES: dict[str, str] = {}


def register(wf: Workflow) -> Workflow:
    if wf.name in WORKFLOWS or wf.name in _ALIASES or wf.name == ASK_COMMAND:
        raise ValueError(f"duplicate workflow registration: {wf.name}")
    for alias in getattr(wf, "aliases", ()) or ():
        if alias in WORKFLOWS or alias in _ALIASES or alias == ASK_COMMAND:
            raise ValueError(f"duplicate workflow alias: {alias}")
    WORKFLOWS[wf.name] = wf
    for alias in getattr(wf, "aliases", ()) or ():
        _ALIASES[alias] = wf.name
    return wf


def unregister(name: str) -> None:
    wf = WORKFLOWS.pop(name, None)
    if wf is None:
        return
    for alias in getattr(wf, "aliases", ()) or ():
        _ALIASES.pop(alias, None)


def get(name: str) -> Optional[Workflow]:
    if not name:
        return None
    key = name.strip().lower()
    return WORKFLOWS.get(key) or WORKFLOWS.get(_ALIASES.get(key, ""))


def is_workflow_tool(wire_name: str) -> bool:
    return workflow_for_tool(wire_name) is not None


def workflow_for_tool(wire_name: str) -> Optional[Workflow]:
    if not wire_name or not wire_name.startswith(TOOL_PREFIX):
        return None
    return get(wire_name[len(TOOL_PREFIX):])


def tool_name_for(wf: Workflow) -> str:
    return f"{TOOL_PREFIX}{wf.name}"


def as_tool_definitions(allowed: Optional[set[tuple[str, str]]]) -> list[ToolDefinition]:
    """Tool definitions for the model. `allowed` is the agent's whitelist of
    `(server, tool)` pairs; `None` means every workflow is offered."""
    return [
        ToolDefinition(name=tool_name_for(wf), description=wf.description, parameters=wf.params_schema)
        for wf in WORKFLOWS.values()
        if allowed is None or (SERVER_NAME, wf.name) in allowed
    ]


def as_tool_handles() -> list[ToolHandle]:
    """Handles under the pseudo-server `workflow`, so the agents tools API can
    list them next to the MCP tools and the whitelist UI can toggle them."""
    return [
        ToolHandle(
            server=SERVER_NAME,
            name=wf.name,
            description=wf.description,
            parameters=wf.params_schema,
            is_proposal=False,
        )
        for wf in WORKFLOWS.values()
    ]


_SLASH_RE = re.compile(r"^/([A-Za-z][\w-]*)(?:\s+(.*))?$", re.S)
_PARAM_RE = re.compile(r'([A-Za-z_][\w-]*)=("[^"]*"|\'[^\']*\'|\S+)')
_PARAM_ALIASES = {"from": "from_date", "to": "to_date"}
_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def strip_ask_prefix(text: str) -> Optional[str]:
    """`/ask <question>` → `<question>`; anything else → None."""
    m = _SLASH_RE.match((text or "").strip())
    if not m or m.group(1).lower() != ASK_COMMAND:
        return None
    return (m.group(2) or "").strip()


def parse_slash_command(text: str) -> Optional[tuple[Workflow, dict[str, Any]]]:
    """`/categorize from=2026-08-01 limit=20` → (workflow, {"from_date": …, "limit": 20}).

    Parameters are `key=value` tokens coerced by the workflow's params schema;
    unknown keys are dropped. Returns None for anything that is not a known
    workflow (including `/ask`).
    """
    m = _SLASH_RE.match((text or "").strip())
    if not m:
        return None
    wf = get(m.group(1))
    if wf is None:
        return None
    return wf, _parse_params(m.group(2) or "", wf.params_schema)


def _parse_params(raw: str, schema: dict[str, Any]) -> dict[str, Any]:
    props = (schema or {}).get("properties") or {}
    out: dict[str, Any] = {}
    for key, value in _PARAM_RE.findall(raw or ""):
        key = _PARAM_ALIASES.get(key.lower(), key.lower())
        if key not in props:
            continue
        value = value.strip("\"'")
        kind = props[key].get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), None)
        try:
            if kind == "boolean":
                low = value.lower()
                if low in _TRUE:
                    out[key] = True
                elif low in _FALSE:
                    out[key] = False
            elif kind == "integer":
                out[key] = int(value)
            elif kind == "number":
                out[key] = float(value)
            else:
                out[key] = value
        except ValueError:
            continue
    return out


# Modules that register the shipped workflows on import (idempotent: Python
# caches the module, and `register` refuses duplicates anyway).
_BUILTIN_MODULES: tuple[str, ...] = ("app.agents.workflows.categorize",)


def load_builtin() -> None:
    """Import the shipped workflows so they register. Idempotent."""
    import importlib

    for module in _BUILTIN_MODULES:
        importlib.import_module(module)
