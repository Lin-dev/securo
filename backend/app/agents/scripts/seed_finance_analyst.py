"""Seed (or refresh) the household "Finance analyst" agent.

Idempotent: running it again updates the prompt, model, connection and tool
whitelist of the existing agent instead of creating a second one, and reuses
the Ollama connection it created before.

    python -m app.agents.scripts.seed_finance_analyst \\
        --email you@example.com --model gpt-oss:20b --base-url http://ollama:11434

Nothing here is a migration on purpose: the agent is user data (it lives in
the workspace like any agent created from the UI) and the prompt evolves
faster than the schema.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models.agent import Agent
from app.agents.models.connection import LlmConnection
from app.agents.prompts import FINANCE_ANALYST_PROMPT, FINANCE_ANALYST_TOOLS
from app.agents.schemas.agent import AgentCreate, AgentUpdate
from app.agents.services import agent_service, connection_service
from app.models.user import User

DEFAULT_AGENT_NAME = "Finance analyst"
DEFAULT_CONNECTION_NAME = "Ollama (LAN Mac)"
DEFAULT_BASE_URL = "http://ollama:11434"
# Server name the built-in MCP registry uses for Securo's own tools.
BUILTIN_MCP_SERVER = "securo"


async def _ollama_connection(
    session: AsyncSession, user_id: uuid.UUID, *, base_url: str, model: str
) -> LlmConnection:
    """Reuse an existing Ollama connection that points at `base_url` (or was
    created by this script); otherwise create one and make it the default."""
    for conn in await connection_service.list_connections(session, user_id):
        if conn.kind != "ollama":
            continue
        if (conn.base_url or DEFAULT_BASE_URL).rstrip("/") == base_url.rstrip("/") or conn.name == DEFAULT_CONNECTION_NAME:
            return conn
    return await connection_service.create_connection(
        session,
        user_id,
        name=DEFAULT_CONNECTION_NAME,
        kind="ollama",
        base_url=base_url,
        default_model=model,
        is_default=True,
    )


async def seed(
    session: AsyncSession,
    *,
    user: User,
    workspace_id: uuid.UUID,
    model: str,
    base_url: str = DEFAULT_BASE_URL,
    name: str = DEFAULT_AGENT_NAME,
) -> Agent:
    conn = await _ollama_connection(session, user.id, base_url=base_url, model=model)

    fields = dict(
        name=name,
        description="Early-retirement finance analyst over your Securo data",
        system_prompt=FINANCE_ANALYST_PROMPT,
        icon="chart-line",
        color="#0EA5E9",
        connection_id=conn.id,
        model=model,
        temperature=0.2,
        # Tool results are stored as full JSON in the history; a short window
        # keeps a 32K local context free for data instead of old turns.
        max_history_messages=10,
        top_n=6,
        similarity_threshold=0.25,
        auto_context=True,
    )
    existing = next(
        (a for a in await agent_service.list_agents(session, workspace_id, include_archived=True) if a.name == name),
        None,
    )
    if existing is None:
        agent = await agent_service.create_agent(session, workspace_id, user.id, AgentCreate(**fields))
    else:
        agent = await agent_service.update_agent(
            session, existing.id, workspace_id, AgentUpdate(**fields, is_archived=False)
        )
        assert agent is not None
    # create_agent ignores `is_default`; the update path is also what clears
    # the flag on any other default agent in the workspace.
    agent = await agent_service.update_agent(session, agent.id, workspace_id, AgentUpdate(is_default=True))
    assert agent is not None
    await agent_service.replace_tool_enablement(
        session, agent.id, [(BUILTIN_MCP_SERVER, tool, True) for tool in FINANCE_ANALYST_TOOLS]
    )
    return agent


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed or refresh the Finance analyst agent.")
    parser.add_argument("--email", required=True, help="email of the user who owns the agent")
    parser.add_argument("--model", required=True, help="Ollama model tag, e.g. gpt-oss:20b")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Ollama base URL as seen from the backend")
    parser.add_argument("--workspace", default=None, help="workspace id (default: the user's default workspace)")
    parser.add_argument("--name", default=DEFAULT_AGENT_NAME, help="agent name")
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> None:
    from app.core.database import async_session_maker
    from app.services import workspace_service

    async with async_session_maker() as session:
        user = (await session.execute(select(User).where(User.email == args.email))).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"no user with email {args.email}")
        if args.workspace:
            workspace_id = uuid.UUID(args.workspace)
        else:
            workspace = await workspace_service.get_default_workspace(session, user.id)
            if workspace is None:
                raise SystemExit("user has no workspace")
            workspace_id = workspace.id
        agent = await seed(
            session,
            user=user,
            workspace_id=workspace_id,
            model=args.model,
            base_url=args.base_url,
            name=args.name,
        )
        print(
            f"agent {agent.id} ({agent.name}) ready in workspace {workspace_id}: "
            f"model {agent.model}, {len(FINANCE_ANALYST_TOOLS)} tools enabled, default agent"
        )


if __name__ == "__main__":
    asyncio.run(_main(_parse_args()))
