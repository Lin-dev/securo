"""Reconnect tokens carry the connection's credentials, and disconnecting
tells the provider to release the link (best effort)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_connection import BankConnection
from app.providers.base import BankProvider, ConnectTokenData
from app.services.connection_service import create_connect_token, delete_connection


class _Fake(BankProvider):
    """Minimal concrete provider to exercise the base-class defaults."""

    def __init__(self):
        self.calls = []

    @property
    def name(self):
        return "fake"

    async def create_connect_token(self, client_user_id, item_id=None):
        self.calls.append(("connect", client_user_id, item_id))
        return ConnectTokenData(access_token=f"tok-{item_id or 'new'}")

    async def get_oauth_url(self, redirect_uri, state, flow_params=None):
        raise NotImplementedError

    async def handle_oauth_callback(self, code):
        raise NotImplementedError

    async def get_accounts(self, credentials):
        return []

    async def get_transactions(self, credentials, account_external_id, since=None, payee_source="auto"):
        return []

    async def refresh_credentials(self, credentials):
        return credentials


@pytest.mark.asyncio
async def test_base_reconnect_token_defaults_to_the_item_id_convention():
    fake = _Fake()
    token = await fake.create_reconnect_token("user-1", {"item_id": "item-9"})
    assert token.access_token == "tok-item-9"
    assert fake.calls == [("connect", "user-1", "item-9")]
    assert await fake.revoke({"item_id": "item-9"}) is None  # default no-op


@pytest.mark.asyncio
async def test_service_routes_credentials_to_create_reconnect_token(test_user):
    provider = AsyncMock()
    provider.create_reconnect_token = AsyncMock(return_value=ConnectTokenData(access_token="update-tok"))
    provider.create_connect_token = AsyncMock(return_value=ConnectTokenData(access_token="new-tok"))
    with patch("app.services.connection_service.get_provider", return_value=provider):
        assert await create_connect_token("plaid", test_user.id, credentials={"item_id": "i"}) == {"access_token": "update-tok"}
        assert await create_connect_token("plaid", test_user.id) == {"access_token": "new-tok"}
    provider.create_reconnect_token.assert_awaited_once_with(str(test_user.id), {"item_id": "i"})
    provider.create_connect_token.assert_awaited_once_with(str(test_user.id), item_id=None)


async def _connection(session, user_id, workspace_id, credentials):
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}", institution_name="Bye Bank",
        credentials=credentials, status="active",
        last_sync_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    return conn.id


@pytest.mark.asyncio
async def test_delete_connection_revokes_at_the_provider(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    conn_id = await _connection(session, uid, wid, {"item_id": "item-1"})
    provider = AsyncMock()
    provider.revoke = AsyncMock()
    with patch("app.services.connection_service.get_provider", return_value=provider):
        await delete_connection(session, conn_id, wid)
    provider.revoke.assert_awaited_once_with({"item_id": "item-1"})
    assert await session.scalar(select(BankConnection).where(BankConnection.id == conn_id)) is None


@pytest.mark.asyncio
async def test_delete_connection_survives_a_failing_or_unknown_provider(session: AsyncSession, test_user, test_workspace):
    uid, wid = test_user.id, test_workspace.id
    failing_id = await _connection(session, uid, wid, {"item_id": "item-2"})
    provider = AsyncMock()
    provider.revoke = AsyncMock(side_effect=RuntimeError("item already removed"))
    with patch("app.services.connection_service.get_provider", return_value=provider):
        await delete_connection(session, failing_id, wid)
    assert await session.scalar(select(BankConnection).where(BankConnection.id == failing_id)) is None

    unknown_id = await _connection(session, uid, wid, {"item_id": "item-3"})
    # provider "test" is not registered: get_provider raises ValueError
    await delete_connection(session, unknown_id, wid)
    assert await session.scalar(select(BankConnection).where(BankConnection.id == unknown_id)) is None
