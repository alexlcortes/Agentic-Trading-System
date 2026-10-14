"""A hold the risk manager forces is decided in code, with no LLM call, so it
carries no confidence. It used to log 1.0, which made up most of the
portfolio manager's logged confidences."""

from agents import portfolio_manager
from agents.risk_manager import REASON_MAX_OPEN_POSITIONS


def test_a_forced_hold_has_no_confidence(monkeypatch):
    def no_llm(**_kwargs):
        raise AssertionError("a forced hold must not call the LLM")

    monkeypatch.setattr(portfolio_manager, "OpenAI", no_llm)
    decision = portfolio_manager.synthesize_decision(
        ticker="ABBV",
        technical_signal={"signal": "buy"},
        sentiment_signal={"sentiment": "bullish"},
        risk_check={
            "approved": False, "adjusted_size": 0.0,
            "reasons": ["portfolio full"], "reason_code": REASON_MAX_OPEN_POSITIONS,
        },
    )
    assert decision["action"] == "hold"
    assert decision["confidence"] is None
