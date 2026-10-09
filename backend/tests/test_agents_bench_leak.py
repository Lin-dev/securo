"""The router prompt must not contain the benchmark's questions.

scripts/agent_bench.py measures the guided router on frozen (v1) and held-out (v2)
questions. A few-shot example that copies one of them would teach the router the
answer key, so no run of six words from any benchmark question may appear in the
router's system prompt.
"""
import ast
import re
from datetime import date
from pathlib import Path

from app.agents.runtime.guided import router_system

BENCH = Path(__file__).resolve().parents[1] / "scripts" / "agent_bench.py"
SPAN = 6


def _merchant_b(tree: ast.Module) -> str:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "MERCHANT_B":
            value = node.value
            return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else ""
    return ""


def _questions() -> list[str]:
    tree = ast.parse(BENCH.read_text())
    merchant_b = _merchant_b(tree)
    out = []
    for node in ast.walk(tree):
        text = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(str(v.value) if isinstance(v, ast.Constant) else merchant_b for v in node.values)
        if text and text.strip().endswith("?") and len(text.split()) >= 5:
            out.append(text)
    return out


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("'", "")).split()


def _spans(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + SPAN]) for i in range(len(words) - SPAN + 1)}


def test_bench_questions_are_found():
    questions = _questions()
    assert len(questions) >= 30
    assert "How much did I pay Uber in June 2026?" in questions
    assert any(q.startswith("How much did I spend at ") and "between January" in q for q in questions)


def test_router_prompt_shares_no_six_word_span_with_any_bench_question():
    prompt_spans = _spans(_words(router_system(today=date(2026, 10, 9), tz="UTC", language="en", prior_user_message=None)))
    leaks = {q: sorted(" ".join(s) for s in _spans(_words(q)) & prompt_spans) for q in _questions()}
    assert {q: s for q, s in leaks.items() if s} == {}
