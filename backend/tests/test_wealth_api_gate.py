"""/api/wealth is mounted, gated on the wealth module, and fixed to its contract (fork)."""
import pytest
from httpx import AsyncClient

from app.api.wealth import router
from app.schemas.wealth import BUCKETS, Bucket

_PATHS = ["/settings", "/summary", "/net-worth-history", "/savings-rate", "/projection", "/levers"]


@pytest.mark.asyncio
async def test_personal_workspace_reaches_every_wealth_route(client: AsyncClient, auth_headers):
    for path in _PATHS:
        response = await client.get(f"/api/wealth{path}", headers=auth_headers)
        # every slice has landed (B1 settings, B3 the rest); never 404 (unmounted or gated off)
        assert response.status_code == 200, (path, response.status_code, response.text)


@pytest.mark.asyncio
async def test_business_workspace_does_not_see_wealth(client: AsyncClient, auth_headers):
    created = await client.post(
        "/api/workspaces", headers=auth_headers,
        json={"name": "Consultoria", "kind": "business", "self_membership": True},
    )
    assert created.status_code == 201, created.text
    headers = {**auth_headers, "X-Workspace-Id": created.json()["id"]}

    for path in _PATHS:
        response = await client.get(f"/api/wealth{path}", headers=headers)
        assert response.status_code == 404, (path, response.status_code)


def test_router_serves_exactly_the_contracted_paths():
    paths = {getattr(r, "path", "") for r in router.routes}
    assert paths == {f"/api/wealth{p}" for p in _PATHS}


def test_bucket_tuple_matches_the_literal():
    assert set(BUCKETS) == set(Bucket.__args__)
