import logging
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field

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
    """
    if not risk_check.get("approved", False):
        reasons = "; ".join(risk_check.get("reasons", [])) or "not specified"
        return _forced_hold(
            ticker, f"Risk manager rejected this trade — forced to hold. Reasons: {reasons}"
        )

    ceiling = float(risk_check.get("adjusted_size", 0.0))
    if ceiling <= 0:
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

    prompt_sections += [
        "",
        f"Risk manager constraint: the MAXIMUM size_pct you may return is {ceiling:.4f} "
        f"({ceiling:.2%} of account equity). This is a hard ceiling, not a suggestion — "
        "you must never return a size_pct above this value. If you believe a smaller size "
        "is more appropriate given the signals above, return that smaller size instead.",
    ]

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a portfolio manager synthesizing multiple agent signals "
                    "into a single final trade decision. Weigh the technical signal as "
                    "the primary driver, sentiment as a secondary input, and fundamentals "
                    "(if given) as a minor, advisory input only. Your size_pct must never "
                    "exceed the hard ceiling stated in the prompt."
                ),
            },
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
