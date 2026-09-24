"""Categorization review: the model classifies, the code does everything else.

Order of work for one run:
1. Load in code (uncategorized merchants, categories, rules, accounts). None
   of this is shown to the model.
2. Decide deterministically what code can decide: transfers between the
   user's own accounts, payments to their own credit cards, merchants an
   existing rule already covers.
3. Ask the model one closed question per batch of merchants: which of these
   categories, with what confidence. The reply is grammar-constrained and
   validated (known category, money direction, thresholds).
4. Build one `propose_create_payee_rule` preview per confident merchant and
   emit it as a card; write a deterministic summary; stop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agents.workflows import registry
from app.agents.workflows.base import WorkflowBudgetExceeded, WorkflowContext, WorkflowStepError
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
    find_category_by_name,
    validate_sign,
)
from app.models.account import Account
from app.models.category_group import CategoryGroup
from app.schemas.rule import RuleAction, RuleCondition, RuleCreate
from app.services import rule_service

logger = logging.getLogger(__name__)

BATCH_SIZE = 12
MAX_PROPOSALS = 25
AUTO_APPLY_MIN_CONFIDENCE = 0.90
CONVENTIONS_MAX_CHARS = 3000
UNKNOWN = "Unknown"
PROPOSAL_TOOL = "securo__propose_create_payee_rule"


# --- the closed question --------------------------------------------------------------


class ClassifiedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: str
    confidence: float = Field(ge=0, le=1)
    reason: str = ""


class ClassificationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ClassifiedItem]


@dataclass
class Classified:
    merchant_id: str
    category_name: str
    confidence: float
    reason: str = ""
    probabilities: Optional[dict[str, float]] = None


def batch_schema(ids: list[str], category_names: list[str]) -> dict[str, Any]:
    """Strict JSON schema with both enums patched in; the object shape mirrors
    `ClassificationBatch`, which validates the parsed reply."""
    return {
        "title": "classification_batch",
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": list(ids)},
                        "category": {"type": "string", "enum": list(category_names) + [UNKNOWN]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "category", "confidence", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


CLASSIFY_SYSTEM = """\
You classify bank-transaction merchants into the user's existing categories for Securo, a personal-finance app.

Categories (use the exact name; nothing else is allowed):
{categories}
- {unknown} — use this whenever you are not reasonably sure.

Household conventions (authoritative; they override your general knowledge):
{conventions}

Rules:
1. Pick exactly one category per merchant from the list above, or {unknown}.
2. A merchant that only ever shows credits (money in) can only be an income category or a transfer-type category. A merchant that only shows debits (money out) is never income unless it is a refund category.
3. Payments to the user's own credit card, transfers between the user's own accounts, and contributions to investment or retirement accounts are transfer-type categories, not income or expenses.
4. confidence is your probability that the category is right: 0.95 only for unmistakable merchants (airlines, supermarkets, pharmacies, streaming brands), 0.7-0.85 when the name is suggestive but ambiguous, below 0.6 when you are guessing. When in doubt prefer {unknown} over a wrong category.
5. reason: at most 120 characters, name the clue you used (brand, keyword, convention).
6. Reply with JSON only, matching the schema; no prose, no markdown."""


def render_categories(cats: list[CategoryFacts]) -> str:
    lines = []
    for cat in cats:
        flag = (
            "; transfer-type (money moved between the user's own accounts or into investments; never income or expense)"
            if cat.treat_as_transfer
            else ""
        )
        lines.append(f"- {cat.name} — group: {cat.group_name or 'none'}{flag}")
    return "\n".join(lines)


def render_batch(batch: list[tuple[str, Merchant]], currency: str) -> str:
    lines = [f"Classify these {len(batch)} merchants. Amounts are in {currency}.", ""]
    for mid, m in batch:
        accounts = ", ".join(m.accounts) or "unknown account"
        lines.append(
            f"{mid} | pattern: {m.pattern} | debits: {m.debits}, credits: {m.credits} "
            f"| avg: {m.average:.2f}, total: {m.total:.2f} | accounts: {accounts}"
        )
        samples = "; ".join(f'"{d}"' for d in m.sample_descriptions[:2]) or f'"{m.merchant}"'
        lines.append(f"     samples: {samples}")
    lines.append("")
    lines.append(f"Return one item per id ({batch[0][0]}..{batch[-1][0]}).")
    return "\n".join(lines)


class OllamaStructuredBackend:
    """Default classifier: one schema-constrained model call per batch."""

    name = "ollama"

    async def classify(
        self,
        ctx: WorkflowContext,
        batch: list[tuple[str, Merchant]],
        categories: list[CategoryFacts],
        conventions: str,
        *,
        currency: str,
    ) -> list[Classified]:
        ids = [mid for mid, _ in batch]
        names = [c.name for c in categories]
        system = CLASSIFY_SYSTEM.format(
            categories=render_categories(categories),
            conventions=conventions.strip() or "none provided",
            unknown=UNKNOWN,
        )
        result = await ctx.llm_structured(
            system,
            render_batch(batch, currency),
            ClassificationBatch,
            reasoning="low",
            temperature=0.0,
            max_tokens=2500,
            retries=1,
            json_schema=batch_schema(ids, names),
        )
        return [
            Classified(merchant_id=item.id, category_name=item.category, confidence=float(item.confidence), reason=item.reason[:120])
            for item in result.items
        ]


# --- the workflow ---------------------------------------------------------------------


@dataclass
class RunStats:
    rows: int = 0
    merchants: int = 0
    truncated: bool = False
    deterministic: int = 0
    covered: int = 0
    llm_count: int = 0
    batches: int = 0
    period_label: str = ""
    notes: list[str] = field(default_factory=list)


class CategorizeWorkflow:
    name = "categorize"
    aliases = ("review",)
    title = "Categorization review"
    description = (
        "Run the categorization review in code: reads uncategorized merchants, categories and "
        "rules, detects transfers and card payments deterministically, classifies the rest with a "
        "bounded structured model step, and emits one propose_create_payee_rule card per confident "
        "merchant. It writes the final reply itself. Call it once, with no other tool in the same "
        "turn, and do not write a reply afterwards."
    )
    params_schema = {
        "type": "object",
        "properties": {
            "from_date": {"type": "string", "format": "date"},
            "to_date": {"type": "string", "format": "date"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 60},
            "apply": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    }

    def __init__(self, backend: Any = None):
        self._backend = backend

    def backend(self, ctx: WorkflowContext):
        return self._backend or OllamaStructuredBackend()

    async def run(self, ctx: WorkflowContext, params: dict[str, Any]) -> AsyncIterator[Any]:
        params = params or {}
        limit = max(1, min(int(params.get("limit") or 60), 100))
        from_date = params.get("from_date") or None
        to_date = params.get("to_date") or None
        want_apply = bool(params.get("apply"))
        stats = RunStats(period_label=_period_label(from_date, to_date))

        # 1. load ---------------------------------------------------------------------------
        merchants_raw = await ctx.tool("list_uncategorized_merchants", from_date=from_date, to_date=to_date, limit=limit)
        cats_raw = await ctx.tool("list_categories")
        rules_raw = await ctx.tool("list_rules", verbose=True)
        accounts, groups = await _load_accounts_and_groups(ctx)
        cats = [
            CategoryFacts(
                id=str(c["id"]), name=str(c["name"]),
                group_name=groups.get(str(c.get("group_id"))) if c.get("group_id") else None,
                treat_as_transfer=bool(c.get("treat_as_transfer")),
            )
            for c in (cats_raw.get("items") or [])
        ]
        merchants = [Merchant.from_item(i) for i in (merchants_raw.get("items") or [])]
        stats.rows = int(merchants_raw.get("total_uncategorized_count") or 0)
        stats.merchants = len(merchants)
        stats.truncated = bool(merchants_raw.get("truncated")) or int(merchants_raw.get("merchant_count") or 0) > len(merchants)
        currency = _currency(ctx)
        for ev in ctx.emit_step("load", {"limit": limit, "from_date": from_date, "to_date": to_date},
                                {"rows": stats.rows, "merchants": stats.merchants, "categories": len(cats)}):
            yield ev
        if not merchants:
            ctx.finish(render_summary(ctx.language, stats, [], [], [], [], nothing=True),
                       {"ok": True, "uncategorized_rows": 0, "merchants": 0, "proposals": 0, "applied": 0, "needs_review": 0, "skipped": 0})
            return

        # 2. deterministic pass -------------------------------------------------------------------
        decisions: list[Decision] = []
        queue: list[Merchant] = []
        rules = rules_raw.get("items") or []
        for m in merchants:
            d = classify_own_account_transfer(m, accounts, cats) or classify_card_payment(m, accounts, cats)
            if d is not None:
                decisions.append(d)
                stats.deterministic += 1
                continue
            rule_name = covered_by_rule(m, rules)
            if rule_name:
                decisions.append(Decision(m, None, 1.0, "rule", "covered", f"already covered by rule **{rule_name}**", covered_by=rule_name))
                stats.covered += 1
                continue
            queue.append(m)

        # 3. the model, in bounded batches -----------------------------------------------------------
        conventions = await ctx.pinned_text(CONVENTIONS_MAX_CHARS)
        backend = self.backend(ctx)
        stats.llm_count = len(queue)
        for start in range(0, len(queue), BATCH_SIZE):
            chunk = queue[start : start + BATCH_SIZE]
            batch = [(f"m{i + 1}", m) for i, m in enumerate(chunk)]
            if ctx.budget.remaining_llm_calls <= 0:
                decisions.extend(Decision(m, None, 0.0, "budget", "review", "not reviewed (run budget reached)") for m in chunk)
                stats.notes.append("model-call budget reached before every merchant was reviewed")
                continue
            stats.batches += 1
            try:
                classified = await backend.classify(ctx, batch, cats, conventions, currency=currency)
            except (WorkflowStepError, WorkflowBudgetExceeded) as exc:
                logger.warning("categorize: batch %d failed: %s", stats.batches, exc)
                decisions.extend(Decision(m, None, 0.0, "budget", "review", f"not reviewed ({exc})") for m in chunk)
                stats.notes.append(f"batch {stats.batches} was not classified ({exc})")
                continue
            by_id = {mid: m for mid, m in batch}
            answered: set[str] = set()
            for item in classified:
                m = by_id.get(item.merchant_id)
                if m is None or item.merchant_id in answered:
                    continue
                answered.add(item.merchant_id)
                decisions.append(_decision_from_model(m, item, cats))
            for mid, m in batch:
                if mid not in answered:
                    decisions.append(Decision(m, None, 0.0, "llm", "unknown", "the model returned no answer for this merchant"))
            for ev in ctx.emit_step("classify", {"batch": stats.batches, "size": len(batch)}, {"classified": len(answered)}):
                yield ev

        # 4. shape -------------------------------------------------------------------------------------
        decisions, merge_notes = dedupe_by_pattern(decisions)
        stats.notes.extend(merge_notes)
        proposals, _overflow = cap_proposals(decisions, MAX_PROPOSALS)

        # 5. previews, optional apply, cards ------------------------------------------------------------
        priority = int(rules_raw.get("suggested_priority_for_new_merchant_rule") or 10)
        auto_apply = want_apply and (
            bool(ctx.settings.workflow_auto_apply) or bool((ctx.agent.extra or {}).get("auto_apply_rules"))
        )
        applied: list[dict[str, Any]] = []
        cards: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        proposed_rows: list[dict[str, Any]] = []
        for d in proposals:
            assert d.category is not None
            args = {
                "match_pattern": d.merchant.pattern,
                "category_id": d.category.id,
                "name": f"Auto-categorize: {d.merchant.pattern}",
                "priority": priority,
            }
            preview = await ctx.tool("propose_create_payee_rule", **args)
            if not isinstance(preview, dict) or preview.get("error"):
                d.status = "skipped"
                d.reason = f"preview failed: {(preview or {}).get('error', 'unknown error')}"
                continue
            if preview.get("proposed", {}).get("name_collision"):
                args["name"] = args["name"] + " (2)"
                preview = await ctx.tool("propose_create_payee_rule", **args)
                if not isinstance(preview, dict) or preview.get("error"):
                    d.status = "skipped"
                    d.reason = "preview failed after a name collision"
                    continue
            rows = int(preview.get("proposed", {}).get("would_categorize_count") or 0)
            if auto_apply and d.confidence >= AUTO_APPLY_MIN_CONFIDENCE:
                try:
                    rule = await rule_service.create_rule(
                        ctx.session, ctx.workspace_id, ctx.user.id,
                        RuleCreate(
                            name=args["name"], priority=priority, conditions_op="and",
                            conditions=[RuleCondition(field="description", op="contains", value=d.merchant.pattern)],
                            actions=[RuleAction(op="set_category", value=d.category.id)],
                        ),
                    )
                    categorized = await rule_service.apply_single_rule(ctx.session, ctx.workspace_id, rule)
                except rule_service.DuplicateRuleError:
                    d.status = "skipped"
                    d.reason = f"a rule named '{args['name']}' already exists"
                    continue
                applied.append({"name": args["name"], "category": d.category.name, "confidence": d.confidence,
                                "rows": int(categorized), "rule_id": str(rule.id), "pattern": d.merchant.pattern})
                continue
            cards.append((PROPOSAL_TOOL, args, preview))
            proposed_rows.append({"pattern": d.merchant.pattern, "category": d.category.name,
                                  "confidence": d.confidence, "reason": d.reason, "rows": rows})
        if cards:
            for ev in ctx.emit_proposals(cards):
                yield ev

        # 6. summary -------------------------------------------------------------------------------------
        review = [d for d in decisions if d.status == "review"]
        skipped = [d for d in decisions if d.status in ("covered", "skipped", "unknown")]
        summary = render_summary(ctx.language, stats, applied, proposed_rows, review, skipped, notes=ctx.notes)
        ctx.finish(
            summary,
            {
                "ok": True,
                "period": stats.period_label,
                "uncategorized_rows": stats.rows,
                "merchants": stats.merchants,
                "classified_in_code": stats.deterministic,
                "covered_by_rules": stats.covered,
                "sent_to_model": stats.llm_count,
                "proposals": len(proposed_rows),
                "applied": len(applied),
                "needs_review": len(review),
                "skipped": len(skipped),
                "proposal_patterns": [p["pattern"] for p in proposed_rows][:MAX_PROPOSALS],
            },
        )


def _decision_from_model(m: Merchant, item: Classified, cats: list[CategoryFacts]) -> Decision:
    reason = (item.reason or "").strip()[:120]
    if item.category_name.strip().lower() == UNKNOWN.lower():
        return Decision(m, None, item.confidence, "llm", "unknown", reason or "the model could not place this merchant",
                        alternatives=_alternatives(item))
    cat = find_category_by_name(cats, item.category_name)
    if cat is None:
        return Decision(m, None, item.confidence, "llm", "unknown", f"model named an unknown category '{item.category_name}'")
    violation = validate_sign(m, cat)
    if violation:
        return Decision(m, cat, min(item.confidence, 0.6), "llm", "review", f"{violation} (model suggested {cat.name})",
                        alternatives=_alternatives(item))
    status = apply_thresholds(item.confidence)
    return Decision(m, cat, item.confidence, "llm", status, reason or f"model: {cat.name}", alternatives=_alternatives(item))


def _alternatives(item: Classified) -> list[tuple[str, float]]:
    if not item.probabilities:
        return []
    ranked = sorted(item.probabilities.items(), key=lambda kv: kv[1], reverse=True)
    return [(name, round(float(p), 3)) for name, p in ranked[:3] if name != item.category_name]


async def _load_accounts_and_groups(ctx: WorkflowContext) -> tuple[list[AccountFacts], dict[str, str]]:
    acc_rows = (
        await ctx.session.execute(
            select(Account.name, Account.type, Account.masked_number).where(Account.workspace_id == ctx.workspace_id)
        )
    ).all()
    accounts = [AccountFacts(name=str(r[0]), type=str(r[1] or ""), masked_number=r[2]) for r in acc_rows]
    grp_rows = (
        await ctx.session.execute(
            select(CategoryGroup.id, CategoryGroup.name).where(CategoryGroup.workspace_id == ctx.workspace_id)
        )
    ).all()
    groups = {str(r[0]): str(r[1]) for r in grp_rows}
    return accounts, groups


def _currency(ctx: WorkflowContext) -> str:
    prefs = getattr(ctx.user, "preferences", None) or {}
    return str(prefs.get("currency_display") or "USD")


def _period_label(from_date: Optional[str], to_date: Optional[str]) -> str:
    if from_date and to_date:
        return f"{from_date} to {to_date}"
    if from_date:
        return f"since {from_date}"
    if to_date:
        return f"until {to_date}"
    return "all time"


# --- summary --------------------------------------------------------------------------------------

_T = {
    "en": {
        "title": "Categorization review",
        "rows": "Uncategorized rows",
        "across": "across",
        "merchants": "merchants",
        "truncated": "(list capped at {limit}; run again after applying)",
        "deterministic": "Classified in code (own-account transfers, card payments)",
        "covered": "Already covered by an existing rule",
        "sent": "Sent to the model",
        "in_batches": "merchants in {n} batch(es)",
        "proposals": "Rule proposals",
        "applied_auto": "applied automatically",
        "applied": "Applied",
        "proposals_h": "Proposals",
        "no_proposals": "No proposals this run.",
        "review": "Needs your call",
        "skipped": "Skipped",
        "best_guess": "best guess",
        "rows_word": "rows",
        "credits_only": "credits only",
        "debits_only": "debits only",
        "th_rule": "Rule", "th_pattern": "Pattern", "th_category": "Category", "th_conf": "Confidence", "th_rows": "Rows", "th_rows_cat": "Rows categorized",
        "next": "**Next step:** click **Apply** on each card above (or **Apply all**) to create the rule — it also categorizes the existing rows — or reply with corrections. Rules are the only memory Securo keeps for merchants, so each applied card prevents the same question next month.",
        "next_covered": "Merchants already covered by a rule are categorized by Rules → Apply all rules.",
        "nothing": "Nothing to categorize: every posted transaction in scope already has a category.",
        "notes": "Notes",
        "unknown_n": "{n} merchant(s) the model could not place",
    },
    "pt-BR": {
        "title": "Revisão de categorização",
        "rows": "Linhas sem categoria",
        "across": "em",
        "merchants": "estabelecimentos",
        "truncated": "(lista limitada a {limit}; rode de novo depois de aplicar)",
        "deterministic": "Classificadas no código (transferências entre suas contas, pagamentos de cartão)",
        "covered": "Já cobertas por uma regra existente",
        "sent": "Enviadas ao modelo",
        "in_batches": "estabelecimentos em {n} lote(s)",
        "proposals": "Propostas de regra",
        "applied_auto": "aplicadas automaticamente",
        "applied": "Aplicadas",
        "proposals_h": "Propostas",
        "no_proposals": "Nenhuma proposta nesta rodada.",
        "review": "Precisa da sua decisão",
        "skipped": "Ignoradas",
        "best_guess": "melhor palpite",
        "rows_word": "linhas",
        "credits_only": "só créditos",
        "debits_only": "só débitos",
        "th_rule": "Regra", "th_pattern": "Padrão", "th_category": "Categoria", "th_conf": "Confiança", "th_rows": "Linhas", "th_rows_cat": "Linhas categorizadas",
        "next": "**Próximo passo:** clique em **Aplicar** em cada cartão acima (ou **Aplicar todos**) para criar a regra — ela também categoriza as linhas existentes — ou responda com correções. Regras são a única memória que o Securo guarda dos estabelecimentos; cada cartão aplicado evita a mesma pergunta no mês que vem.",
        "next_covered": "Estabelecimentos já cobertos por uma regra são categorizados em Regras → Aplicar todas as regras.",
        "nothing": "Nada para categorizar: toda transação lançada no período já tem categoria.",
        "notes": "Observações",
        "unknown_n": "{n} estabelecimento(s) que o modelo não conseguiu classificar",
    },
}


def _t(lang: str) -> dict[str, str]:
    return _T["pt-BR"] if (lang or "").lower().startswith("pt") else _T["en"]


def render_summary(
    lang: str,
    stats: RunStats,
    applied: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    review: list[Decision],
    skipped: list[Decision],
    *,
    notes: Optional[list[str]] = None,
    nothing: bool = False,
) -> str:
    t = _t(lang)
    out: list[str] = [f"## {t['title']} — {stats.period_label}", ""]
    if nothing:
        out.append(t["nothing"])
        return "\n".join(out)
    trunc = (" " + t["truncated"].format(limit=stats.merchants)) if stats.truncated else ""
    out.append(f"- {t['rows']}: **{stats.rows}** {t['across']} **{stats.merchants}** {t['merchants']}{trunc}")
    out.append(f"- {t['deterministic']}: **{stats.deterministic}**")
    out.append(f"- {t['covered']}: **{stats.covered}**")
    out.append(f"- {t['sent']}: **{stats.llm_count}** {t['in_batches'].format(n=stats.batches)}")
    out.append(f"- {t['proposals']}: **{len(proposals)}** · {t['applied_auto']}: **{len(applied)}**")
    out.append("")
    if applied:
        out.append(f"### {t['applied']} ({len(applied)})")
        out.append(f"| {t['th_rule']} | {t['th_category']} | {t['th_conf']} | {t['th_rows_cat']} |")
        out.append("|---|---|---|---|")
        for a in applied:
            out.append(f"| `{a['name']}` | {a['category']} | {a['confidence']:.2f} | {a['rows']} |")
        out.append("")
    out.append(f"### {t['proposals_h']} ({len(proposals)})")
    if proposals:
        out.append(f"| {t['th_pattern']} | {t['th_category']} | {t['th_conf']} | {t['th_rows']} |")
        out.append("|---|---|---|---|")
        for p in proposals:
            reason = f" — {p['reason']}" if p.get("reason") else ""
            out.append(f"| `{p['pattern']}` | {p['category']} | {p['confidence']:.2f}{reason} | {p['rows']} |")
    else:
        out.append(t["no_proposals"])
    out.append("")
    if review:
        out.append(f"### {t['review']} ({len(review)})")
        for d in review:
            m = d.merchant
            direction = t["credits_only"] if m.credit_only else (t["debits_only"] if m.debit_only else "")
            direction = f", {direction}" if direction else ""
            guess = f" — {t['best_guess']} **{d.category.name}** ({d.confidence:.2f})" if d.category else ""
            out.append(f"- `{m.pattern}` ({m.count} {t['rows_word']}{direction}){guess}: {d.reason}")
        out.append("")
    if skipped or stats.notes:
        out.append(f"### {t['skipped']} ({len(skipped)})")
        unknown = [d for d in skipped if d.status == "unknown"]
        for d in skipped:
            if d.status == "unknown":
                continue
            out.append(f"- `{d.merchant.pattern}` — {d.reason}")
        if len(unknown) <= 5:
            for d in unknown:
                out.append(f"- `{d.merchant.pattern}` — {d.reason}")
        elif unknown:
            out.append(f"- {t['unknown_n'].format(n=len(unknown))}: " + ", ".join(f"`{d.merchant.pattern}`" for d in unknown[:15]))
        for n in stats.notes:
            out.append(f"- {n}")
        out.append("")
    if notes:
        out.append(f"### {t['notes']}")
        out.extend(f"- {n}" for n in notes)
        out.append("")
    out.append(t["next"])
    if stats.covered:
        out.append(t["next_covered"])
    return "\n".join(out)


registry.register(CategorizeWorkflow())
