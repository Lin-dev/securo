"""Scheduled syncs must not keep spending an aggregator's request budget on
credentials it already refused; a reconnect clears the flag."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.tasks import sync_tasks
from tests.conftest import TestSessionLocal


class _NoDisposeEngine:
    async def dispose(self):  # the test engine is shared; never dispose it
        return None


@pytest.mark.asyncio
async def test_scheduled_sync_skips_connections_awaiting_reconnect(session: AsyncSession, test_connection):
    test_connection.last_sync_at = datetime.now(timezone.utc) - timedelta(days=2)
    test_connection.status = "error"
    test_connection.credentials = {**(test_connection.credentials or {}), "action_required_at": datetime.now(timezone.utc).isoformat()}
    await session.commit()

    with patch.object(sync_tasks, "_make_session_maker", return_value=(_NoDisposeEngine(), TestSessionLocal)), \
         patch.object(sync_tasks, "_sync_one", new=AsyncMock()) as sync_one:
        synced = await sync_tasks._sync_all()
    assert synced == 0
    sync_one.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_sync_still_runs_stale_connections_without_the_flag(session: AsyncSession, test_connection):
    test_connection.last_sync_at = datetime.now(timezone.utc) - timedelta(days=2)
    test_connection.status = "error"
    test_connection.credentials = {k: v for k, v in (test_connection.credentials or {}).items() if k != "action_required_at"}
    await session.commit()

    with patch.object(sync_tasks, "_make_session_maker", return_value=(_NoDisposeEngine(), TestSessionLocal)), \
         patch.object(sync_tasks, "_sync_one", new=AsyncMock()) as sync_one:
        synced = await sync_tasks._sync_all()
    assert synced == 1
    sync_one.assert_awaited_once()


def test_every_beat_entry_expires_after_its_interval():
    from app.worker import celery_app

    for name, entry in celery_app.conf.beat_schedule.items():
        assert entry.get("options", {}).get("expires") == entry["schedule"], name


@pytest.mark.asyncio
async def test_refused_sync_flags_the_connection(session: AsyncSession, test_connection, test_user):
    from app.providers.base import ProviderUserActionRequired
    from app.services import connection_service

    provider = MagicMock()
    provider.refresh_credentials = AsyncMock(side_effect=lambda c: c)
    provider.get_accounts = AsyncMock(side_effect=ProviderUserActionRequired("refused (403)", code="credentials_invalid"))
    with patch.object(connection_service, "get_provider", return_value=provider):
        with pytest.raises(ProviderUserActionRequired):
            await connection_service.sync_connection(session, test_connection.id, test_connection.workspace_id, test_user.id)
    await session.refresh(test_connection)
    assert test_connection.status == "error"
    assert (test_connection.credentials or {}).get("action_required_at")
