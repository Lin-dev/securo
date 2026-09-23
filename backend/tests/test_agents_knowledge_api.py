"""HTTP-level coverage for /api/agents/{id}/knowledge.

Goes through the FastAPI router with the conftest `client` and
`auth_headers` fixtures so we exercise auth + route + service in one
trip. Celery dispatch is mocked because we don't run a worker in tests.
"""
from __future__ import annotations

import io
import tempfile
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture(autouse=True)
def _storage_in_tmpdir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="kb-api-")
    from app.agents.config import get_agent_settings

    monkeypatch.setattr(get_agent_settings(), "knowledge_storage_path", tmp)
    yield tmp


@pytest.fixture(autouse=True)
def _stub_celery_dispatch():
    """Knowledge upload does send_task → we don't want it to actually
    enqueue anything during tests. Returning a sentinel keeps the upload
    happy."""
    with patch("app.worker.celery_app.send_task", return_value=None) as m:
        yield m


async def _make_agent(client, auth_headers, name="KB Agent") -> str:
    r = await client.post("/api/agents", json={"name": name}, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_list_knowledge_404_for_unknown_agent(client, auth_headers, test_user):
    r = await client.get(f"/api/agents/{uuid.uuid4()}/knowledge", headers=auth_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_knowledge_returns_empty_for_new_agent(client, auth_headers, test_user):
    aid = await _make_agent(client, auth_headers)
    r = await client.get(f"/api/agents/{aid}/knowledge", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_upload_knowledge_persists_doc_and_dispatches_celery(
    client, auth_headers, test_user, _stub_celery_dispatch
):
    aid = await _make_agent(client, auth_headers)
    files = {"file": ("notes.md", io.BytesIO(b"# Title\nbody"), "text/markdown")}
    r = await client.post(
        f"/api/agents/{aid}/knowledge",
        files=files,
        data={"pinned": "false"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "notes.md"
    assert body["mime"] == "text/markdown"
    assert body["status"] == "pending"
    assert body["pinned"] is False
    assert body["size_bytes"] == len(b"# Title\nbody")
    # Celery was asked to schedule the ingest task once.
    _stub_celery_dispatch.assert_called_once()
    args, _ = _stub_celery_dispatch.call_args
    assert args[0] == "app.agents.tasks.ingest.ingest_doc"

    # And it shows up in list_knowledge.
    r = await client.get(f"/api/agents/{aid}/knowledge", headers=auth_headers)
    assert r.json()["total"] == 1


@pytest.mark.asyncio
async def test_upload_knowledge_inline_marks_doc_ready(
    client, auth_headers, test_user, session, monkeypatch, _stub_celery_dispatch
):
    """AGENTS_KNOWLEDGE_INGEST_INLINE=true: no Celery dispatch; the ingest
    runs as a background task of the request and the doc is `ready` by
    the time the ASGI call returns (httpx's transport awaits it)."""
    import app.agents.api.knowledge as knowledge_api
    from app.agents.config import get_agent_settings

    monkeypatch.setattr(get_agent_settings(), "knowledge_ingest_inline", True)
    monkeypatch.setattr(
        knowledge_api, "async_session_maker", async_sessionmaker(session.bind, expire_on_commit=False)
    )

    async def fake_embed(chunks):
        return [[0.0] * 1536 for _ in chunks], "fake-model"

    monkeypatch.setattr("app.agents.services.embedding.embed_texts", fake_embed)

    aid = await _make_agent(client, auth_headers)
    r = await client.post(
        f"/api/agents/{aid}/knowledge",
        files={"file": ("conventions.md", io.BytesIO(b"# Conventions\nTransfers are not income."), "text/markdown")},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    _stub_celery_dispatch.assert_not_called()

    docs = (await client.get(f"/api/agents/{aid}/knowledge", headers=auth_headers)).json()
    assert docs["total"] == 1
    assert docs["items"][0]["status"] == "ready"
    assert docs["items"][0]["chunk_count"] >= 1
    assert docs["items"][0]["error"] is None


@pytest.mark.asyncio
async def test_upload_knowledge_404_for_unknown_agent(client, auth_headers, test_user):
    r = await client.post(
        f"/api/agents/{uuid.uuid4()}/knowledge",
        files={"file": ("x.txt", io.BytesIO(b"x"), "text/plain")},
        headers=auth_headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_upload_knowledge_returns_413_when_too_large(
    client, auth_headers, test_user, monkeypatch
):
    from app.agents.config import get_agent_settings

    monkeypatch.setattr(get_agent_settings(), "knowledge_max_file_size_mb", 1)
    aid = await _make_agent(client, auth_headers)
    big = b"x" * (1024 * 1024 + 1)
    r = await client.post(
        f"/api/agents/{aid}/knowledge",
        files={"file": ("big.bin", io.BytesIO(big), "application/octet-stream")},
        headers=auth_headers,
    )
    assert r.status_code == 413
    assert "exceeds" in r.json()["detail"]


@pytest.mark.asyncio
async def test_toggle_pin_flips_flag(client, auth_headers, test_user):
    aid = await _make_agent(client, auth_headers)
    r = await client.post(
        f"/api/agents/{aid}/knowledge",
        files={"file": ("p.txt", io.BytesIO(b"p"), "text/plain")},
        headers=auth_headers,
    )
    doc_id = r.json()["id"]

    # Pin
    r = await client.patch(
        f"/api/agents/{aid}/knowledge/{doc_id}/pin?pinned=true", headers=auth_headers
    )
    assert r.status_code == 200
    assert r.json()["pinned"] is True

    # Unpin
    r = await client.patch(
        f"/api/agents/{aid}/knowledge/{doc_id}/pin?pinned=false", headers=auth_headers
    )
    assert r.status_code == 200
    assert r.json()["pinned"] is False


@pytest.mark.asyncio
async def test_toggle_pin_404_for_unknown_doc(client, auth_headers, test_user):
    aid = await _make_agent(client, auth_headers)
    r = await client.patch(
        f"/api/agents/{aid}/knowledge/{uuid.uuid4()}/pin?pinned=true", headers=auth_headers
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_knowledge_removes_doc(client, auth_headers, test_user):
    aid = await _make_agent(client, auth_headers)
    r = await client.post(
        f"/api/agents/{aid}/knowledge",
        files={"file": ("d.txt", io.BytesIO(b"d"), "text/plain")},
        headers=auth_headers,
    )
    doc_id = r.json()["id"]

    r = await client.delete(f"/api/agents/{aid}/knowledge/{doc_id}", headers=auth_headers)
    assert r.status_code == 204

    docs = (await client.get(f"/api/agents/{aid}/knowledge", headers=auth_headers)).json()
    assert docs["total"] == 0


@pytest.mark.asyncio
async def test_delete_knowledge_404_for_unknown_doc(client, auth_headers, test_user):
    aid = await _make_agent(client, auth_headers)
    r = await client.delete(
        f"/api/agents/{aid}/knowledge/{uuid.uuid4()}", headers=auth_headers
    )
    assert r.status_code == 404
