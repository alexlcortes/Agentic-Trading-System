"""Regression tests for 2026-09-25: the portfolio manager returned
size_pct=5.0 (meaning "5%") on the override path, the notification showed
"buy 5.0 of equity", a human approved it, and execution sized 1462 AAPL
(~$498k on ~$100k equity). Only Alpaca's buying-power check stopped it.
"""

import pandas as pd
import pytest
from pydantic import ValidationError

from agents import human_override, risk_manager
from agents.portfolio_manager import PortfolioDecision
from config import settings
from config.settings import RiskLimits
from orchestration import graph


@pytest.fixture(autouse=True)
def no_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(risk_manager, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")


def _state(size_pct):
    # AAPL already at the 5% cap, so any buy is max_position_pct_exceeded.
    return {
        "run_id": "test",
        "ticker": "AAPL",
        "portfolio_state": {
            "equity": 100_000.0,
            "open_positions": {"AAPL": 5_000.0},
            "daily_realized_pnl": 0.0,
        },
        "risk_limits": RiskLimits(),
        "price_data": pd.DataFrame({"Close": [340.88]}),
        "portfolio_decision": {
            "ticker": "AAPL",
            "action": "buy",
            "size_pct": size_pct,
            "confidence": 0.78,
            "reasoning": "test",
        },
    }


def test_schema_rejects_percentage_written_as_whole_number():
    with pytest.raises(ValidationError):
        PortfolioDecision(ticker="AAPL", action="buy", size_pct=5.0, confidence=0.8, reasoning="x")


def test_oversized_override_is_never_sent_to_a_human(monkeypatch):
    calls = []
    monkeypatch.setattr(graph, "request_override", lambda **kw: calls.append(kw))

    updates = graph.risk_final_check_node(_state(5.0))

    assert calls == []
    assert updates["human_override_result"]["approved"] is False
    assert updates["portfolio_decision"]["action"] == "hold"
    assert updates["portfolio_decision"]["size_pct"] == 0.0


def test_override_just_over_the_cap_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(graph, "request_override", lambda **kw: calls.append(kw))

    graph.risk_final_check_node(_state(RiskLimits().max_override_size_pct + 0.0001))

    assert calls == []


def test_sane_override_sends_shares_and_dollars(monkeypatch):
    calls = []

    def fake_request_override(**kw):
        calls.append(kw)
        return {"approved": True, "responder": "test", "reason": "ok"}

    monkeypatch.setattr(graph, "request_override", fake_request_override)

    updates = graph.risk_final_check_node(_state(0.02))

    assert len(calls) == 1
    assert calls[0]["requested_qty"] == 5  # 0.02 * 100k // 340.88
    assert calls[0]["requested_notional"] == pytest.approx(5 * 340.88)
    assert updates["risk_check_final"]["approved"] is True


def test_notification_payload_shows_real_consequence(monkeypatch):
    sent = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"approved": False, "responder": "test", "reason": "denied"}

    def fake_post(url, json, headers, timeout):
        sent.update(json)
        return FakeResponse()

    monkeypatch.setattr(settings, "ENABLE_HUMAN_OVERRIDE", True)
    monkeypatch.setattr(settings, "N8N_OVERRIDE_WEBHOOK_URL", "http://n8n.test/hook")
    monkeypatch.setattr(settings, "N8N_OVERRIDE_SECRET", "secret")
    monkeypatch.setattr(human_override.httpx, "post", fake_post)
    monkeypatch.setattr(human_override, "log_human_override", lambda **kw: None)

    human_override.request_override(
        run_id="test", ticker="AAPL", requested_size_pct=0.02, requested_qty=5,
        requested_notional=1704.40, equity=100_000.0, existing_pct=0.05,
        max_position_pct=0.05, reasoning="test",
    )

    assert sent["message"].startswith("AAPL: buy 5 sh (~$1,704, 2.00% of equity)")
    assert "cap: 5.00%" in sent["message"]
    assert sent["requested_qty"] == 5
