"""/api/wealth — the road to the user's net-worth milestone (fork, Wealth Cockpit).

Gated on the `wealth` module. Each endpoint answers 501 until its slice lands:
settings (B1), then summary, net worth history, savings rate, projection and
levers (B3). Shapes are fixed in app/schemas/wealth.py.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.module_gate import require_module, require_module_write
from app.core.workspace_context import WorkspaceContext
from app.schemas.wealth import (
    NetWorthHistory,
    SavingsRateSeries,
    WealthLevers,
    WealthProjection,
    WealthSettingsRead,
    WealthSettingsUpdate,
    WealthSummary,
)
from app.services.module_service import ModuleId
from app.services.wealth.settings import read_settings, update_settings

router = APIRouter(prefix="/api/wealth", tags=["wealth"])

read_ctx = require_module(ModuleId.WEALTH)
write_ctx = require_module_write(ModuleId.WEALTH)


def _not_yet() -> HTTPException:
    return HTTPException(status_code=501, detail="Not implemented yet")


@router.get("/settings", response_model=WealthSettingsRead)
async def get_settings(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> WealthSettingsRead:
    return await read_settings(session, ctx)


@router.put("/settings", response_model=WealthSettingsRead)
async def put_settings(
    body: WealthSettingsUpdate,
    ctx: WorkspaceContext = Depends(write_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> WealthSettingsRead:
    return await update_settings(session, ctx, body)


@router.get("/summary", response_model=WealthSummary)
async def get_summary(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> WealthSummary:
    raise _not_yet()


@router.get("/net-worth-history", response_model=NetWorthHistory)
async def get_net_worth_history(
    months: int = Query(12, ge=1, le=120),
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> NetWorthHistory:
    raise _not_yet()


@router.get("/savings-rate", response_model=SavingsRateSeries)
async def get_savings_rate(
    months: int = Query(12, ge=1, le=60),
    ctx: WorkspaceContext = Depends(read_ctx),
    session: AsyncSession = Depends(get_async_session),
) -> SavingsRateSeries:
    raise _not_yet()


@router.get("/projection", response_model=WealthProjection)
async def get_projection(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> WealthProjection:
    raise _not_yet()


@router.get("/levers", response_model=WealthLevers)
async def get_levers(
    ctx: WorkspaceContext = Depends(read_ctx), session: AsyncSession = Depends(get_async_session)
) -> WealthLevers:
    raise _not_yet()
