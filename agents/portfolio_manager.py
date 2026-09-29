import logging
from datetime import date
from enum import Enum
from typing import Literal, Optional

from openai import OpenAI
from pydantic import BaseModel, Field, create_model

from agents.risk_manager import REASON_MAX_POSITION_PCT
from config import settings

logger = logging.getLogger(__name__)


class PortfolioDecision(BaseModel):
    ticker: str
    action: Literal["buy", "sell", "hold"]
    # Fraction of equity (0.05 = 5%). le=1.0 is a unit guard, not a risk
    # limit: it rejects the model writing 5.0 for "5%" (i.e. 500%).
    size_pct: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of account equity, e.g. 0.05 means 5%. Never a percentage number.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


def _forced_hold(ticker: str, reasoning: str) -> dict:
    """A decision made entirely in code, with no LLM call — used whenever
    the risk manager has already made the outcome a foregone conclusion."""
    return {
        "ticker": ticker,
        "action": "hold",
        "size_pct": 0.0,
        "confidence": 1.0,
        "reasoning": reasoning,
    }


def _signal_sections(
    technical_signal: dict, sentiment_signal: dict, fundamentals_signal: dict | None
) -> list[str]:
    """The signal block shared by both prompts, so a position review sees the
    signals described exactly as the entry decision did."""
    sections = [
        "",
        "Technical signal:",
        f"- signal: {technical_signal.get('signal')}",
        f"- confidence: {technical_signal.get('confidence')}",
        f"- reasoning: {technical_signal.get('reasoning')}",
        "",
        "Sentiment signal (confidence = strength of news evidence, capped by how many "
        "recent headlines support it; low confidence means little news, not a contrary view):",
        f"- sentiment: {sentiment_signal.get('sentiment')}",
        f"- confidence: {sentiment_signal.get('confidence')}",
        f"- headlines behind it: {len(sentiment_signal.get('headlines') or [])}",
        f"- reasoning: {sentiment_signal.get('reasoning')}",
    ]

    if fundamentals_signal is not None:
        sections += [
            "",
            "Fundamentals flag (minor, advisory input only — not a primary signal):",
            f"- unusual: {fundamentals_signal.get('unusual')}",
            f"- flags: {fundamentals_signal.get('flags')}",
            f"- reasoning: {fundamentals_signal.get('reasoning')}",
        ]

    return sections


def synthesize_decision(
    ticker: str,
    technical_signal: dict,
    sentiment_signal: dict,
    risk_check: dict,
    fundamentals_signal: dict | None = None,
) -> dict:
    """Synthesize technical + sentiment (+ optional fundamentals) signals,
    under the risk manager's ceiling, into a final trade decision.

    IMPORTANT: risk_check["adjusted_size"] is a ceiling computed for
    whatever specific action (buy/sell) was proposed to the risk manager
    upstream — buy and sell sizing use entirely different math in
    risk_manager.py. If the LLM here decides on a *different* action than
    whatever was risk-checked, this ceiling is not a meaningful bound for
    that new action. This function does not resolve that mismatch (that's
    an orchestration-layer concern); it only enforces the ceiling against
    whatever size_pct comes back, for whatever action comes back. The
    orchestration graph (Phase 5) should re-run check_trade against the
    *final* decision as one more gate before execution.

    The LLM is explicitly told the ceiling and told never to exceed it,
    but that instruction is never trusted on its own — size_pct is always
    re-validated (clamped) in code afterward, regardless of what the model
    returned or claimed to have done.

    One precheck rejection is deliberately NOT forced to hold here: a buy
    blocked purely because the position is already at/over max_position_pct
    (reason_code == REASON_MAX_POSITION_PCT, ceiling == 0). Every other
    rejection reason is a hard stop and forces a hold immediately, with no
    LLM call. But orchestration.graph.risk_final_check_node offers exactly
    this one case to a human for override — and it only does that when the
    *final* decision is still a "buy". If this function forced a hold here
    too, that decision would already be "hold" by the time the graph reaches
    the override check, and the override path would be permanently
    unreachable for the one case it exists for. So instead the LLM still
    runs, is told there's no automatic headroom, and is free to return a
    real buy anyway — which then flows to risk_final_check_node for human
    review instead of executing automatically.
    """
    approved = risk_check.get("approved", False)
    override_eligible = not approved and risk_check.get("reason_code") == REASON_MAX_POSITION_PCT

    if not approved and not override_eligible:
        reasons = "; ".join(risk_check.get("reasons", [])) or "not specified"
        return _forced_hold(
            ticker, f"Risk manager rejected this trade — forced to hold. Reasons: {reasons}"
        )

    ceiling = float(risk_check.get("adjusted_size", 0.0))
    if ceiling <= 0 and not override_eligible:
        return _forced_hold(
            ticker, "Risk manager allows zero size for this trade — forced to hold."
        )

    prompt_sections = [
        f"Ticker: {ticker}",
        *_signal_sections(technical_signal, sentiment_signal, fundamentals_signal),
    ]

    if override_eligible:
        reasons_text = "; ".join(risk_check.get("reasons", [])) or "not specified"
        prompt_sections += [
            "",
            f"Risk manager note: {reasons_text} There is currently zero automatic headroom "
            "to add to this position. If the signals above still justify a buy, return "
            "action='buy' with the size_pct you'd genuinely recommend anyway, as a fraction "
            "of equity (e.g. 0.02 for 2%, not 2.0) — it will NOT "
            "execute automatically; it will be routed to a human for manual override "
            "approval before any order is placed. If the signals don't justify overriding "
            "the limit, return action='hold' instead.",
        ]
    else:
        prompt_sections += [
            "",
            f"Risk manager constraint: the MAXIMUM size_pct you may return is {ceiling:.4f} "
            f"({ceiling:.2%} of account equity). This is a hard ceiling, not a suggestion — "
            "you must never return a size_pct above this value. If you believe a smaller size "
            "is more appropriate given the signals above, return that smaller size instead.",
        ]

    system_content = (
        "You are a portfolio manager synthesizing multiple agent signals "
        "into a single final trade decision. Weigh the technical signal as "
        "the primary driver, sentiment as a secondary input, and fundamentals "
        "(if given) as a minor, advisory input only. "
        "size_pct is always a fraction of account equity: 0.05 means 5%, 0.02 means 2%. "
        "Never write a percentage number like 5.0. "
    )
    system_content += (
        "Follow the risk manager note's instructions about the buy/hold choice."
        if override_eligible
        else "Your size_pct must never exceed the hard ceiling stated in the prompt."
    )

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": "\n".join(prompt_sections)},
        ],
        response_format=PortfolioDecision,
    )

    decision = completion.choices[0].message.parsed
    result = decision.model_dump()

    # Never trust the LLM to have obeyed the ceiling on its own — re-validate here.
    # Structured output guarantees the *shape* of the response; it says nothing
    # about whether a value obeys a constraint that depended on runtime state
    # (the ceiling varies per call, so it can't be baked into a static schema).
    result["ticker"] = ticker  # never trust the model's echo of the ticker either

    if result["action"] == "hold":
        result["size_pct"] = 0.0
    elif override_eligible and result["action"] == "buy":
        # No automatic ceiling applies here by design (ceiling == 0 just
        # means "no automatic headroom", not "cap this at zero") — the real
        # gates on this size_pct are risk_final_check_node's re-check and,
        # if it's still rejected for the same reason, a human's approval.
        pass
    elif result["size_pct"] > ceiling:
        logger.warning(
            "Portfolio manager LLM returned size_pct=%.4f exceeding ceiling=%.4f for %s "
            "— clamping. This is worth tracking: it means the model is not reliably "
            "obeying the stated constraint.",
            result["size_pct"],
            ceiling,
            ticker,
        )
        original_size = result["size_pct"]
        result["size_pct"] = ceiling
        result["reasoning"] += (
            f" [NOTE: model requested size_pct={original_size:.4f}, exceeding the risk "
            f"manager's ceiling of {ceiling:.4f}; clamped down to the ceiling.]"
        )

    return result


def _position_lines(position: dict, today: date) -> list[str]:
    """A held position as the review and rotation prompts both describe it.
    position is one entry of portfolio_state["position_details"]."""
    entry_price = float(position["avg_entry_price"])
    current_price = float(position["current_price"])
    high_water_mark = max(float(position.get("high_water_mark") or 0.0), current_price)
    opened_at = position.get("opened_at")
    held = (
        f"{(today - date.fromisoformat(opened_at)).days} days (opened {opened_at})"
        if opened_at
        else "unknown"
    )
    return [
        f"- {position['qty']:g} shares, entry ${entry_price:,.2f}, now ${current_price:,.2f} "
        f"(unrealized {float(position['unrealized_plpc']):+.2%})",
        f"- held {held}",
        f"- {1 - current_price / high_water_mark:.2%} below its highest close since entry "
        f"(${high_water_mark:,.2f})",
    ]


class PositionReview(BaseModel):
    # Both cases come before the action: fields are generated in order, so
    # the model has argued against owning the stock before it decides,
    # rather than weighing only whether something has "clearly broken".
    case_for_owning: str
    case_against_owning: str
    # hold/sell only: a review can exit a position, but never add to one. No
    # size field — the exit is all-or-nothing and sized in code, so the model
    # only makes the call.
    action: Literal["hold", "sell"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


# Why the review is running, stated to the model: the technical signal it sees
# may be a hold or a buy, and a buy must not read as a case for adding.
REVIEW_TRIGGER_NOTES = {
    "technical_hold": "The technical signal says hold, so nothing else weighs this position today.",
    "at_cap_buy": (
        "The technical signal says buy, but the position is already at its size cap, "
        "so adding is not an option. Decide only whether to keep what you hold."
    ),
}

REVIEW_SYSTEM_PROMPT = (
    "You are a portfolio manager reviewing a position you already hold. Adding to "
    "it is not an option here; your job is to decide whether to keep it or exit it, "
    "judged on today's evidence. First make the case for owning this stock based on "
    "today's signals, then make the case against owning it, each as strongly as the "
    "evidence honestly allows — as if you were seeing the stock for the first time. "
    "Then decide which case is stronger. Neither side needs to be decisive: sell if "
    "the case against is stronger, hold if the case for is stronger. Only when the "
    "two are genuinely about equal, hold — because exiting and re-entering has a "
    "cost — and say that is why; a mix of weak points on both sides is not "
    "automatically equal, so weigh them. 'Nothing has clearly broken' is not a "
    "reason to hold, just as 'it has not moved yet' is not a reason to sell. The "
    "entry price and unrealized P&L are context, not reasons: 'it is down, wait for "
    "it to come back' and 'it is up, lock in the gain' are not valid justifications "
    "on their own. Stop-losses and trailing stops are handled separately by fixed "
    "rules, so do not act as one. Weigh the "
    "technical signal as the primary driver, sentiment as a secondary input, and "
    "fundamentals (if given) as a minor, advisory input only."
)


def review_position(
    ticker: str,
    position: dict,
    sell_size_pct: float,
    technical_signal: dict,
    sentiment_signal: dict,
    fundamentals_signal: dict | None = None,
    today: date | None = None,
    trigger: str = "technical_hold",
) -> dict:
    """Ask the LLM whether to keep a held position that nothing else would
    reconsider: a technical hold (a forced hold with no LLM call) or a buy
    blocked at the position cap. Returns a decision in the same shape as
    synthesize_decision.

    position is one entry of portfolio_state["position_details"];
    sell_size_pct is the risk manager's approved size for selling all of it.
    The prompt design reasoning lives in PROMPT_PATTERNS.md.
    """
    prompt_sections = [
        f"Ticker: {ticker}",
        *_signal_sections(technical_signal, sentiment_signal, fundamentals_signal),
        "",
        "Current position (you already hold this):",
        *_position_lines(position, today or date.today()),
        "",
        f"Why this review: {REVIEW_TRIGGER_NOTES[trigger]}",
        "",
        "Make the case for owning it, then the case against, then return action='sell' "
        "to exit the whole position or action='hold' to keep it.",
    ]

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(prompt_sections)},
        ],
        response_format=PositionReview,
    )
    review = completion.choices[0].message.parsed

    return {
        "ticker": ticker,
        "action": review.action,
        "size_pct": sell_size_pct if review.action == "sell" else 0.0,
        "confidence": review.confidence,
        "reasoning": review.reasoning,
        "case_for_owning": review.case_for_owning,
        "case_against_owning": review.case_against_owning,
    }


ROTATION_SYSTEM_PROMPT = (
    "You are a portfolio manager. The portfolio holds its maximum number of positions, "
    "so a new stock can only be bought by selling one already held. The candidates below "
    "got a buy signal today and were blocked only because the portfolio is full. Decide "
    "whether to swap one holding for one candidate, or make no swap. No swap is the "
    "default: a swap means two trades and giving up a position on the strength of one "
    "day's signal, so propose one only when a candidate is clearly stronger than the "
    "holding it would replace, not merely comparable. Every candidate has a buy signal "
    "because that is how candidates are chosen, while holdings are shown with whatever "
    "signal they have today — so the label alone is not evidence that a candidate is "
    "better; compare the confidence and reasoning behind each signal. Judge each holding "
    "as if choosing whether to buy it today at the current price: entry price and "
    "unrealized P&L are context, not reasons, so 'it is down' is not a reason to sell it "
    "and 'it is up' is not a reason to keep it. Stop-losses and trailing stops are handled "
    "separately by fixed rules. Weigh the technical signal as the primary driver, "
    "sentiment as a secondary input, and fundamentals (if given) as a minor, advisory "
    "input only."
)


def _rotation_schema(candidates: list[str], holdings: list[str]) -> type[BaseModel]:
    """Built per call so the model can only name a ticker that is actually a
    candidate (to buy) or a holding (to sell). No size fields: a swap sells
    the whole holding and code sizes the buy. Enums, not Literals: a
    one-ticker Literal becomes a JSON-schema "const", an enum stays "enum"."""
    sell = Enum("HoldingTicker", {t: t for t in holdings}, type=str)
    buy = Enum("CandidateTicker", {t: t for t in candidates}, type=str)
    return create_model(
        "RotationProposal",
        action=(Literal["swap", "no_swap"], ...),
        sell_ticker=(Optional[sell], ...),
        buy_ticker=(Optional[buy], ...),
        confidence=(float, Field(ge=0.0, le=1.0)),
        reasoning=(str, ...),
    )


def propose_rotation(candidates: list[dict], holdings: list[dict], today: date | None = None) -> dict:
    """Ask the LLM whether to swap one held position for one buy that was
    blocked because the portfolio is full. candidates are
    {"ticker", "technical_signal", "sentiment_signal", "fundamentals_signal"};
    holdings are {"ticker", "position", "signals"}, where signals has the same
    three keys or is None when the ticker had no run today. Returns
    {"action", "sell_ticker", "buy_ticker", "confidence", "reasoning"}, with
    both tickers None unless the action is a complete swap.
    The prompt design reasoning lives in PROMPT_PATTERNS.md.
    """
    today = today or date.today()
    prompt_sections = ["Candidates (buy signal today, blocked because the portfolio is full):"]
    for c in candidates:
        prompt_sections += [
            "",
            f"=== Candidate: {c['ticker']} ===",
            *_signal_sections(c["technical_signal"], c["sentiment_signal"], c.get("fundamentals_signal")),
        ]
    prompt_sections += ["", "Holdings (a swap sells the whole position):"]
    for h in holdings:
        signals = h.get("signals")
        prompt_sections += ["", f"=== Holding: {h['ticker']} ==="]
        if signals:
            prompt_sections += _signal_sections(
                signals["technical_signal"], signals["sentiment_signal"], signals.get("fundamentals_signal")
            )
        else:
            prompt_sections += ["", "No signals today (not analyzed in this run)."]
        prompt_sections += ["", "Position:", *_position_lines(h["position"], today)]
    prompt_sections += [
        "",
        "Return action='swap' with the holding to sell and the candidate to buy, "
        "or action='no_swap' with both left null.",
    ]

    schema = _rotation_schema([c["ticker"] for c in candidates], [h["ticker"] for h in holdings])
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": ROTATION_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(prompt_sections)},
        ],
        response_format=schema,
    )
    proposal = completion.choices[0].message.parsed

    # A swap missing either side is not a swap.
    swap = proposal.action == "swap" and proposal.sell_ticker and proposal.buy_ticker
    return {
        "action": "swap" if swap else "no_swap",
        "sell_ticker": proposal.sell_ticker.value if swap else None,
        "buy_ticker": proposal.buy_ticker.value if swap else None,
        "confidence": proposal.confidence,
        "reasoning": proposal.reasoning,
    }
