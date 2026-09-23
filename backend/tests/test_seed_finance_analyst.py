"""The Finance analyst seed is idempotent: two runs, one agent, one connection."""
import pytest
from sqlalchemy import select

from app.agents.models.agent import Agent, AgentTool
from app.agents.models.connection import LlmConnection
from app.agents.prompts import FINANCE_ANALYST_PROMPT, FINANCE_ANALYST_TOOLS
from app.agents.scripts.seed_finance_analyst import DEFAULT_AGENT_NAME, seed

pytestmark = pytest.mark.asyncio


async def _agents(session, workspace_id):
    return (await session.execute(select(Agent).where(Agent.workspace_id == workspace_id))).scalars().all()


async def test_seed_creates_default_agent_connection_and_tools(session, test_user, test_workspace):
    agent = await seed(
        session,
        user=test_user,
        workspace_id=test_workspace.id,
        model="gpt-oss:20b",
        base_url="http://ollama:11434",
    )

    assert agent.name == DEFAULT_AGENT_NAME
    assert agent.is_default is True
    assert agent.model == "gpt-oss:20b"
    assert agent.system_prompt == FINANCE_ANALYST_PROMPT
    assert agent.temperature == 0.2
    assert agent.max_history_messages == 10

    conns = (await session.execute(select(LlmConnection).where(LlmConnection.user_id == test_user.id))).scalars().all()
    assert len(conns) == 1
    assert conns[0].kind == "ollama"
    assert conns[0].is_default is True
    assert conns[0].base_url == "http://ollama:11434"
    assert conns[0].default_model == "gpt-oss:20b"
    assert conns[0].api_key_encrypted is None
    assert agent.connection_id == conns[0].id

    tools = (await session.execute(select(AgentTool).where(AgentTool.agent_id == agent.id))).scalars().all()
    assert len(tools) == len(FINANCE_ANALYST_TOOLS) == 20
    assert {t.tool_name for t in tools} == set(FINANCE_ANALYST_TOOLS)
    assert all(t.enabled and t.server == "securo" for t in tools)


async def test_seed_twice_updates_instead_of_duplicating(session, test_user, test_workspace):
    first = await seed(session, user=test_user, workspace_id=test_workspace.id, model="gpt-oss:20b")
    second = await seed(session, user=test_user, workspace_id=test_workspace.id, model="qwen3.8:27b")

    assert second.id == first.id
    assert second.model == "qwen3.8:27b"
    assert second.is_default is True
    assert len(await _agents(session, test_workspace.id)) == 1

    conns = (await session.execute(select(LlmConnection).where(LlmConnection.user_id == test_user.id))).scalars().all()
    assert len(conns) == 1 and conns[0].is_default is True

    tools = (await session.execute(select(AgentTool).where(AgentTool.agent_id == first.id))).scalars().all()
    assert len(tools) == 20


async def test_seed_takes_over_default_from_another_agent(session, test_user, test_workspace, test_agent):
    test_agent.is_default = True
    await session.commit()

    agent = await seed(session, user=test_user, workspace_id=test_workspace.id, model="gpt-oss:20b")

    await session.refresh(test_agent)
    assert agent.is_default is True
    assert test_agent.is_default is False
    assert len(await _agents(session, test_workspace.id)) == 2
