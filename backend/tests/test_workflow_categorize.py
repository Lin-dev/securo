"""Categorization review workflow: deterministic classifiers, the closed model
question, validation, proposals and the summary (fork addition, qc9)."""
from __future__ import annotations

import json
import re
import uuid
from typing import AsyncIterator

from sqlalchemy import select

import mcp_server.tools  # noqa: F401  (registers the tool handlers)
from app.agents.providers.base import ChatChunk, Usage
from app.agents.workflows import registry
from app.agents.workflows.base import WorkflowBudget, build_context, collect
from app.agents.workflows.categorize import (
    PROPOSAL_TOOL,
    UNKNOWN,
    CategorizeWorkflow,
    RunStats,
    batch_schema,
    render_batch,
    render_summary,
)
from app.agents.workflows.classifiers import (
    AccountFacts,
    CategoryFacts,
    Decision,
    Merchant,
    apply_thresholds,
    cap_proposals,
    classify_card_payment,
    classify_own_account_transfer,
    covered_by_rule,
    dedupe_by_pattern,
    is_income_like,
    validate_sign,
)
from app.models.category import Category
from app.models.category_group import CategoryGroup
from app.models.rule import Rule
from app.models.transaction import Transaction
from tests.test_agents_workflows import _StructuredProvider
from tests.test_mcp_finance_tools import _account, _category, _txn

# asyncio_mode=auto covers the coroutine tests; the module mixes sync and async tests.


# --- fixtures and fakes -------------------------------------------------------------------


def _m(pattern: str, *, debits=0, credits=0, samples=None, accounts=None, count=None, total=100.0) -> Merchant:
    return Merchant(
        merchant=pattern, pattern=pattern, count=count or (debits + credits) or 1, total=total,
        type_mix={"debit": debits, "credit": credits}, accounts=list(accounts or ["Checking"]),
        sample_descriptions=list(samples or [pattern]), sample_transaction_ids=[],
    )


CATS = [
    CategoryFacts("c-int", "Internal transfer", "Transfers & internal", True),
    CategoryFacts("c-card", "Card payment", "Transfers & internal", True),
    CategoryFacts("c-travel", "Travel", "Lifestyle", False),
    CategoryFacts("c-dental", "Medical & dental", "Lifestyle", False),
    CategoryFacts("c-income", "Other income", "Income", False),
    CategoryFacts("c-refund", "Refunds & credits", "Income", False),
]
ACCOUNTS = [
    AccountFacts("Checking", "checking", None),
    AccountFacts("Savings", "savings", "3154"),
    AccountFacts("Visa", "credit_card", None),
    AccountFacts("TD SIMPLE SAVINGS (9846)", "savings", None),
]


class _ClassifierProvider(_StructuredProvider):
    """Answers a batch from a pattern → (category, confidence) mapping by reading
    the ids out of the user message, so the test does not depend on ordering."""

    def __init__(self, mapping: dict[str, tuple[str, float]]):
        super().__init__([])
        self.mapping = mapping

    async def chat_stream(  # type: ignore[override]
        self, messages, *, model, tools=None, temperature=0.4, max_tokens=None,
        response_format=None, reasoning=None,
    ) -> AsyncIterator[ChatChunk]:
        self.calls.append({
            "messages": list(messages), "model": model, "tools": tools, "temperature": temperature,
            "max_tokens": max_tokens, "response_format": response_format, "reasoning": reasoning,
        })
        user_text = messages[-1].content or ""
        items = []
        for mid, pattern in re.findall(r"^(m\d+) \| pattern: (.+?) \|", user_text, flags=re.M):
            for key, (category, confidence) in self.mapping.items():
                if pattern.startswith(key):
                    items.append({"id": mid, "category": category, "confidence": confidence, "reason": f"test: {key}"})
                    break
            else:
                items.append({"id": mid, "category": UNKNOWN, "confidence": 0.3, "reason": "test: unmapped"})
        yield ChatChunk(type="text_delta", text=json.dumps({"items": items}))
        yield ChatChunk(type="usage", usage=Usage(input_tokens=50, output_tokens=20))
        yield ChatChunk(type="finish", finish_reason="stop")


async def _seed(session, uid, wid, *, netflix_rule: bool = True):
    income = CategoryGroup(id=uuid.uuid4(), user_id=uid, workspace_id=wid, name="Income", icon="trending-up", color="#16A34A", position=5)
    session.add(income)
    await session.flush()
    cats = {
        "Internal transfer": await _category(session, uid, wid, "Internal transfer", transfer=True),
        "Card payment": await _category(session, uid, wid, "Card payment", transfer=True),
        "Travel": await _category(session, uid, wid, "Travel", transfer=False),
        "Medical & dental": await _category(session, uid, wid, "Medical & dental", transfer=False),
        "Streaming": await _category(session, uid, wid, "Streaming", transfer=False),
    }
    other_income = Category(id=uuid.uuid4(), user_id=uid, workspace_id=wid, name="Other income", icon="x", color="#000000", group_id=income.id)
    session.add(other_income)
    cats["Other income"] = other_income
    checking = await _account(session, uid, wid, "Checking", "checking")
    savings = await _account(session, uid, wid, "Savings", "savings", masked_number="3154")
    visa = await _account(session, uid, wid, "Visa", "credit_card")
    rows = []
    for _ in range(4):
        rows.append(_txn(uid, wid, checking, 500, "credit", description="ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 3154"))
    rows.append(_txn(uid, wid, visa, 1000, "credit", description="PAYMENT - THANK YOU"))
    for _ in range(2):
        rows.append(_txn(uid, wid, visa, 300, "debit", description="JETBLUE AIRWAYS 27920312345"))
    for _ in range(2):
        rows.append(_txn(uid, wid, visa, 200, "debit", description="KINGS HWY ORAL&MAX DDS"))
    for _ in range(2):
        rows.append(_txn(uid, wid, checking, 100, "credit", description="ZELLE FROM JOHN"))
    for _ in range(2):
        rows.append(_txn(uid, wid, visa, 15, "debit", description="NETFLIX.COM"))
    session.add_all(rows)
    if netflix_rule:
        session.add(Rule(
            id=uuid.uuid4(), user_id=uid, workspace_id=wid, name="Streaming (Netflix)", conditions_op="and",
            conditions=[{"field": "description", "op": "contains", "value": "NETFLIX"}],
            actions=[{"op": "set_category", "value": str(cats["Streaming"].id)}], priority=30, is_active=True,
        ))
    await session.commit()
    return cats, {"checking": checking, "savings": savings, "visa": visa}


DEFAULT_MAPPING = {"JETBLUE": ("Travel", 0.93), "KINGS HWY": ("Medical & dental", 0.90), "ZELLE": ("Travel", 0.90)}


async def _run(session, test_user, test_agent, mapping=None, params=None, budget=None, language="en"):
    provider = _ClassifierProvider(mapping or DEFAULT_MAPPING)
    ctx = await build_context(session, user=test_user, agent=test_agent, conversation_id=uuid.uuid4(),
                              provider=provider, model="gpt-oss:20b")
    ctx.language = language  # the conftest user prefers pt-BR; the assertions below read English
    if budget is not None:
        ctx.budget = budget
    events, result = await collect(CategorizeWorkflow(), ctx, params or {})
    return provider, ctx, events, result


# --- classifiers (pure) -------------------------------------------------------------------


def test_own_account_transfer_matches_masked_number():
    m = _m("ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN", credits=4,
           samples=["ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 3154"], accounts=["Checking"])
    d = classify_own_account_transfer(m, ACCOUNTS, CATS)
    assert d is not None and d.status == "propose" and d.source == "transfer"
    assert d.category is not None and d.category.name == "Internal transfer"
    assert d.confidence == 0.98 and "3154" in d.reason and "Savings" in d.reason


def test_own_account_transfer_reads_last4_from_account_name():
    m = _m("TRANSFER TO ACCT", debits=1, samples=["ONLINE TRANSFER TO ACCT #9846"])
    d = classify_own_account_transfer(m, ACCOUNTS, CATS)
    assert d is not None and d.status == "propose" and "9846" in d.reason


def test_own_account_transfer_unknown_digits_goes_to_review():
    m = _m("ACH DEPOSIT", credits=1, samples=["ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 7777"])
    d = classify_own_account_transfer(m, ACCOUNTS, CATS)
    assert d is not None and d.status == "review" and d.confidence == 0.6 and "7777" in d.reason


def test_transfer_words_without_digits_is_review():
    m = _m("ONLINE TRANSFER TO SAVINGS", debits=1)
    d = classify_own_account_transfer(m, ACCOUNTS, CATS)
    assert d is not None and d.status == "review" and d.confidence == 0.7


def test_missing_transfer_category_adds_note():
    m = _m("ACH", credits=1, samples=["ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 3154"])
    d = classify_own_account_transfer(m, ACCOUNTS, [c for c in CATS if not c.treat_as_transfer])
    assert d is not None and d.status == "review" and d.category is None and "Internal transfer" in d.reason


def test_plain_merchant_is_not_a_transfer():
    assert classify_own_account_transfer(_m("JETBLUE AIRWAYS", debits=2), ACCOUNTS, CATS) is None


def test_card_payment_requires_credit_card_account_or_card_word():
    on_card = _m("PAYMENT - THANK YOU", credits=1, accounts=["Visa"])
    d = classify_card_payment(on_card, ACCOUNTS, CATS)
    assert d is not None and d.status == "propose" and d.category.name == "Card payment" and d.confidence == 0.95
    on_checking = _m("PAYMENT - THANK YOU", debits=1, accounts=["Checking"])
    assert classify_card_payment(on_checking, ACCOUNTS, CATS) is None
    with_card_word = _m("AMEX AUTOPAY PAYMENT", debits=1, accounts=["Checking"], samples=["AMEX AUTOPAY PAYMENT 1234"])
    assert classify_card_payment(with_card_word, ACCOUNTS, CATS) is not None
    assert classify_card_payment(_m("JETBLUE", debits=1, accounts=["Visa"]), ACCOUNTS, CATS) is None


def test_covered_by_rule_uses_engine_semantics():
    rules = [
        {"name": "Uber", "is_active": True, "conditions_op": "or",
         "conditions": [{"field": "description", "op": "starts_with", "value": "UBER"}],
         "actions": [{"op": "set_category", "value": "x"}]},
        {"name": "Big amounts", "is_active": True, "conditions_op": "and",
         "conditions": [{"field": "description", "op": "contains", "value": "NETFLIX"}, {"field": "amount", "op": "gt", "value": 1}],
         "actions": [{"op": "set_category", "value": "x"}]},
        {"name": "Inactive netflix", "is_active": False, "conditions_op": "and",
         "conditions": [{"field": "description", "op": "contains", "value": "NETFLIX"}],
         "actions": [{"op": "set_category", "value": "x"}]},
        {"name": "Grouped", "is_active": True, "conditions_op": "and",
         "conditions": [{"op": "or", "conditions": [{"field": "description", "op": "contains", "value": "SPOTIFY"}, {"field": "description", "op": "regex", "value": "NETFL[IY]X"}]}],
         "actions": [{"op": "set_category", "value": "x"}]},
    ]
    assert covered_by_rule(_m("UBER TRIP", samples=["Uber *Trip 123"]), rules) == "Uber"
    assert covered_by_rule(_m("NETFLIX.COM", samples=["NETFLIX.COM"]), rules) == "Grouped"
    assert covered_by_rule(_m("NETFLIX.COM", samples=["NETFLIX.COM"]), rules[:3]) is None  # amount rule ignored, inactive ignored
    assert covered_by_rule(_m("JETBLUE", samples=["JETBLUE 123"]), rules) is None


def test_validate_sign_credit_only_rejects_expense_category():
    credit_only = _m("ZELLE FROM JOHN", credits=2)
    assert validate_sign(credit_only, CATS[2]) is not None          # Travel
    assert validate_sign(credit_only, CATS[4]) is None              # Other income
    assert validate_sign(credit_only, CATS[0]) is None              # Internal transfer


def test_validate_sign_debit_only_rejects_income_unless_refund():
    debit_only = _m("JETBLUE", debits=2)
    assert validate_sign(debit_only, CATS[4]) is not None           # Other income
    assert validate_sign(debit_only, CATS[5]) is None               # Refunds & credits
    assert validate_sign(debit_only, CATS[2]) is None               # Travel
    mixed = _m("AMAZON", debits=3, credits=1)
    assert validate_sign(mixed, CATS[4]) is None


def test_income_group_detected_across_locales_and_names():
    assert is_income_like(CategoryFacts("a", "Whatever", "Renda", False))
    assert is_income_like(CategoryFacts("a", "Whatever", "Ingresos", False))
    assert is_income_like(CategoryFacts("a", "Salary & wages", None, False))
    assert not is_income_like(CategoryFacts("a", "Groceries", "Food & Dining", False))


def test_thresholds_dedupe_and_cap():
    assert apply_thresholds(0.9) == "propose" and apply_thresholds(0.7) == "review" and apply_thresholds(0.2) == "unknown"
    a = Decision(_m("AMAZON", debits=5, count=5, total=500), CATS[2], 0.9, "llm", "propose", "r")
    b = Decision(_m("AMAZON", debits=2, count=2, total=50), CATS[2], 0.9, "llm", "propose", "r")
    short = Decision(_m("AB", debits=1), CATS[2], 0.9, "llm", "propose", "r")
    blank = Decision(_m("(blank)", debits=1), CATS[2], 0.9, "llm", "propose", "r")
    out, notes = dedupe_by_pattern([b, a, short, blank])
    assert [d.status for d in out] == ["skipped", "propose", "skipped", "skipped"]
    assert notes and "merged" in notes[0]
    many = [Decision(_m(f"M{i:02d}X", debits=1, total=float(i)), CATS[2], 0.9, "llm", "propose", "r") for i in range(30)]
    keep, overflow = cap_proposals(many, limit=25)
    assert len(keep) == 25 and len(overflow) == 5
    assert keep[0].merchant.total == 29.0 and all(d.status == "review" for d in overflow)


def test_batch_schema_patches_both_enums_and_is_strict():
    schema = batch_schema(["m1", "m2"], ["Travel", "Other income"])
    item = schema["properties"]["items"]["items"]
    assert item["properties"]["id"]["enum"] == ["m1", "m2"]
    assert item["properties"]["category"]["enum"] == ["Travel", "Other income", UNKNOWN]
    assert item["additionalProperties"] is False and schema["additionalProperties"] is False
    assert set(item["required"]) == {"id", "category", "confidence", "reason"}
    json.dumps(schema)


def test_render_batch_lists_ids_patterns_and_samples():
    text = render_batch([("m1", _m("JETBLUE AIRWAYS", debits=2, samples=["JETBLUE 123", "JETBLUE 456"], accounts=["Visa"], total=600))], "USD")
    assert "m1 | pattern: JETBLUE AIRWAYS | debits: 2, credits: 0 | avg: 300.00, total: 600.00 | accounts: Visa" in text
    assert 'samples: "JETBLUE 123"; "JETBLUE 456"' in text and "(m1..m1)" in text


def test_render_summary_en_and_pt_br_contain_counts_tables_and_next_step():
    stats = RunStats(rows=12, merchants=6, deterministic=2, covered=1, llm_count=3, batches=1, period_label="all time")
    proposals = [{"pattern": "JETBLUE AIRWAYS", "category": "Travel", "confidence": 0.93, "reason": "airline", "rows": 2}]
    review = [Decision(_m("ZELLE FROM JOHN", credits=2), CATS[2], 0.6, "llm", "review", "credit-only merchant cannot be an expense category")]
    skipped = [Decision(_m("NETFLIX.COM", debits=2), None, 1.0, "rule", "covered", "already covered by rule **Streaming**", covered_by="Streaming")]
    en = render_summary("en", stats, [], proposals, review, skipped)
    assert "## Categorization review — all time" in en and "**12** across **6** merchants" in en
    assert "### Proposals (1)" in en and "| `JETBLUE AIRWAYS` | Travel | 0.93 — airline | 2 |" in en
    assert "### Needs your call (1)" in en and "ZELLE FROM JOHN" in en and "credits only" in en
    assert "### Skipped (1)" in en and "Streaming" in en and "**Next step:**" in en and "Apply all rules" in en
    pt = render_summary("pt-BR", stats, [{"name": "Auto-categorize: X", "category": "Travel", "confidence": 0.95, "rows": 3}], [], [], [])
    assert "Revisão de categorização" in pt and "### Aplicadas (1)" in pt and "Nenhuma proposta" in pt and "Próximo passo" in pt
    assert render_summary("en", RunStats(period_label="all time"), [], [], [], [], nothing=True).endswith("already has a category.")


# --- end to end ---------------------------------------------------------------------------------


async def test_categorize_end_to_end_emits_cards_and_summary(session, test_user, test_workspace, test_agent):
    cats, _ = await _seed(session, test_user.id, test_workspace.id)
    provider, ctx, events, result = await _run(session, test_user, test_agent)

    # the model saw only what code could not decide
    assert len(provider.calls) == 1
    call = provider.calls[0]
    user_text = call["messages"][-1].content
    assert "JETBLUE AIRWAYS" in user_text and "KINGS HWY" in user_text and "ZELLE" in user_text
    assert "ACH DEPOSIT" not in user_text and "PAYMENT - THANK YOU" not in user_text and "NETFLIX" not in user_text
    assert call["tools"] is None and call["reasoning"] == "low" and call["temperature"] == 0.0
    enum = call["response_format"]["properties"]["items"]["items"]["properties"]["category"]["enum"]
    assert set(enum) == set(cats.keys()) | {UNKNOWN}
    assert call["response_format"]["properties"]["items"]["items"]["properties"]["id"]["enum"] == ["m1", "m2", "m3"]
    assert "Internal transfer — group: none; transfer-type" in call["messages"][0].content
    assert "Other income — group: Income" in call["messages"][0].content

    # proposals: transfer + card payment decided in code, two from the model
    card_events = [e for e in events if e.tool_name == PROPOSAL_TOOL]
    assert [e.type for e in card_events] == ["tool_call"] * 4 + ["tool_result"] * 4
    by_pattern = {p.arguments["match_pattern"]: p for p in ctx.pending_proposals}
    assert set(by_pattern) == {"ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN", "PAYMENT - THANK YOU", "JETBLUE AIRWAYS", "KINGS HWY ORAL&MAX DDS"}
    ach = by_pattern["ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN"]
    assert ach.result["kind"] == "create_payee_rule"
    assert ach.result["proposed"]["category_name"] == "Internal transfer"
    assert ach.result["proposed"]["would_categorize_count"] == 4
    assert ach.arguments["priority"] == 30 and ach.arguments["name"].startswith("Auto-categorize: ACH DEPOSIT")
    assert by_pattern["PAYMENT - THANK YOU"].result["proposed"]["category_name"] == "Card payment"
    assert by_pattern["JETBLUE AIRWAYS"].result["proposed"]["category_name"] == "Travel"
    assert all(p.tool_call_id.startswith("wf_") for p in ctx.pending_proposals)
    step_names = [e.tool_name for e in events if e.type == "tool_call" and e.tool_name.startswith("workflow.")]
    assert step_names == ["workflow.load", "workflow.classify"]

    # summary and compact result
    assert result.final is True and result.ok is True
    assert "## Categorization review — all time" in result.summary
    assert "### Proposals (4)" in result.summary and "3154" in result.summary
    assert "### Needs your call (1)" in result.summary and "ZELLE FROM JOHN" in result.summary
    assert "credit-only merchant cannot be an expense category" in result.summary
    assert "Streaming (Netflix)" in result.summary and "**Next step:**" in result.summary
    assert result.data["proposals"] == 4 and result.data["applied"] == 0 and result.data["needs_review"] == 1
    assert result.data["classified_in_code"] == 2 and result.data["covered_by_rules"] == 1 and result.data["sent_to_model"] == 3
    assert result.data["uncategorized_rows"] == 13
    # previews only: nothing was written
    rules = (await session.execute(select(Rule).where(Rule.workspace_id == test_workspace.id))).scalars().all()
    assert [r.name for r in rules] == ["Streaming (Netflix)"]


async def test_categorize_apply_is_gated_by_setting_and_agent_extra(session, test_user, test_workspace, test_agent):
    cats, accounts = await _seed(session, test_user.id, test_workspace.id)
    provider, ctx, events, result = await _run(session, test_user, test_agent, params={"apply": True})
    rules = (await session.execute(select(Rule).where(Rule.workspace_id == test_workspace.id))).scalars().all()
    assert len(rules) == 1 and len(ctx.pending_proposals) == 4 and result.data["applied"] == 0

    test_agent.extra = {"auto_apply_rules": True}
    await session.commit()
    provider, ctx, events, result = await _run(session, test_user, test_agent, params={"apply": True})
    rules = (await session.execute(select(Rule).where(Rule.workspace_id == test_workspace.id))).scalars().all()
    names = {r.name for r in rules}
    assert {"Auto-categorize: ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN", "Auto-categorize: PAYMENT - THANK YOU",
            "Auto-categorize: JETBLUE AIRWAYS", "Auto-categorize: KINGS HWY ORAL&MAX DDS"} <= names
    assert ctx.pending_proposals == []
    assert result.data["applied"] == 4 and result.data["proposals"] == 0
    assert "### Applied (4)" in result.summary
    jetblue_rows = (await session.execute(
        select(Transaction).where(Transaction.workspace_id == test_workspace.id, Transaction.description.like("JETBLUE%"))
    )).scalars().all()
    assert all(r.category_id == cats["Travel"].id for r in jetblue_rows)


async def test_categorize_sign_violation_demotes_to_review_even_when_confident(session, test_user, test_workspace, test_agent):
    await _seed(session, test_user.id, test_workspace.id)
    mapping = {**DEFAULT_MAPPING, "ZELLE": ("Travel", 0.99)}
    provider, ctx, events, result = await _run(session, test_user, test_agent, mapping=mapping)
    review = [d for d in [] ]  # noqa: F841 — the decision list is internal; assert through the summary and cards
    assert "ZELLE FROM JOHN" not in {p.arguments["match_pattern"] for p in ctx.pending_proposals}
    assert "credit-only merchant cannot be an expense category (model suggested Travel)" in result.summary


async def test_categorize_unknown_and_unmapped_categories_are_not_proposed(session, test_user, test_workspace, test_agent):
    await _seed(session, test_user.id, test_workspace.id)
    mapping = {"JETBLUE": ("Nonexistent category", 0.99), "KINGS HWY": (UNKNOWN, 0.4), "ZELLE": ("Other income", 0.7)}
    provider, ctx, events, result = await _run(session, test_user, test_agent, mapping=mapping)
    patterns = {p.arguments["match_pattern"] for p in ctx.pending_proposals}
    assert patterns == {"ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN", "PAYMENT - THANK YOU"}
    assert "unknown category 'Nonexistent category'" in result.summary
    assert "best guess **Other income** (0.70)" in result.summary        # 0.6–0.85 → needs your call
    assert result.data["needs_review"] == 1


async def test_categorize_budget_exhaustion_leaves_review_notes(session, test_user, test_workspace, test_agent):
    await _seed(session, test_user.id, test_workspace.id)
    provider, ctx, events, result = await _run(session, test_user, test_agent, budget=WorkflowBudget(max_llm_calls=0))
    assert provider.calls == []
    patterns = {p.arguments["match_pattern"] for p in ctx.pending_proposals}
    assert patterns == {"ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN", "PAYMENT - THANK YOU"}
    assert result.data["needs_review"] == 3 and "run budget reached" in result.summary
    assert result.data["sent_to_model"] == 3 and "batch(es)" in result.summary


async def test_categorize_name_collision_gets_a_suffix(session, test_user, test_workspace, test_agent):
    cats, _ = await _seed(session, test_user.id, test_workspace.id)
    session.add(Rule(
        id=uuid.uuid4(), user_id=test_user.id, workspace_id=test_workspace.id, name="Auto-categorize: JETBLUE AIRWAYS",
        conditions_op="and", conditions=[{"field": "description", "op": "contains", "value": "SOMETHING ELSE"}],
        actions=[{"op": "set_category", "value": str(cats["Travel"].id)}], priority=30, is_active=True,
    ))
    await session.commit()
    provider, ctx, events, result = await _run(session, test_user, test_agent)
    jet = next(p for p in ctx.pending_proposals if p.arguments["match_pattern"] == "JETBLUE AIRWAYS")
    assert jet.arguments["name"] == "Auto-categorize: JETBLUE AIRWAYS (2)"
    assert jet.result["proposed"]["name_collision"] is False
    ach = next(p for p in ctx.pending_proposals if p.arguments["match_pattern"].startswith("ACH"))
    assert ach.arguments["name"] == "Auto-categorize: ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN"


async def test_categorize_with_nothing_to_do_finishes_early(session, test_user, test_workspace, test_agent):
    provider, ctx, events, result = await _run(session, test_user, test_agent)
    assert provider.calls == [] and ctx.pending_proposals == []
    assert result.data["merchants"] == 0 and "Nothing to categorize" in result.summary
    assert [e.tool_name for e in events] == ["workflow.load", "workflow.load"]


async def test_categorize_summary_follows_the_context_language(session, test_user, test_workspace, test_agent):
    await _seed(session, test_user.id, test_workspace.id)
    provider, ctx, events, result = await _run(session, test_user, test_agent, language="pt-BR")
    assert "## Revisão de categorização" in result.summary and "### Propostas (4)" in result.summary


async def test_categorize_respects_period_and_limit_params(session, test_user, test_workspace, test_agent):
    await _seed(session, test_user.id, test_workspace.id)
    provider, ctx, events, result = await _run(session, test_user, test_agent, params={"from_date": "2099-01-01", "limit": 5})
    assert result.data["merchants"] == 0 and "2099-01-01" in result.summary


def test_workflow_is_registered_as_builtin_with_alias_and_tool():
    registry.load_builtin()
    wf = registry.get("categorize")
    assert wf is not None and registry.get("review") is wf
    defs = registry.as_tool_definitions(None)
    assert any(d.name == "workflow__categorize" for d in defs)
    tool = next(d for d in defs if d.name == "workflow__categorize")
    assert set(tool.parameters["properties"]) == {"from_date", "to_date", "limit", "apply"}
    assert "Call it once" in tool.description
    wf2, params = registry.parse_slash_command("/review limit=20 from=2026-08-01")
    assert wf2 is wf and params == {"limit": 20, "from_date": "2026-08-01"}
