"""Celery task: the scheduled weekly/monthly finance digest.

Beat ticks this hourly (`finance-digest-hourly-tick`). Celery runs in UTC
with integer schedules, so each tick converts "now" into the agent owner's
timezone and runs the digest that is due there: weekly on Monday, monthly on
the 1st, at or after `AGENTS_DIGEST_HOUR`. The digest is a normal agent run
in a fresh conversation (channel "digest"), so it is persisted, usage-logged
and shows up in the agent's history like any chat. Idempotent per period:
the conversation title is the key.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.config import get_agent_settings
from app.agents.models.agent import Agent
from app.agents.models.conversation import Conversation
from app.agents.runtime.executor import AgentExecutor
from app.agents.services import conversation_service
from app.agents.services.digest_service import (
    DigestKind,
    build_figures_pack,
    digest_title,
    is_due,
    render_figures_pack,
)
from app.core.config import get_settings
from app.models.user import User
from app.worker import celery_app

logger = logging.getLogger(__name__)

DIGEST_CHANNEL = "digest"
DIGEST_KINDS: tuple[DigestKind, ...] = ("weekly", "monthly")

DIGEST_INSTRUCTION = (
    "Write the {kind} finance review from the figures below. Use only these figures "
    "(Securo computed them); you may call list_uncategorized_merchants once to name the "
    "largest uncategorized merchants, and no other tool. Structure: a 3-5 bullet headline "
    "(income, expenses, net, savings rate versus the previous period), notable category "
    "moves, investing, net worth change, one suggested action. Under 200 words, one table "
    "at most, no chart."
)

# Error codes where the model produced a transcript worth keeping (the executor
# already persisted a visible fallback). Everything else is deleted so the next
# hourly tick retries the period.
_KEEP_CONVERSATION_ERRORS = frozenset({"max_iterations"})


@celery_app.task(name="app.agents.tasks.digest.run_finance_digests")
def run_finance_digests() -> dict:
    """Sync entry point that runs the async digest pass in a fresh loop."""
    return asyncio.run(_run())


async def _run(
    session_maker=None,
    *,
    now: Optional[datetime] = None,
    executor_factory: Callable[[], AgentExecutor] = AgentExecutor,
) -> dict[str, Any]:
    settings = get_agent_settings()
    if not (settings.enabled and settings.digest_enabled):
        return {"skipped": "disabled"}

    # Same pattern as the ingest task: a fresh NullPool engine per run so
    # prefork workers never reuse asyncpg connections across event loops.
    engine = None
    if session_maker is None:
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_maker() as session:
            return await _run_all(
                session,
                now=now or datetime.now(timezone.utc),
                hour=int(settings.digest_hour),
                executor_factory=executor_factory,
            )
    finally:
        if engine is not None:
            await engine.dispose()


async def _run_all(
    session: AsyncSession,
    *,
    now: datetime,
    hour: int,
    executor_factory: Callable[[], AgentExecutor],
) -> dict[str, Any]:
    agents = (
        await session.execute(
            select(Agent).where(Agent.is_default.is_(True), Agent.is_archived.is_(False)).order_by(Agent.created_at)
        )
    ).scalars().all()
    ran: list[dict[str, Any]] = []
    for agent in agents:
        user = await session.get(User, agent.user_id)
        if user is None:
            continue
        local_now = now.astimezone(_timezone((user.preferences or {}).get("timezone")))
        for kind in DIGEST_KINDS:
            if not is_due(kind, local_now, hour):
                continue
            title = digest_title(kind, local_now.date())
            if await _exists(session, agent.id, title):
                continue
            ok = await _run_one(session, agent, user, kind, local_now.date(), title, executor_factory)
            ran.append({"agent_id": str(agent.id), "kind": kind, "title": title, "ok": ok})
    return {"ran": ran}


def _timezone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


async def _exists(session: AsyncSession, agent_id: uuid.UUID, title: str) -> bool:
    row = await session.execute(
        select(Conversation.id)
        .where(
            Conversation.agent_id == agent_id,
            Conversation.channel == DIGEST_CHANNEL,
            Conversation.title == title,
        )
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def _run_one(
    session: AsyncSession,
    agent: Agent,
    user: User,
    kind: DigestKind,
    today: date,
    title: str,
    executor_factory: Callable[[], AgentExecutor],
) -> bool:
    try:
        pack = await build_figures_pack(
            session,
            workspace_id=agent.workspace_id,
            user_id=user.id,
            kind=kind,
            today=today,
            currency=user.primary_currency,
        )
    except Exception:  # noqa: BLE001
        logger.exception("digest figures failed for agent %s (%s)", agent.id, title)
        return False

    conv = await conversation_service.create_conversation(
        session,
        workspace_id=agent.workspace_id,
        user_id=user.id,
        agent_id=agent.id,
        channel=DIGEST_CHANNEL,
        title=title,
    )
    prompt = DIGEST_INSTRUCTION.format(kind=kind) + "\n\n" + render_figures_pack(pack)

    error_code: Optional[str] = None
    try:
        async for ev in executor_factory().run(
            session=session,
            agent=agent,
            user_id=user.id,
            workspace_id=agent.workspace_id,
            conversation_id=conv.id,
            user_message=prompt,
            channel=DIGEST_CHANNEL,
        ):
            if ev.type == "error":
                error_code = ev.error_code or "unknown"
                logger.warning("digest %s failed: %s (%s)", title, ev.error_message, error_code)
    except Exception:  # noqa: BLE001
        logger.exception("digest run crashed for %s", title)
        error_code = "crash"

    if error_code is None:
        return True
    if error_code not in _KEEP_CONVERSATION_ERRORS:
        await conversation_service.delete_conversation(session, conv.id, agent.workspace_id)
    return False
