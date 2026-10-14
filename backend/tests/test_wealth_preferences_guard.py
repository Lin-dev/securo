"""User.preferences["wealth"] survives every whole-object preferences writer (fork, plan P6).

Only PUT /api/wealth/settings may change it. The frontend sends the whole object
back on PATCH /users/me (onboarding does `{...user.preferences, onboarding_completed:
true}`), the admin panel replaces it, and a workspace currency change writes back a
copy taken at request start; each must keep the stored wealth settings.
"""
import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.admin import AdminUserUpdate
from app.services import admin_service
from app.services.wealth.preferences import load_preferences_for_update

pytestmark = pytest.mark.asyncio

WEALTH = {"schema_version": 1, "projection": {"return": 0.06}}


async def _store_wealth(session: AsyncSession, user: User) -> dict:
    """Store WEALTH and return the copy a client loaded before it existed."""
    before = dict(user.preferences or {})
    user.preferences = {**before, "wealth": WEALTH}
    session.add(user)
    await session.commit()
    return before


async def _stored(session: AsyncSession, user_id: uuid.UUID) -> dict:
    return (await session.execute(select(User.preferences).where(User.id == user_id))).scalar_one() or {}


async def test_completing_onboarding_keeps_wealth_settings(client: AsyncClient, session, test_user, auth_headers):
    copy_without_wealth = await _store_wealth(session, test_user)

    response = await client.patch(
        "/api/users/me", json={"preferences": {**copy_without_wealth, "onboarding_completed": True}}, headers=auth_headers,
    )

    assert response.status_code == 200
    prefs = await _stored(session, test_user.id)
    assert prefs["wealth"] == WEALTH
    assert prefs["onboarding_completed"] is True


async def test_a_stale_wealth_copy_cannot_overwrite_the_stored_one(client: AsyncClient, session, test_user, auth_headers):
    copy_without_wealth = await _store_wealth(session, test_user)
    stale = {"schema_version": 1, "projection": {"return": 0.99}}

    response = await client.patch(
        "/api/users/me", json={"preferences": {**copy_without_wealth, "wealth": stale}}, headers=auth_headers,
    )

    assert response.status_code == 200
    assert (await _stored(session, test_user.id))["wealth"] == WEALTH


async def test_admin_preferences_edit_keeps_wealth_settings(session, test_user):
    await _store_wealth(session, test_user)

    await admin_service.update_user(session, test_user.id, AdminUserUpdate(preferences={"language": "en"}), test_user.id)

    prefs = await _stored(session, test_user.id)
    assert prefs == {"language": "en", "wealth": WEALTH}


async def test_workspace_currency_change_keeps_wealth_settings(
    client: AsyncClient, session, test_user, test_workspace, auth_headers
):
    await _store_wealth(session, test_user)

    response = await client.patch(f"/api/workspaces/{test_workspace.id}", json={"default_currency": "EUR"}, headers=auth_headers)

    assert response.status_code == 200
    prefs = await _stored(session, test_user.id)
    assert prefs["currency_display"] == "EUR"
    assert prefs["wealth"] == WEALTH


async def test_preferences_read_locks_the_user_row_on_postgres(postgres_sessions):
    """SQLite ignores FOR UPDATE, so only PostgreSQL can show the lock is taken."""
    async with postgres_sessions() as setup:
        user = User(
            id=uuid.uuid4(), email=f"lock-{uuid.uuid4().hex[:8]}@test.com", hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True, preferences={"wealth": WEALTH},
        )
        setup.add(user)
        await setup.commit()

    async with postgres_sessions() as holder, postgres_sessions() as other:
        assert (await load_preferences_for_update(holder, user.id))["wealth"] == WEALTH
        await other.execute(text("SET lock_timeout = '500ms'"))
        with pytest.raises(Exception, match="lock"):
            await asyncio.wait_for(
                other.execute(select(User.preferences).where(User.id == user.id).with_for_update()), timeout=10,
            )
        await holder.rollback()
