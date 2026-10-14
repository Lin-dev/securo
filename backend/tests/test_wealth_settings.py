"""Wealth settings: load/read/update (fork, plan P6)."""
import asyncio
import logging
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workspace_context import WorkspaceContext
from app.models.account import Account
from app.models.goal import Goal
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.schemas.wealth import AccountAnnotation, ProjectionSettings, WealthSettingsUpdate, WealthSettingsValues
from app.services.wealth import settings as settings_service
from app.services.wealth.buckets import fallback_key
from app.services.wealth.figures import account_facts_from
from app.services.wealth.preferences import load_preferences_for_update

pytestmark = pytest.mark.asyncio

_SOURCE_FIELDS = ("goal_id", "projection", "buffer_months", "income_category_ids", "one_off_overrides", "accounts")


async def _ctx(session: AsyncSession, test_user: User, test_workspace) -> WorkspaceContext:
    member = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == test_workspace.id, WorkspaceMember.user_id == test_user.id,
            )
        )
    ).scalar_one()
    return WorkspaceContext(workspace=test_workspace, member=member, user=test_user)


async def _account(session: AsyncSession, user_id, workspace_id, name, acc_type, **kw) -> Account:
    acc = Account(
        id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, name=name,
        type=acc_type, balance=Decimal("0"), currency="USD", **kw,
    )
    session.add(acc)
    await session.commit()
    await session.refresh(acc)
    return acc


# --- load_settings / defaults ------------------------------------------------------


async def test_defaults_on_a_fresh_user(session, test_user, test_workspace):
    assert settings_service.load_settings(test_user) == WealthSettingsValues()

    ctx = await _ctx(session, test_user, test_workspace)
    result = await settings_service.read_settings(session, ctx)

    assert result.values == WealthSettingsValues()
    assert result.sources == {field: "default" for field in _SOURCE_FIELDS}
    assert result.accounts == []
    assert result.income_categories == []
    assert result.one_offs == []


async def test_invalid_stored_data_falls_back_to_defaults(session, test_user, caplog):
    test_user.preferences = {**(test_user.preferences or {}), "wealth": {"buffer_months": "not-a-number"}}
    session.add(test_user)
    await session.commit()

    with caplog.at_level(logging.WARNING):
        result = settings_service.load_settings(test_user)

    assert result == WealthSettingsValues()
    assert any("invalid stored wealth settings" in r.message for r in caplog.records)


# --- update_settings: merge, accounts, sources --------------------------------------


async def test_put_partial_merge_keeps_other_fields(session, test_user, test_workspace):
    ctx = await _ctx(session, test_user, test_workspace)
    await settings_service.update_settings(session, ctx, WealthSettingsUpdate(buffer_months=5))

    result = await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(projection=ProjectionSettings(return_nominal=0.08))
    )

    assert result.values.buffer_months == 5
    assert result.values.projection.return_nominal == 0.08


async def test_accounts_null_removes_an_override(session, test_user, test_workspace):
    ctx = await _ctx(session, test_user, test_workspace)
    key = str(uuid.uuid4())
    await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={key: AccountAnnotation(bucket="cash")})
    )
    assert key in (await settings_service.read_settings(session, ctx)).values.accounts

    result = await settings_service.update_settings(session, ctx, WealthSettingsUpdate(accounts={key: None}))

    assert key not in result.values.accounts


async def test_fallback_key_is_written_by_the_server(session, test_user, test_workspace):
    account = await _account(session, test_user.id, test_workspace.id, "Robinhood", "investment")
    ctx = await _ctx(session, test_user, test_workspace)

    result = await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={str(account.id): AccountAnnotation(bucket="crypto")})
    )

    expected = fallback_key(account_facts_from(account))
    assert result.values.accounts[str(account.id)].fallback_key == expected
    assert result.values.accounts[str(account.id)].bucket == "crypto"


async def test_account_override_field_level_merge_keeps_untouched_fields(session, test_user, test_workspace):
    account = await _account(session, test_user.id, test_workspace.id, "Robinhood", "investment")
    key = str(account.id)
    ctx = await _ctx(session, test_user, test_workspace)
    await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={key: AccountAnnotation(bucket="retirement_roth", liquid=False)})
    )

    result = await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={key: AccountAnnotation(liquid=True)})
    )

    stored = result.values.accounts[key]
    assert stored.bucket == "retirement_roth"  # untouched by the second PUT
    assert stored.liquid is True


async def test_account_override_explicit_null_field_clears_only_that_field(session, test_user, test_workspace):
    account = await _account(session, test_user.id, test_workspace.id, "Robinhood", "investment")
    key = str(account.id)
    ctx = await _ctx(session, test_user, test_workspace)
    await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={key: AccountAnnotation(bucket="retirement_roth", liquid=False)})
    )

    result = await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={key: AccountAnnotation(bucket=None)})
    )

    stored = result.values.accounts[key]
    assert stored.bucket is None  # cleared -> falls back to the account's default
    assert stored.liquid is False  # untouched


async def test_account_override_with_nothing_left_is_dropped(session, test_user, test_workspace):
    account = await _account(session, test_user.id, test_workspace.id, "Robinhood", "investment")
    key = str(account.id)
    ctx = await _ctx(session, test_user, test_workspace)
    await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={key: AccountAnnotation(bucket="retirement_roth")})
    )

    result = await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={key: AccountAnnotation(bucket=None)})
    )

    assert key not in result.values.accounts  # bucket, liquid and apr are all now None


async def test_orphaned_rows(session, test_user, test_workspace):
    ctx = await _ctx(session, test_user, test_workspace)
    key = str(uuid.uuid4())  # no account has this id, and no fallback_key is set either
    await settings_service.update_settings(
        session, ctx, WealthSettingsUpdate(accounts={key: AccountAnnotation(bucket="cash")})
    )

    result = await settings_service.read_settings(session, ctx)

    orphaned = [row for row in result.accounts if row.orphaned]
    assert len(orphaned) == 1
    assert orphaned[0].account_id is None
    assert orphaned[0].override_key == key


async def test_sources_report_user_only_for_fields_actually_set(session, test_user, test_workspace):
    ctx = await _ctx(session, test_user, test_workspace)
    await settings_service.update_settings(session, ctx, WealthSettingsUpdate(buffer_months=10))

    result = await settings_service.read_settings(session, ctx)

    assert result.sources["buffer_months"] == "user"
    for field in _SOURCE_FIELDS:
        if field != "buffer_months":
            assert result.sources[field] == "default"


# --- goal validation -----------------------------------------------------------------


async def test_invalid_goal_gives_422(session, test_user, test_workspace):
    ctx = await _ctx(session, test_user, test_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await settings_service.update_settings(session, ctx, WealthSettingsUpdate(goal_id=uuid.uuid4()))

    assert exc_info.value.status_code == 422


async def test_goal_with_wrong_tracking_type_gives_422(session, test_user, test_workspace):
    goal = Goal(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="Vacation",
        target_amount=Decimal("1000"), tracking_type="manual",
    )
    session.add(goal)
    await session.commit()
    ctx = await _ctx(session, test_user, test_workspace)

    with pytest.raises(HTTPException) as exc_info:
        await settings_service.update_settings(session, ctx, WealthSettingsUpdate(goal_id=goal.id))

    assert exc_info.value.status_code == 422


async def test_valid_net_worth_goal_is_accepted(session, test_user, test_workspace):
    goal = Goal(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="A Million",
        target_amount=Decimal("1000000"), tracking_type="net_worth",
    )
    session.add(goal)
    await session.commit()
    ctx = await _ctx(session, test_user, test_workspace)

    result = await settings_service.update_settings(session, ctx, WealthSettingsUpdate(goal_id=goal.id))

    assert result.values.goal_id == goal.id


# --- Postgres: PUT takes the row lock -----------------------------------------------


async def test_put_settings_takes_the_row_lock_on_postgres(postgres_sessions):
    """`update_settings`'s first step is `load_preferences_for_update`: SQLite ignores
    FOR UPDATE, so only PostgreSQL can show the lock is taken (mirrors
    test_wealth_preferences_guard.py::test_preferences_read_locks_the_user_row_on_postgres)."""
    async with postgres_sessions() as setup:
        user = User(
            id=uuid.uuid4(), email=f"wealth-put-{uuid.uuid4().hex[:8]}@test.com", hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True, preferences={},
        )
        setup.add(user)
        await setup.commit()

    async with postgres_sessions() as holder, postgres_sessions() as other:
        assert (await load_preferences_for_update(holder, user.id)) == {}
        await other.execute(text("SET lock_timeout = '500ms'"))
        with pytest.raises(Exception, match="lock"):
            await asyncio.wait_for(
                other.execute(select(User.preferences).where(User.id == user.id).with_for_update()), timeout=10,
            )
        await holder.rollback()


async def test_concurrent_puts_do_not_lose_an_update_on_postgres(postgres_sessions):
    """Two concurrent PUTs each change a different field; the row lock serializes
    them instead of the second clobbering the first's write."""
    from app.models.workspace import Workspace

    async with postgres_sessions() as setup:
        user = User(
            id=uuid.uuid4(), email=f"wealth-concurrent-{uuid.uuid4().hex[:8]}@test.com", hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True, preferences={},
        )
        workspace = Workspace(
            id=uuid.uuid4(), name="W", kind="personal", created_by_user_id=user.id, default_currency="USD",
        )
        setup.add_all([user, workspace])
        await setup.flush()
        member = WorkspaceMember(id=uuid.uuid4(), workspace_id=workspace.id, user_id=user.id, role="owner")
        setup.add(member)
        await setup.commit()

    other_category_id = uuid.uuid4()

    async def _put(body: WealthSettingsUpdate) -> None:
        async with postgres_sessions() as session:
            ctx = WorkspaceContext(
                workspace=await session.get(Workspace, workspace.id),
                member=await session.get(WorkspaceMember, member.id),
                user=await session.get(User, user.id),
            )
            await settings_service.update_settings(session, ctx, body)

    await asyncio.gather(
        _put(WealthSettingsUpdate(buffer_months=10)),
        _put(WealthSettingsUpdate(income_category_ids=[other_category_id])),
    )

    async with postgres_sessions() as check:
        stored = (await check.execute(select(User.preferences).where(User.id == user.id))).scalar_one()

    assert stored["wealth"]["buffer_months"] == 10
    assert stored["wealth"]["income_category_ids"] == [str(other_category_id)]


# --- API round trip -------------------------------------------------------------------


async def test_api_round_trip(client: AsyncClient, auth_headers):
    get_resp = await client.get("/api/wealth/settings", headers=auth_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["values"]["buffer_months"] == 3.0

    put_resp = await client.put("/api/wealth/settings", json={"buffer_months": 7}, headers=auth_headers)
    assert put_resp.status_code == 200
    assert put_resp.json()["values"]["buffer_months"] == 7

    get_again = await client.get("/api/wealth/settings", headers=auth_headers)
    assert get_again.json()["values"]["buffer_months"] == 7


async def test_api_put_invalid_goal_returns_422(client: AsyncClient, auth_headers):
    resp = await client.put(
        "/api/wealth/settings", json={"goal_id": str(uuid.uuid4())}, headers=auth_headers,
    )
    assert resp.status_code == 422
