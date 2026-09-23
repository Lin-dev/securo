"""Local models mangle tool calls in predictable ways; the runtime tolerates them."""
from __future__ import annotations

import pytest

from app.agents.mcp.client import _normalize_wire_name
from mcp_server.registry import REGISTRY, ToolSpec, coerce_arguments


def _spec(props, additional=False):
    params = {"type": "object", "properties": props}
    if additional is not False:
        params["additionalProperties"] = additional
    else:
        params["additionalProperties"] = False

    async def handler(*, session, ctx, **kw):  # pragma: no cover - never called here
        return kw

    return ToolSpec(name="t", description="", parameters=params, handler=handler)


def test_coerce_arguments_drops_unknown_keys_and_keeps_known_ones():
    spec = _spec({"from_date": {"type": "string"}, "group_by": {"type": "string"}})
    out = coerce_arguments(spec, {"from_date": "2026-08-01", "group_by": "month", "status": "posted", "currency": "USD"})
    assert out == {"from_date": "2026-08-01", "group_by": "month"}


def test_coerce_arguments_passes_everything_when_schema_allows_extras():
    spec = _spec({"a": {"type": "string"}}, additional=True)
    assert coerce_arguments(spec, {"a": "1", "b": 2}) == {"a": "1", "b": 2}


def test_coerce_arguments_handles_missing_arguments():
    spec = _spec({"a": {"type": "string"}})
    assert coerce_arguments(spec, None) == {}


def test_every_registered_tool_declares_properties():
    import mcp_server.tools  # noqa: F401 — registers the tools

    for spec in REGISTRY.values():
        assert isinstance(spec.parameters.get("properties"), dict), spec.name


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("securo__get_transactions_summary", "securo__get_transactions_summary"),
        ("secular__get_transactions_summary?", "secular__get_transactions_summary"),
        ("get_money_map.", "get_money_map"),
        (" list_rules)\n", "list_rules"),
        ("", ""),
    ],
)
def test_normalize_wire_name(raw, expected):
    assert _normalize_wire_name(raw) == expected
