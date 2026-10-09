"""Advice rules for the Finance analyst: figures in, one "Next move" out. Pure, no I/O.

The constants are policy the user approved (plan decision 4, 2026-10-09), each with its
reason, and a rule whose inputs are missing is skipped, never guessed. The model never
writes the next move: guided answers append the line this module returns, so the action
and its amount always match the figures Securo computed.

Facts contract (every key optional; `advisor_figures.advisor_facts` builds it):
    monthly_spend         float   spending per month over the days of complete data
    liquid_cash           float   checking + savings (positive) + non-retirement brokerage cash
    cards_with_interest   list    [{"card", "interest", "owed"}] cards charged interest in 90 days
    interest_90           float   all card interest in the last 90 days
    spikes                list | None   [{"category", "amount", "baseline", "excess"}]; None = too little history
    savings_rate          float   contribution-aware savings rate (fraction)
    band_next, band_cut   float   next savings band (fraction) and the monthly spending cut that reaches it
    years_now, years_after_cut    float | None   years to FI now and after that cut
    retirement_cash       float   cash inside retirement accounts
    invested_assets       float   the FIRE denominator for every share
    crypto_value          float
    largest_stock         dict    {"name", "ticker", "value"}
    stock_total           float   all positions classified as single stocks
    top_controllable      dict    {"category", "amount" (per month), "half"}
    annual_contribution   float
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Policy constants (decision 4) — the reason for each is in the plan and the rule's docstring line.
CASH_FLOOR_MONTHS = 3            # below 3 months an emergency forces selling or borrowing
CASH_BUFFER_MONTHS = 6           # above a 6-month buffer cash only drags
IDLE_CASH_MIN = 1000.0
SPIKE_RATIO = 1.5                # month > 1.5 x its usual level ...
SPIKE_MIN_EXCESS = 200.0         # ... and at least 200 over it
RETIREMENT_CASH_MAX = 0.05       # cash inside retirement accounts, share of invested assets
RETIREMENT_CASH_MIN = 1000.0
SAVINGS_BANDS = (0.20, 0.35, 0.50, 0.65)   # ~37 / 25 / 17 / 11 years to FI from zero (5% real, 4% withdrawal)
CRYPTO_MAX = 0.05                # a 75% crypto drawdown then costs at most ~4%
SINGLE_POSITION_MAX = 0.10       # a 50% drop in one stock then costs at most 5%
STOCK_SHARE_MAX = 0.20           # single-stock risk is not paid for

PRIORITY = {
    "card_carry": 1, "cash_floor": 2, "spending_spike": 3, "idle_cash": 4, "cash_drag": 5,
    "crypto_cap": 7, "single_position": 8, "stock_share": 9, "spending_cut": 10, "keep_going": 12,
}

# message stems -> the rules a "Next move" may use for that question (decision 4)
FOCUS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("cash", "emergenc", "buffer"), ("cash_floor", "idle_cash")),
    (("debt", "card", "interest"), ("card_carry",)),
    (("retire", "on track", "savings rate"), ("savings_band",)),
    (("invest more", "contribut", "put away"), ("savings_band", "idle_cash")),
    (("crypto",), ("crypto_cap",)),
    (("stock", "concentrat", "diversif"), ("single_position", "stock_share")),
    (("wast", "cut", "spend"), ("spending_spike", "spending_cut")),
)

# cap fractions answers may quote ("10%"); money thresholds stay out of packs on purpose,
# since grounding accepts every pack number x100
CAPS = {"crypto_max": CRYPTO_MAX, "single_position_max": SINGLE_POSITION_MAX, "stock_share_max": STOCK_SHARE_MAX,
        "retirement_cash_max": RETIREMENT_CASH_MAX, "top_savings_band": SAVINGS_BANDS[-1]}


@dataclass(frozen=True)
class Recommendation:
    rule: str
    priority: int
    text: str
    amount: Optional[float]
    figures: dict[str, float] = field(default_factory=dict)
    names: tuple[str, ...] = ()      # names the text mentions (a card, a category), for grounding


def focus_rules(message: str) -> set[str]:
    text = (message or "").lower()
    out: set[str] = set()
    for stems, rules in FOCUS:
        if any(stem in text for stem in stems):
            out.update(rules)
    if re.search(r"\bFI\b|\bFIRE\b", message or ""):
        out.add("savings_band")
    return out


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def _num(facts: dict[str, Any], key: str) -> Optional[float]:
    value = facts.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def recommend(facts: dict[str, Any], money: Callable[[Optional[float]], str]) -> list[Recommendation]:
    """Every rule that fires, lowest priority number first."""
    out: list[Recommendation] = []
    monthly = _num(facts, "monthly_spend")
    cash = _num(facts, "liquid_cash")
    rate = _num(facts, "savings_rate")
    invested = _num(facts, "invested_assets")

    carrying = [c for c in facts.get("cards_with_interest") or [] if (c.get("owed") or 0) > 0]
    if carrying:
        worst = max(carrying, key=lambda c: c["owed"])
        out.append(Recommendation(
            "card_carry", PRIORITY["card_carry"],
            f"Pay off the {money(worst['owed'])} on {worst['card']}: you paid {money(worst['interest'])} in card interest "
            "in the last 90 days, and that interest beats any return you can earn.",
            worst["owed"], {"owed": worst["owed"], "interest": worst["interest"], "window_days": 90}, (str(worst["card"]),)))
    if monthly and cash is not None and cash < CASH_FLOOR_MONTHS * monthly:
        target = round(CASH_FLOOR_MONTHS * monthly, 2)
        months = round(cash / monthly, 1)
        out.append(Recommendation(
            "cash_floor", PRIORITY["cash_floor"],
            f"Build cash to {money(target)}, 3 months of spending; you have {money(cash)}, {months:.1f} months.",
            target, {"target": target, "cash": cash, "months": months}))
    spikes = facts.get("spikes")
    if spikes:
        s = spikes[0]
        out.append(Recommendation(
            "spending_spike", PRIORITY["spending_spike"],
            f"{s['category']} hit {money(s['amount'])} last month, {money(s['excess'])} over its usual {money(s['baseline'])}. Pull it back.",
            s["excess"], {"amount": s["amount"], "excess": s["excess"], "baseline": s["baseline"]}, (str(s["category"]),)))
    if monthly and cash is not None:
        buffer = round(CASH_BUFFER_MONTHS * monthly + max(IDLE_CASH_MIN, 0.5 * monthly), 2)
        if cash > buffer:
            excess = round(cash - buffer, 2)
            out.append(Recommendation(
                "idle_cash", PRIORITY["idle_cash"],
                f"Move {money(excess)} into investments: that is the cash above a 6-month buffer of {money(buffer)}.",
                excess, {"excess": excess, "buffer": buffer}))
    retirement_cash = _num(facts, "retirement_cash")
    if invested and retirement_cash and retirement_cash > RETIREMENT_CASH_MAX * invested and retirement_cash >= RETIREMENT_CASH_MIN:
        out.append(Recommendation(
            "cash_drag", PRIORITY["cash_drag"],
            f"{money(retirement_cash)} is sitting in cash inside your retirement accounts; invest it in your target allocation.",
            retirement_cash, {"retirement_cash": retirement_cash}))
    band, cut = _num(facts, "band_next"), _num(facts, "band_cut")
    if rate is not None and band is not None and cut is not None and rate < band:
        years_now, years_after = _num(facts, "years_now"), _num(facts, "years_after_cut")
        tail = ""
        if years_now is not None and years_after is not None:
            tail = f" That alone moves financial independence from {years_now:.1f} to {years_after:.1f} years away."
        out.append(Recommendation(
            "savings_band", 6 if rate < SAVINGS_BANDS[0] else 11,
            f"Cut {money(cut)} a month from spending to reach a {_pct(band)} savings rate; you are at {_pct(rate)}.{tail}",
            cut, {"cut": cut, "band": band, "rate": rate,
                  **({"years_now": years_now, "years_after": years_after} if years_now is not None and years_after is not None else {})}))
    crypto = _num(facts, "crypto_value")
    if invested and crypto and crypto > CRYPTO_MAX * invested:
        excess = round(crypto - CRYPTO_MAX * invested, 2)
        share = round(crypto / invested, 4)
        out.append(Recommendation(
            "crypto_cap", PRIORITY["crypto_cap"],
            f"Crypto is {_pct(share)} of your investments, {money(excess)} over the 5% cap. Stop adding to it; new money goes to broad index funds.",
            excess, {"share": share, "excess": excess}))
    largest = facts.get("largest_stock") or {}
    if invested and (largest.get("value") or 0) > SINGLE_POSITION_MAX * invested:
        excess = round(largest["value"] - SINGLE_POSITION_MAX * invested, 2)
        share = round(largest["value"] / invested, 4)
        name = largest.get("ticker") or largest.get("name") or "One stock"
        out.append(Recommendation(
            "single_position", PRIORITY["single_position"],
            f"{name} alone is {_pct(share)} of your investments, {money(excess)} over the 10% cap. Stop adding to it.",
            excess, {"share": share, "excess": excess}, (str(name),)))
    stock_total = _num(facts, "stock_total")
    if invested and stock_total and stock_total > STOCK_SHARE_MAX * invested:
        excess = round(stock_total - STOCK_SHARE_MAX * invested, 2)
        share = round(stock_total / invested, 4)
        out.append(Recommendation(
            "stock_share", PRIORITY["stock_share"],
            f"Individual stocks are {_pct(share)} of your investments, {money(excess)} over the 20% cap. Send new money to broad index funds instead.",
            excess, {"share": share, "excess": excess}))
    top = facts.get("top_controllable") or {}
    if rate is not None and rate < SAVINGS_BANDS[-1] and top.get("category"):
        out.append(Recommendation(
            "spending_cut", PRIORITY["spending_cut"],
            f"{top['category']} runs {money(top['amount'])} a month. Cut it in half and invest the {money(top['half'])}.",
            top["half"], {"amount": top["amount"], "half": top["half"]}, (str(top["category"]),)))
    serious = {"card_carry", "cash_floor", "spending_spike", "idle_cash", "cash_drag", "crypto_cap", "single_position", "stock_share"}
    if rate is not None and rate >= SAVINGS_BANDS[-1] and not any(r.rule in serious for r in out):
        monthly_invest = round((_num(facts, "annual_contribution") or 0.0) / 12, 2)
        out.append(Recommendation(
            "keep_going", PRIORITY["keep_going"],
            f"Nothing here needs fixing. Keep investing {money(monthly_invest)} a month.",
            monthly_invest, {"monthly": monthly_invest}))
    return sorted(out, key=lambda r: r.priority)


def _focus_verdict(focus: set[str], facts: dict[str, Any], money: Callable[[Optional[float]], str]) -> Optional[str]:
    """What to say about the question's own subject when none of its rules fired."""
    monthly, cash = _num(facts, "monthly_spend"), _num(facts, "liquid_cash")
    if focus & {"card_carry"}:
        interest = _num(facts, "interest_90") or 0.0
        if interest > 0:
            return f"You paid {money(interest)} in card interest in the last 90 days, but no card that charged it carries a balance now."
        return "No card charged you interest in the last 90 days."
    if focus & {"cash_floor", "idle_cash"} and monthly and cash is not None:
        return f"Your cash, {money(cash)}, covers {cash / monthly:.1f} months of spending: inside the 3 to 6 month range."
    if focus & {"crypto_cap"}:
        invested, crypto = _num(facts, "invested_assets"), _num(facts, "crypto_value") or 0.0
        if invested:
            return f"Crypto is {_pct(crypto / invested)} of your investments, under the 5% cap."
    if focus & {"single_position", "stock_share"}:
        invested, stocks = _num(facts, "invested_assets"), _num(facts, "stock_total") or 0.0
        if invested:
            return f"No single stock is over 10% and stocks are {_pct(stocks / invested)} in total, under the 20% cap."
    if focus & {"savings_band"}:
        rate = _num(facts, "savings_rate")
        if rate is not None:
            return f"Your {_pct(rate)} savings rate is in the top band."
    if focus & {"spending_spike", "spending_cut"}:
        if facts.get("spikes") is None:
            return "There is not enough complete history yet to call a category jump."
        return "No category jumped unusually."
    return None


def _verdict_figures(facts: dict[str, Any]) -> dict[str, float]:
    """The numbers a focus verdict may quote, rounded the way the verdict writes them."""
    monthly, cash, invested = _num(facts, "monthly_spend"), _num(facts, "liquid_cash"), _num(facts, "invested_assets")
    out = {k: v for k, v in (("interest_90", _num(facts, "interest_90")), ("liquid_cash", cash),
                             ("savings_rate", _num(facts, "savings_rate"))) if v is not None}
    out["window_days"] = 90.0
    if monthly and cash is not None:
        out["months"] = round(cash / monthly, 1)
    if invested:
        out["crypto_share"] = round((_num(facts, "crypto_value") or 0.0) / invested, 4)
        out["stock_share"] = round((_num(facts, "stock_total") or 0.0) / invested, 4)
    return out


def next_move(facts: dict[str, Any], message: str, money: Callable[[Optional[float]], str]) -> Optional[dict[str, Any]]:
    """The one action an answer ends with: the highest-priority fired rule the question is
    about; otherwise a verdict on the question's subject plus the biggest lever overall.

    Returns {rule, text, amount, figures, names, rules_fired}. Callers check the text
    against `figures` + `names` + caps only (never against the text itself)."""
    recs = recommend(facts, money)
    rules_fired = [r.rule for r in recs]
    focus = focus_rules(message)
    pick = next((r for r in recs if r.rule in focus), None)
    if pick is not None:
        return {"rule": pick.rule, "text": pick.text, "amount": pick.amount, "figures": dict(pick.figures),
                "names": list(pick.names), "rules_fired": rules_fired}
    verdict = _focus_verdict(focus, facts, money) if focus else None
    if not recs:
        if not verdict:
            return None
        return {"rule": None, "text": verdict, "amount": None, "figures": _verdict_figures(facts), "names": [], "rules_fired": []}
    top = recs[0]
    figures = dict(top.figures)
    if verdict:
        figures.update({f"verdict_{k}": v for k, v in _verdict_figures(facts).items()})
    return {"rule": top.rule, "text": f"{verdict} Biggest lever: {top.text}" if verdict else top.text,
            "amount": top.amount, "figures": figures, "names": list(top.names), "rules_fired": rules_fired}


def grounding_pack(move: dict[str, Any]) -> dict[str, Any]:
    """What a next-move line may be checked against: its own figures, the names it
    mentions and the caps, never its own text (that would ground anything)."""
    return {"figures": move.get("figures") or {}, "names": move.get("names") or [], "caps": CAPS}


NEXT_MOVE_LINE_RE = re.compile(r"(?im)^\s*(?:[-*•]\s*)?(?:\*\*)?\s*(?:next move|próximo passo|próximo paso)\b.*$")


def strip_model_next_move(text: str) -> str:
    """Remove any "Next move" line the model wrote; Securo appends its own."""
    out = NEXT_MOVE_LINE_RE.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()
