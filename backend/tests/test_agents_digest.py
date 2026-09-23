"""Scheduled finance digest: period math, deterministic figures, and the task run.

The task is exercised end to end with the scripted provider and fake MCP from
the executor tests, so a digest is a real agent run — persisted conversation,
messages and usage — without any LLM or MCP socket.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import AsyncIterator

import pytest
from sqlalchemy import select

from app.agents.config import get_agent_settings
from app.agents.models.conversation import Conversation, Message
from app.agents.providers.base import ChatChunk, LLMProvider, LLMUnavailableError, Usage
from app.agents.runtime.executor import AgentExecutor
from app.agents.services.digest_service import (
    build_figures_pack,
    digest_title,
    is_due,
    period_bounds,
    render_figures_pack,
)
from app.agents.tasks.digest import DIGEST_CHANNEL, _run
from app.models.transaction import Transaction
from tests.test_agents_executor import _FakeMCP, _patch_provider, _ScriptedProvider

# No module-level asyncio mark: this file mixes sync helper tests with async
# ones, and asyncio_mode=auto picks up the coroutines by itself.

MONDAY = date(2026, 9, 21)
# 10:30 UTC is 07:30 in America/Sao_Paulo, the test user's timezone.
MONDAY_TICK_UTC = datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc)
TUESDAY_TICK_UTC = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
FIRST_OF_MONTH_TICK_UTC = datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc)


class _SessionMaker:
    """Hands the test's own session to the task without closing it afterwards."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _FailingProvider(LLMProvider):
    name = "openai"

    def __init__(self):
        super().__init__(api_key="x")

    async def chat_stream(  # type: ignore[override]
        self, messages, *, model, tools=None, temperature=0.4, max_tokens=None
    ) -> AsyncIterator[ChatChunk]:
        raise LLMUnavailableError("ollama down")
        yield  # pragma: no cover - makes this an async generator

    async def embed(self, texts, *, model):
        return [[0.0] * 4 for _ in texts]


def _digest_provider(text: str = "Weekly digest text") -> _ScriptedProvider:
    return _ScriptedProvider([[
        ChatChunk(type="text_delta", text=text),
        ChatChunk(type="usage", usage=Usage(input_tokens=50, output_tokens=20)),
        ChatChunk(type="finish", finish_reason="stop"),
    ]])


def _executor() -> AgentExecutor:
    return AgentExecutor(mcp=_FakeMCP(tools=[]))


@pytest.fixture
def digest_on(monkeypatch):
    settings = get_agent_settings()
    monkeypatch.setattr(settings, "enabled", True)
    monkeypatch.setattr(settings, "digest_enabled", True)
    monkeypatch.setattr(settings, "digest_hour", 7)
    return settings


@pytest.fixture
async def default_agent(session, test_agent):
    test_agent.is_default = True
    await session.commit()
    await session.refresh(test_agent)
    return test_agent


def _tx(user, account, desc, amount, day, typ, category_id=None) -> Transaction:
    return Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        category_id=category_id,
        description=desc,
        amount=Decimal(amount),
        date=day,
        type=typ,
        source="manual",
        created_at=datetime.now(timezone.utc),
    )


async def _seed_two_weeks(session, user, account, categories):
    food, transport, income = categories[0].id, categories[1].id, categories[2].id
    rows = [
        # current week: Sep 14–20
        _tx(user, account, "SALARY", "1000.00", date(2026, 9, 15), "credit", income),
        _tx(user, account, "GROCERIES", "200.00", date(2026, 9, 16), "debit", food),
        _tx(user, account, "UBER", "50.00", date(2026, 9, 17), "debit", transport),
        _tx(user, account, "MYSTERY SHOP", "30.00", date(2026, 9, 18), "debit", None),
        # previous week: Sep 7–13
        _tx(user, account, "SALARY", "800.00", date(2026, 9, 8), "credit", income),
        _tx(user, account, "GROCERIES", "100.00", date(2026, 9, 9), "debit", food),
    ]
    session.add_all(rows)
    await session.commit()


async def _digest_conversations(session, agent_id):
    return (await session.execute(
        select(Conversation).where(Conversation.agent_id == agent_id, Conversation.channel == DIGEST_CHANNEL)
    )).scalars().all()


# --- Pure helpers ---------------------------------------------------------


def test_period_bounds_weekly_and_monthly():
    assert period_bounds("weekly", MONDAY) == (date(2026, 9, 14), date(2026, 9, 20), date(2026, 9, 7), date(2026, 9, 13))
    # Any day of the week resolves to the same last complete week.
    assert period_bounds("weekly", date(2026, 9, 24)) == period_bounds("weekly", MONDAY)
    assert period_bounds("monthly", date(2026, 10, 1)) == (
        date(2026, 9, 1), date(2026, 9, 30), date(2026, 8, 1), date(2026, 8, 31),
    )
    assert period_bounds("monthly", date(2026, 1, 1)) == (
        date(2025, 12, 1), date(2025, 12, 31), date(2025, 11, 1), date(2025, 11, 30),
    )
    with pytest.raises(ValueError):
        period_bounds("daily", MONDAY)  # type: ignore[arg-type]


def test_digest_titles():
    assert digest_title("weekly", MONDAY) == "Weekly review — 2026-09-14 to 2026-09-20"
    assert digest_title("monthly", date(2026, 10, 1)) == "Monthly review — September 2026"


def test_is_due_matrix():
    monday_0730 = datetime(2026, 9, 21, 7, 30)
    monday_0659 = datetime(2026, 9, 21, 6, 59)
    tuesday_0730 = datetime(2026, 9, 22, 7, 30)
    first_0730 = datetime(2026, 10, 1, 7, 30)
    assert is_due("weekly", monday_0730, 7) is True
    assert is_due("weekly", monday_0659, 7) is False
    assert is_due("weekly", tuesday_0730, 7) is False
    assert is_due("monthly", first_0730, 7) is True
    assert is_due("monthly", monday_0730, 7) is False
    assert is_due("monthly", datetime(2026, 10, 1, 6, 0), 7) is False


# --- Figures --------------------------------------------------------------


async def test_build_figures_pack_compares_periods(session, test_user, test_workspace, test_account, test_categories):
    await _seed_two_weeks(session, test_user, test_account, test_categories)

    pack = await build_figures_pack(
        session, workspace_id=test_workspace.id, user_id=test_user.id, kind="weekly", today=MONDAY, currency="BRL"
    )

    cur, prev = pack["current"], pack["previous"]
    assert (cur["from_date"], cur["to_date"]) == ("2026-09-14", "2026-09-20")
    assert cur["income"] == 1000.0 and cur["expense"] == 280.0 and cur["net"] == 720.0
    assert cur["savings_rate"] == 0.72
    assert prev["income"] == 800.0 and prev["expense"] == 100.0
    assert pack["deltas"]["income"] == 200.0 and pack["deltas"]["expense"] == 180.0
    top = pack["top_category_deltas"]
    assert top[0]["category"] == "Alimentação" and top[0]["delta"] == 100.0
    assert {m["category"] for m in top} == {"Alimentação", "Transporte", "Uncategorized"}
    assert pack["uncategorized_count"] == 1
    assert set(pack["net_worth"]) == {"start", "end", "delta"}

    text = render_figures_pack(pack)
    assert "1,000.00 BRL" in text and "280.00 BRL" in text and "72.0%" in text
    assert "Alimentação" in text and "Uncategorized transactions in the current period: 1" in text


# --- Task run -------------------------------------------------------------


async def test_run_creates_digest_conversation_when_due(
    session, test_user, test_workspace, test_account, test_categories, default_agent, digest_on
):
    await _seed_two_weeks(session, test_user, test_account, test_categories)

    with _patch_provider(_digest_provider()):
        result = await _run(_SessionMaker(session), now=MONDAY_TICK_UTC, executor_factory=_executor)

    assert result["ran"] == [{
        "agent_id": str(default_agent.id),
        "kind": "weekly",
        "title": "Weekly review — 2026-09-14 to 2026-09-20",
        "ok": True,
    }]
    convs = await _digest_conversations(session, default_agent.id)
    assert len(convs) == 1
    assert convs[0].title == digest_title("weekly", MONDAY)
    assert convs[0].workspace_id == test_workspace.id
    msgs = (await session.execute(
        select(Message).where(Message.conversation_id == convs[0].id).order_by(Message.ordinal)
    )).scalars().all()
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert "## Figures (weekly review" in (msgs[0].content or "")
    assert "1,000.00 BRL" in (msgs[0].content or "")
    assert msgs[1].content == "Weekly digest text"


async def test_run_is_idempotent_per_period(session, test_user, default_agent, digest_on):
    with _patch_provider(_digest_provider()):
        first = await _run(_SessionMaker(session), now=MONDAY_TICK_UTC, executor_factory=_executor)
    with _patch_provider(_digest_provider("second")):
        second = await _run(_SessionMaker(session), now=MONDAY_TICK_UTC, executor_factory=_executor)

    assert len(first["ran"]) == 1
    assert second == {"ran": []}
    assert len(await _digest_conversations(session, default_agent.id)) == 1


async def test_run_monthly_on_the_first(session, test_user, default_agent, digest_on):
    with _patch_provider(_digest_provider()):
        result = await _run(_SessionMaker(session), now=FIRST_OF_MONTH_TICK_UTC, executor_factory=_executor)

    assert [r["kind"] for r in result["ran"]] == ["monthly"]
    assert result["ran"][0]["title"] == "Monthly review — September 2026"


async def test_run_not_due_outside_schedule(session, test_user, default_agent, digest_on):
    with _patch_provider(_digest_provider()):
        result = await _run(_SessionMaker(session), now=TUESDAY_TICK_UTC, executor_factory=_executor)

    assert result == {"ran": []}
    assert await _digest_conversations(session, default_agent.id) == []


async def test_run_skips_when_disabled(session, test_user, default_agent, monkeypatch):
    settings = get_agent_settings()
    monkeypatch.setattr(settings, "enabled", True)
    monkeypatch.setattr(settings, "digest_enabled", False)

    with _patch_provider(_digest_provider()):
        result = await _run(_SessionMaker(session), now=MONDAY_TICK_UTC, executor_factory=_executor)

    assert result == {"skipped": "disabled"}
    assert await _digest_conversations(session, default_agent.id) == []


async def test_run_ignores_non_default_agents(session, test_user, test_agent, digest_on):
    assert test_agent.is_default is False
    with _patch_provider(_digest_provider()):
        result = await _run(_SessionMaker(session), now=MONDAY_TICK_UTC, executor_factory=_executor)

    assert result == {"ran": []}


async def test_provider_error_removes_conversation(session, test_user, default_agent, digest_on):
    with _patch_provider(_FailingProvider()):
        result = await _run(_SessionMaker(session), now=MONDAY_TICK_UTC, executor_factory=_executor)

    assert len(result["ran"]) == 1 and result["ran"][0]["ok"] is False
    assert await _digest_conversations(session, default_agent.id) == []

    # The period is retried on the next tick because nothing was kept.
    with _patch_provider(_digest_provider()):
        retry = await _run(_SessionMaker(session), now=MONDAY_TICK_UTC, executor_factory=_executor)
    assert retry["ran"][0]["ok"] is True
    assert len(await _digest_conversations(session, default_agent.id)) == 1
