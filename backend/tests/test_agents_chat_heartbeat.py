"""SSE heartbeat for the agents chat stream.

Local models can be silent for a minute before the first byte; proxies close
idle responses. `_with_heartbeat` yields None markers during silence, which the
endpoint turns into SSE comment lines that every parser ignores.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.api import chat as chat_api
from app.agents.api.chat import _HEARTBEAT, _with_heartbeat
from app.models.user import User


async def _collect(gen):
    return [x async for x in gen]


async def test_with_heartbeat_passes_items_in_order_when_fast():
    async def src():
        for i in range(5):
            yield i

    assert await _collect(_with_heartbeat(src(), interval=1.0)) == [0, 1, 2, 3, 4]


async def test_with_heartbeat_emits_markers_during_silence():
    async def src():
        yield "a"
        await asyncio.sleep(0.12)
        yield "b"

    out = await _collect(_with_heartbeat(src(), interval=0.02))
    assert out[0] == "a" and out[-1] == "b"
    markers = [x for x in out if x is None]
    assert len(markers) >= 2, out
    # nothing but markers sits between the two real items
    assert [x for x in out if x is not None] == ["a", "b"]


async def test_with_heartbeat_closes_source_on_early_exit():
    closed = asyncio.Event()

    async def src():
        try:
            yield "first"
            await asyncio.sleep(10)
            yield "never"  # pragma: no cover
        finally:
            closed.set()

    gen = _with_heartbeat(src(), interval=0.05)
    first = await gen.__anext__()
    assert first == "first"
    # consumer goes away (client disconnected): the pending __anext__ task is
    # cancelled and the source generator's finally runs; no exception escapes.
    await gen.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1.0)


async def test_chat_stream_carries_heartbeat_comments_when_executor_is_slow(
    client: AsyncClient, auth_headers: dict, session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
):
    from app.agents.models.agent import Agent
    from app.agents.runtime.executor import ExecutorEvent

    agent = Agent(id=uuid.uuid4(), user_id=test_user.id, name="Slow")
    session.add(agent)
    await session.commit()

    class _SlowExecutor:
        def __init__(self, *a, **kw):
            pass

        async def run(self, **kw):
            await asyncio.sleep(0.15)
            yield ExecutorEvent(type="text_delta", text="hi")
            yield ExecutorEvent(type="done", finish_reason="stop")

    monkeypatch.setattr(chat_api, "AgentExecutor", _SlowExecutor)
    monkeypatch.setattr(chat_api, "SSE_HEARTBEAT_SECONDS", 0.02)

    r = await client.post(f"/api/agents/{agent.id}/chat", headers=auth_headers, json={"content": "hello"})
    assert r.status_code == 200, r.text
    body = r.content
    assert body.startswith(b"event: conversation\n")
    assert body.count(_HEARTBEAT) >= 2
    assert b"event: text_delta" in body and b"event: done" in body
    # comments never split an event: every heartbeat sits on its own blank-line-terminated block
    for block in body.split(b"\n\n"):
        if block.startswith(b":"):
            assert block == b": ping"
