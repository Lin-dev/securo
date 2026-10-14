"""Keys in User.preferences that only their own endpoint may write.

`PATCH /users/me` replaces the whole preferences object, and the frontend sends
back whatever it loaded (`{...user.preferences, onboarding_completed: true}`).
A tab opened before the wealth settings changed would otherwise write the old
copy back. Every whole-object writer therefore keeps the database value of
these keys, read under a row lock, and only `PUT /api/wealth/settings` changes
them. Selecting the column (not the entity) returns the row's current value,
not the copy already loaded into the session.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

PROTECTED_PREFERENCE_KEYS: tuple[str, ...] = ("wealth",)


async def load_preferences_for_update(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """The user's stored preferences, with the row locked until the transaction ends."""
    stored: Optional[dict[str, Any]] = (
        await session.execute(select(User.preferences).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    return dict(stored or {})


async def keep_protected_preferences(
    session: AsyncSession, user_id: uuid.UUID, incoming: dict[str, Any]
) -> dict[str, Any]:
    """`incoming` with every protected key replaced by its stored value (or dropped if none)."""
    stored = await load_preferences_for_update(session, user_id)
    merged = {k: v for k, v in incoming.items() if k not in PROTECTED_PREFERENCE_KEYS}
    for key in PROTECTED_PREFERENCE_KEYS:
        if key in stored:
            merged[key] = stored[key]
    return merged
