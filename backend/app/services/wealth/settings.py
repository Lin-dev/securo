"""Wealth settings stored in `User.preferences["wealth"]` (plan P6).

`load_settings` is the only reader every other wealth module should use — it
never raises, so a page with corrupted stored data still renders with
defaults. `update_settings` is the only writer: it merges under
`preferences.load_preferences_for_update`'s row lock, so a stale whole-object
PATCH from another tab can never clobber it (see
`tests/test_wealth_preferences_guard.py`).
"""
from __future__ import annotations

import logging
import uuid
from collections import namedtuple
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.app_clock import app_today
from app.core.workspace_context import WorkspaceContext
from app.models.account import Account
from app.models.category import Category
from app.models.category_group import CategoryGroup
from app.models.goal import Goal
from app.models.user import User
from app.schemas.wealth import (
    AccountAnnotation,
    AccountSettingRow,
    IncomeCategoryRow,
    SettingSource,
    WealthSettingsRead,
    WealthSettingsUpdate,
    WealthSettingsValues,
)
from app.services.wealth.buckets import choose_bucket, fallback_key
from app.services.wealth.classify import is_default_income_category
from app.services.wealth.figures import account_facts_from, detect_one_offs, effective_income_category_ids
from app.services.wealth.preferences import load_preferences_for_update

logger = logging.getLogger(__name__)

_NamedCat = namedtuple("_NamedCat", ["name", "group_name"])

# Top-level fields `sources` reports on (schema_version is internal bookkeeping,
# not something a user "sets").
_SOURCE_FIELDS: tuple[str, ...] = (
    "goal_id", "projection", "buffer_months", "income_category_ids", "one_off_overrides", "accounts",
)


def load_settings(user: User) -> WealthSettingsValues:
    """`user.preferences["wealth"]`, validated against the stored shape. Missing or
    invalid data falls back to defaults — this must never raise, since a settings
    page (or any page reading wealth figures) has to render regardless of what got
    written there by an older schema version or a hand-edited row."""
    raw = (user.preferences or {}).get("wealth")
    return _parse_stored(raw, user.id)


def _sources(raw: Any) -> dict[str, SettingSource]:
    present = raw if isinstance(raw, dict) else {}
    return {field: ("user" if field in present else "default") for field in _SOURCE_FIELDS}


async def _account_rows(
    session: AsyncSession, workspace_id: uuid.UUID, values: WealthSettingsValues
) -> list[AccountSettingRow]:
    """One row per open account (via `choose_bucket`), plus a row for every override
    that no current account claims — by id or by fallback key (`orphaned=True`)."""
    accounts = (
        await session.execute(
            select(Account)
            .where(Account.workspace_id == workspace_id, Account.is_closed.is_(False))
            .options(selectinload(Account.connection))
        )
    ).scalars().all()

    rows: list[AccountSettingRow] = []
    claimed_keys: set[str] = set()
    for account in accounts:
        facts = account_facts_from(account)
        choice = choose_bucket(facts, values.accounts)
        claimed_keys.add(choice.override_key)
        rows.append(AccountSettingRow(
            account_id=account.id, name=account.name, type=account.type, institution=facts.institution,
            default_bucket=choice.default_bucket, bucket=choice.bucket, liquid=choice.liquid,
            apr=choice.apr, source=choice.source, orphaned=False, override_key=choice.override_key,
        ))

    for key, annotation in values.accounts.items():
        if key in claimed_keys:
            continue
        name = key
        if annotation.fallback_key:
            parts = annotation.fallback_key.split("|", 2)
            if len(parts) == 3 and parts[2]:
                name = parts[2]
        rows.append(AccountSettingRow(
            account_id=None, name=name, type=None, institution=None, default_bucket=None,
            bucket=annotation.bucket or "cash", liquid=annotation.liquid if annotation.liquid is not None else False,
            apr=annotation.apr, source="user", orphaned=True, override_key=key,
        ))
    return rows


async def _income_category_rows(
    session: AsyncSession, workspace_id: uuid.UUID, values: WealthSettingsValues
) -> list[IncomeCategoryRow]:
    rows = (
        await session.execute(
            select(Category, CategoryGroup.name)
            .outerjoin(CategoryGroup, Category.group_id == CategoryGroup.id)
            .where(Category.workspace_id == workspace_id, Category.treat_as_transfer.is_(False))
        )
    ).all()
    effective = await effective_income_category_ids(session, workspace_id, values)
    return [
        IncomeCategoryRow(
            id=cat.id, name=cat.name, group_name=group_name,
            is_default=is_default_income_category(_NamedCat(cat.name, group_name)),
            selected=cat.id in effective,
        )
        for cat, group_name in rows
    ]


async def read_settings(session: AsyncSession, ctx: WorkspaceContext) -> WealthSettingsRead:
    """Effective settings plus their sources, and the rows the settings page lists:
    accounts (with defaults, fallback keys, orphans), income categories, one-offs."""
    raw = (ctx.user.preferences or {}).get("wealth")
    values = load_settings(ctx.user)
    return WealthSettingsRead(
        values=values,
        sources=_sources(raw),
        accounts=await _account_rows(session, ctx.id, values),
        income_categories=await _income_category_rows(session, ctx.id, values),
        one_offs=await detect_one_offs(session, ctx.id, ctx.user_id, app_today(), settings=values),
    )


async def update_settings(
    session: AsyncSession, ctx: WorkspaceContext, body: WealthSettingsUpdate
) -> WealthSettingsRead:
    """A partial merge of `body` onto the stored settings, under
    `load_preferences_for_update`'s row lock: read the whole preferences dict,
    replace only its "wealth" key, assign the whole dict back to `user.preferences`,
    then commit — the pattern every whole-object preferences writer has to respect
    (`wealth.preferences.keep_protected_preferences`)."""
    if body.goal_id is not None and not body.clear_goal:
        goal = await session.get(Goal, body.goal_id)
        if goal is None or goal.workspace_id != ctx.id or goal.tracking_type != "net_worth":
            raise HTTPException(
                status_code=422, detail="goal_id must be a net-worth goal in this workspace"
            )

    stored_prefs = await load_preferences_for_update(session, ctx.user_id)
    raw_wealth = stored_prefs.get("wealth")
    current = _parse_stored(raw_wealth, ctx.user_id)
    # Stored sparse (only the keys a PUT has ever touched), not a full dump of
    # `current` — `sources` tells "user" from "default" by key presence, and a
    # full dump would make every field look user-set after the first PUT.
    sparse: dict[str, Any] = dict(raw_wealth) if isinstance(raw_wealth, dict) else {}

    if body.clear_goal:
        sparse["goal_id"] = None
    elif body.goal_id is not None:
        sparse["goal_id"] = str(body.goal_id)
    if body.projection is not None:
        sparse["projection"] = body.projection.model_dump(mode="json")
    if body.buffer_months is not None:
        sparse["buffer_months"] = body.buffer_months
    if body.reset_income_categories:
        sparse["income_category_ids"] = None
    elif body.income_category_ids is not None:
        sparse["income_category_ids"] = [str(cat_id) for cat_id in body.income_category_ids]

    if body.one_off_overrides is not None:
        one_offs = dict(current.one_off_overrides)
        for txn_id, value in body.one_off_overrides.items():
            if value is None:
                one_offs.pop(txn_id, None)
            else:
                one_offs[txn_id] = value
        sparse["one_off_overrides"] = one_offs

    if body.accounts is not None:
        accounts = {key: annotation.model_dump(mode="json") for key, annotation in current.accounts.items()}
        for key, annotation in body.accounts.items():
            if annotation is None:
                accounts.pop(key, None)
                continue
            merged = _merge_annotation(current.accounts.get(key, AccountAnnotation()), annotation)
            account_id = _parse_uuid(key)
            if account_id is not None:
                # `account.institution` is eager (model-level selectin); `connection`
                # is not, and `account_facts_from` falls back to reading it when
                # `institution` is None — a lazy load that raises under async
                # SQLAlchemy unless it is eager-loaded here too. A plain `select()`
                # (not `session.get()`) so the eager load always runs even when the
                # account is already in this session's identity map, where `get()`
                # would silently skip the options and return the cached, unloaded
                # instance.
                account = (
                    await session.execute(
                        select(Account)
                        .where(Account.id == account_id)
                        .options(selectinload(Account.connection))
                    )
                ).scalar_one_or_none()
                if account is not None and account.workspace_id == ctx.id:
                    merged = merged.model_copy(
                        update={"fallback_key": fallback_key(account_facts_from(account))}
                    )
            if merged.bucket is None and merged.liquid is None and merged.apr is None:
                accounts.pop(key, None)  # nothing left to override — drop the key
                continue
            accounts[key] = merged.model_dump(mode="json")
        sparse["accounts"] = accounts

    # Catch a bug in the merge above (not user input, already validated by
    # `WealthSettingsUpdate`) before it reaches storage.
    WealthSettingsValues.model_validate(sparse)

    stored_prefs["wealth"] = sparse
    ctx.user.preferences = stored_prefs
    session.add(ctx.user)
    await session.commit()

    return await read_settings(session, ctx)


def _parse_stored(raw: Any, user_id: uuid.UUID) -> WealthSettingsValues:
    if raw is None:
        return WealthSettingsValues()
    try:
        return WealthSettingsValues.model_validate(raw)
    except (ValidationError, TypeError, ValueError) as exc:
        logger.warning("invalid stored wealth settings for user %s: %s", user_id, exc)
        return WealthSettingsValues()


def _parse_uuid(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _merge_annotation(stored: AccountAnnotation, incoming: AccountAnnotation) -> AccountAnnotation:
    """Field-by-field PATCH onto `stored`: only fields the client actually sent
    (`incoming.model_fields_set`) change. A field sent as `null` clears it (falls
    back to the default); a field never mentioned keeps its stored value.
    `fallback_key` is excluded here — the server always writes its own, below."""
    updates = {field: getattr(incoming, field) for field in incoming.model_fields_set if field != "fallback_key"}
    return stored.model_copy(update=updates)
