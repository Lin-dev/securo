"""Decision backends for the categorization workflow: the Kev System One
client, backend selection, and the fallback to the model (fork addition, qc9)."""
from __future__ import annotations

import json
import uuid

import httpx
from sqlalchemy import select

from app.agents.config import get_agent_settings
from app.agents.models.usage import LlmUsage
from app.agents.workflows.backends import (
    UNKNOWN,
    BackendUnavailable,
    KevBackend,
    OllamaStructuredBackend,
    category_meaning,
    select_backend,
)
from app.agents.workflows.base import build_context, collect
from app.agents.workflows.categorize import CategorizeWorkflow
from app.agents.workflows.classifiers import CategoryFacts, Merchant
from tests.test_workflow_categorize import CATS, DEFAULT_MAPPING, _ClassifierProvider, _m, _seed

# asyncio_mode=auto covers the coroutine tests; the module mixes sync and async tests.


KEV_ANSWERS = {
    "JETBLUE": ("Travel", {"Travel": 0.91, "Medical & dental": 0.03, UNKNOWN: 0.06}, 0.02),
    "KINGS HWY": ("Medical & dental", {"Medical & dental": 0.88, "Travel": 0.05, UNKNOWN: 0.07}, 0.01),
    "ZELLE": ("Other income", {"Other income": 0.55, "Internal transfer": 0.40, UNKNOWN: 0.05}, 0.45),
}


def _kev_transport(requests: list[dict], *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/systemone"
        body = json.loads(request.content)
        requests.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        state = body["state"]
        for key, (choice, probs, noul) in KEV_ANSWERS.items():
            if key in state:
                break
        else:
            choice, probs, noul = UNKNOWN, {UNKNOWN: 0.7, "Travel": 0.3}, 0.1
        return httpx.Response(200, json={
            "model": body["model"],
            "answers": {
                "category": {"type": "choice", "choice": choice, "confidence": 0.5, "probabilities": probs},
                "own_account_transfer": {"type": "noul", "noul": noul},
            },
            "usage": {"input_tokens": 120, "output_tokens": 9},
        })
    return httpx.MockTransport(handler)


def _failing_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)
    return httpx.MockTransport(handler)


# --- request shape and mapping ----------------------------------------------------------------


def test_build_request_lists_every_category_plus_unknown_and_the_conventions():
    backend = KevBackend("http://kev.test:8009/", model="kev-4b")
    m = _m("JETBLUE AIRWAYS", debits=2, samples=["JETBLUE 123"], accounts=["Visa"], total=600)
    body = backend.build_request(m, CATS, "Airlines are Travel.", currency="USD")
    assert body["model"] == "kev-4b"
    criteria = body["questions"]["category"]["criteria"]
    assert set(criteria) == {c.name for c in CATS} | {UNKNOWN}
    assert criteria["Internal transfer"] == category_meaning(CATS[0]) and "transfer-type" in criteria["Internal transfer"]
    assert body["questions"]["category"]["type"] == "choice"
    assert body["questions"]["own_account_transfer"]["type"] == "noul"
    assert "pattern: JETBLUE AIRWAYS" in body["state"] and '"JETBLUE 123"' in body["state"]
    assert "Household conventions:\nAirlines are Travel." in body["state"]
    assert backend.base_url == "http://kev.test:8009"


async def test_kev_classify_maps_probabilities_to_confidence_and_records_usage(session, test_user, test_agent, test_workspace):
    requests: list[dict] = []
    backend = KevBackend("http://kev.test", transport=_kev_transport(requests))
    ctx = await build_context(session, user=test_user, agent=test_agent, conversation_id=uuid.uuid4(),
                              provider=_ClassifierProvider({}), model="gpt-oss:20b")
    batch = [("m1", _m("JETBLUE AIRWAYS", debits=2)), ("m2", _m("ZELLE FROM JOHN", credits=2)), ("m3", _m("WHO KNOWS", debits=1))]
    out = await backend.classify(ctx, batch, CATS, "", currency="USD")
    assert len(requests) == 3 and [c.merchant_id for c in out] == ["m1", "m2", "m3"]
    jet, zelle, unknown = out
    assert jet.category_name == "Travel" and jet.confidence == 0.91 and jet.probabilities["Travel"] == 0.91
    assert "own-account transfer p=0.02" in jet.reason
    assert zelle.category_name == "Other income" and zelle.confidence == 0.55 and zelle.probabilities["Internal transfer"] == 0.40
    assert unknown.category_name == UNKNOWN and unknown.confidence == 0.7
    rows = (await session.execute(select(LlmUsage).where(LlmUsage.user_id == test_user.id))).scalars().all()
    assert len(rows) == 3 and all(r.provider == "kev" and r.kind == "workflow" and r.input_tokens == 120 for r in rows)
    assert ctx.budget.llm_calls == 0  # Kev calls do not consume the model-call budget


async def test_kev_connection_error_raises_backend_unavailable(session, test_user, test_agent, test_workspace):
    backend = KevBackend("http://kev.test", transport=_failing_transport())
    ctx = await build_context(session, user=test_user, agent=test_agent, conversation_id=uuid.uuid4(),
                              provider=_ClassifierProvider({}), model="gpt-oss:20b")
    try:
        await backend.classify(ctx, [("m1", _m("JETBLUE", debits=1))], CATS, "", currency="USD")
    except BackendUnavailable as exc:
        assert "unreachable" in str(exc)
    else:
        raise AssertionError("expected BackendUnavailable")


async def test_kev_server_error_raises_backend_unavailable(session, test_user, test_agent, test_workspace):
    backend = KevBackend("http://kev.test", transport=_kev_transport([], status=503))
    ctx = await build_context(session, user=test_user, agent=test_agent, conversation_id=uuid.uuid4(),
                              provider=_ClassifierProvider({}), model="gpt-oss:20b")
    try:
        await backend.classify(ctx, [("m1", _m("JETBLUE", debits=1))], CATS, "", currency="USD")
    except BackendUnavailable:
        pass
    else:
        raise AssertionError("expected BackendUnavailable")


# --- selection -----------------------------------------------------------------------------------


def test_select_backend_defaults_to_the_model(monkeypatch):
    s = get_agent_settings()
    monkeypatch.setattr(s, "classifier_backend", "ollama")
    monkeypatch.setattr(s, "kev_base_url", "")
    assert isinstance(select_backend(s), OllamaStructuredBackend)
    monkeypatch.setattr(s, "classifier_backend", "kev")          # kev without a URL → still the model
    assert isinstance(select_backend(s), OllamaStructuredBackend)


def test_select_backend_picks_kev_when_configured(monkeypatch):
    s = get_agent_settings()
    monkeypatch.setattr(s, "classifier_backend", "kev")
    monkeypatch.setattr(s, "kev_base_url", "http://192.168.86.22:8009")
    monkeypatch.setattr(s, "kev_model", "kev-latest")
    monkeypatch.setattr(s, "kev_timeout_seconds", 12.5)
    backend = select_backend(s)
    assert isinstance(backend, KevBackend)
    assert backend.base_url == "http://192.168.86.22:8009" and backend.model == "kev-latest" and backend.timeout == 12.5


def test_settings_defaults():
    s = get_agent_settings()
    assert s.classifier_backend == "ollama" and s.kev_base_url == "" and s.kev_model == "kev-latest"
    assert s.kev_timeout_seconds == 20.0


# --- end to end through the workflow ----------------------------------------------------------------


async def test_categorize_with_kev_backend_never_calls_the_model(session, test_user, test_workspace, test_agent):
    await _seed(session, test_user.id, test_workspace.id)
    requests: list[dict] = []
    provider = _ClassifierProvider(DEFAULT_MAPPING)
    ctx = await build_context(session, user=test_user, agent=test_agent, conversation_id=uuid.uuid4(),
                              provider=provider, model="gpt-oss:20b")
    ctx.language = "en"
    wf = CategorizeWorkflow(backend=KevBackend("http://kev.test", transport=_kev_transport(requests)))
    events, result = await collect(wf, ctx, {})
    assert provider.calls == []
    assert len(requests) == 3                                   # JETBLUE, KINGS HWY, ZELLE — never ACH/PAYMENT/NETFLIX
    assert all("ACH DEPOSIT" not in r["state"] and "NETFLIX" not in r["state"] for r in requests)
    patterns = {p.arguments["match_pattern"] for p in ctx.pending_proposals}
    assert patterns == {"ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN", "PAYMENT - THANK YOU", "JETBLUE AIRWAYS", "KINGS HWY ORAL&MAX DDS"}
    # ZELLE: Kev says Other income at 0.55 → below the review floor's 0.6? No: 0.55 < 0.6 → unknown, listed with its reason
    assert "ZELLE FROM JOHN" in result.summary and "kev p=0.55" in result.summary
    assert result.data["proposals"] == 4


async def test_categorize_falls_back_to_the_model_when_kev_is_down(session, test_user, test_workspace, test_agent):
    await _seed(session, test_user.id, test_workspace.id)
    provider = _ClassifierProvider(DEFAULT_MAPPING)
    ctx = await build_context(session, user=test_user, agent=test_agent, conversation_id=uuid.uuid4(),
                              provider=provider, model="gpt-oss:20b")
    ctx.language = "en"
    wf = CategorizeWorkflow(backend=KevBackend("http://kev.test", transport=_failing_transport()))
    events, result = await collect(wf, ctx, {})
    assert len(provider.calls) == 1                             # the model answered the batch instead
    patterns = {p.arguments["match_pattern"] for p in ctx.pending_proposals}
    assert {"JETBLUE AIRWAYS", "KINGS HWY ORAL&MAX DDS"} <= patterns
    assert "kev classifier was unreachable" in result.summary
    assert result.data["proposals"] == 4


def test_category_meaning_mentions_group_and_transfer_flag():
    assert category_meaning(CategoryFacts("a", "Travel", "Lifestyle", False)) == "group: Lifestyle"
    assert category_meaning(CategoryFacts("a", "Card payment", None, True)).startswith("group: none; transfer-type")
    m = Merchant(merchant="X", pattern="X", count=1, total=1.0)
    assert m.average == 1.0
