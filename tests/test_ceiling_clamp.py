"""Regression tests for 2026-10-12: GOOGL's ceiling was 0.004364, the prompt
showed it as 0.0044 (:.4f rounds up), the model returned 0.0044, and the
clamp logged "size_pct=0.0044 exceeding ceiling=0.0044" as if the model had
disobeyed the constraint.
"""

import logging
from types import SimpleNamespace

from agents import portfolio_manager
from agents.portfolio_manager import PortfolioDecision

CEILING = 0.004364


def _fake_llm(monkeypatch, size_pct):
    sent = {}
    parsed = PortfolioDecision(
        ticker="GOOGL", action="buy", size_pct=size_pct, confidence=0.72, reasoning="r"
    )
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])

    def parse(**kwargs):
        sent.update(kwargs)
        return completion

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)))
    monkeypatch.setattr(portfolio_manager, "OpenAI", lambda **kwargs: client)
    return sent


def _decide():
    return portfolio_manager.synthesize_decision(
        ticker="GOOGL",
        technical_signal={"signal": "buy"},
        sentiment_signal={"sentiment": "neutral"},
        risk_check={"approved": True, "adjusted_size": CEILING},
    )


def test_prompt_states_the_exact_ceiling(monkeypatch):
    sent = _fake_llm(monkeypatch, CEILING)
    _decide()
    prompt = sent["messages"][1]["content"]
    assert "MAXIMUM size_pct you may return is 0.004364 " in prompt


def test_float_noise_above_ceiling_clamps_without_warning(monkeypatch, caplog):
    _fake_llm(monkeypatch, CEILING + 1e-9)
    with caplog.at_level(logging.WARNING, logger=portfolio_manager.__name__):
        result = _decide()
    assert result["size_pct"] == CEILING
    assert "NOTE" not in result["reasoning"]
    assert caplog.records == []


def test_real_overshoot_still_warns(monkeypatch, caplog):
    _fake_llm(monkeypatch, 0.0044)
    with caplog.at_level(logging.WARNING, logger=portfolio_manager.__name__):
        result = _decide()
    assert result["size_pct"] == CEILING
    assert "size_pct=0.004400 exceeding ceiling=0.004364" in caplog.text
