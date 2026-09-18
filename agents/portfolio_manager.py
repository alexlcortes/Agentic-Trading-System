import logging
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from agents.risk_manager import REASON_MAX_POSITION_PCT
from config import settings

logger = logging.getLogger(__name__)


class PortfolioDecision(BaseModel):
    ticker: str
    action: Literal["buy", "sell", "hold"]
    size_pct: float = Field(ge=0.0)
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
        "",
        "Technical signal:",
        f"- signal: {technical_signal.get('signal')}",
        f"- confidence: {technical_signal.get('confidence')}",
        f"- reasoning: {technical_signal.get('reasoning')}",
        "",
        "Sentiment signal:",
        f"- sentiment: {sentiment_signal.get('sentiment')}",
        f"- confidence: {sentiment_signal.get('confidence')}",
        f"- reasoning: {sentiment_signal.get('reasoning')}",
    ]

    if fundamentals_signal is not None:
        prompt_sections += [
            "",
            "Fundamentals flag (minor, advisory input only — not a primary signal):",
            f"- unusual: {fundamentals_signal.get('unusual')}",
            f"- flags: {fundamentals_signal.get('flags')}",
            f"- reasoning: {fundamentals_signal.get('reasoning')}",
        ]

    if override_eligible:
        reasons_text = "; ".join(risk_check.get("reasons", [])) or "not specified"
        prompt_sections += [
            "",
            f"Risk manager note: {reasons_text} There is currently zero automatic headroom "
            "to add to this position. If the signals above still justify a buy, return "
            "action='buy' with the size_pct you'd genuinely recommend anyway — it will NOT "
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
