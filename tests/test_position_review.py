"""Position review: a held position whose technical signal is "hold" used to
end in a forced hold with no LLM call, and one whose signal is "buy" at the
position cap was only ever asked about adding — so nothing reconsidered
either. The review asks the portfolio manager hold-or-sell — shadow logs the
answer, live acts on a sell — and can only ever exit, never add.
"""

import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from pydantic import ValidationError

import run_daily
from agents import portfolio_manager, risk_manager
from agents.portfolio_manager import PositionReview
from config import settings
from config.settings import RiskLimits
from logs import audit_logger
from orchestration import graph


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(risk_manager, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(audit_logger, "LOG_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "shadow")


def _position():
    return {
        "qty": 12.0, "avg_entry_price": 362.40, "current_price": 346.70,
        "unrealized_plpc": -0.0433, "opened_at": "2026-09-22",
        "entry_run_id": "entry-run", "high_water_mark": 351.16,
    }


def _state(signal="hold", held=True, reason_code="hold"):
    return {
        "run_id": "test",
        "ticker": "GOOGL",
        "portfolio_state": {
            "equity": 100_000.0,
            "open_positions": {"GOOGL": 4_160.40} if held else {},
            "position_details": {"GOOGL": _position()} if held else {},
            "daily_realized_pnl": 0.0,
        },
        "risk_limits": RiskLimits(),
        "price_data": pd.DataFrame({"Close": [346.70]}),
        "technical_signal": {"signal": signal, "confidence": 0.83, "reasoning": "flat"},
        "sentiment_signal": {"sentiment": "neutral", "confidence": 0.4, "reasoning": "mixed"},
        "risk_check": {"approved": reason_code != risk_manager.REASON_MAX_POSITION_PCT,
                       "adjusted_size": 0.0, "reasons": [], "reason_code": reason_code},
    }


def _fake_review(monkeypatch, action="sell"):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {
            "ticker": kwargs["ticker"], "action": action,
            "size_pct": kwargs["sell_size_pct"] if action == "sell" else 0.0,
            "confidence": 0.7, "reasoning": "thesis gone",
        }

    monkeypatch.setattr(graph, "review_position", fake)
    return calls


def test_not_held_hold_is_still_a_forced_hold_with_no_review(monkeypatch):
    calls = _fake_review(monkeypatch)
    updates = graph.portfolio_manager_node(_state(held=False))
    assert calls == []
    assert "position_review" not in updates
    assert updates["portfolio_decision"]["action"] == "hold"


@pytest.mark.parametrize("signal", ["buy", "sell"])
def test_non_hold_signals_are_not_reviewed(monkeypatch, signal):
    calls = _fake_review(monkeypatch)
    monkeypatch.setattr(
        graph, "synthesize_decision",
        lambda **kw: {"ticker": "GOOGL", "action": signal, "size_pct": 0.01,
                      "confidence": 0.8, "reasoning": "x"},
    )
    updates = graph.portfolio_manager_node(_state(signal))
    assert calls == []
    assert "position_review" not in updates


def _pm_decides(monkeypatch, action, size_pct=0.0):
    monkeypatch.setattr(
        graph, "synthesize_decision",
        lambda **kw: {"ticker": "GOOGL", "action": action, "size_pct": size_pct,
                      "confidence": 0.8, "reasoning": "pm"},
    )


def _at_cap_buy():
    return _state("buy", reason_code=risk_manager.REASON_MAX_POSITION_PCT)


def test_an_at_cap_buy_is_reviewed(monkeypatch):
    calls = _fake_review(monkeypatch)
    _pm_decides(monkeypatch, "hold")
    updates = graph.portfolio_manager_node(_at_cap_buy())
    assert calls[0]["trigger"] == "at_cap_buy"
    assert updates["position_review"]["trigger"] == "at_cap_buy"
    assert updates["portfolio_decision"]["action"] == "hold"


def test_a_technical_hold_is_tagged_as_such(monkeypatch):
    calls = _fake_review(monkeypatch)
    updates = graph.portfolio_manager_node(_state())
    assert calls[0]["trigger"] == "technical_hold"
    assert updates["position_review"]["trigger"] == "technical_hold"


def test_a_buy_blocked_for_another_reason_is_not_reviewed(monkeypatch):
    calls = _fake_review(monkeypatch)
    _pm_decides(monkeypatch, "hold")
    updates = graph.portfolio_manager_node(_state("buy", reason_code="max_drawdown_exceeded"))
    assert calls == []
    assert "position_review" not in updates


def test_live_review_hold_does_not_cancel_an_override_buy(monkeypatch):
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "live")
    _fake_review(monkeypatch, action="hold")
    _pm_decides(monkeypatch, "buy", size_pct=0.02)
    updates = graph.portfolio_manager_node(_at_cap_buy())
    assert updates["portfolio_decision"]["action"] == "buy"
    assert updates["portfolio_decision"]["size_pct"] == 0.02


def test_live_review_sell_wins_over_an_at_cap_buy(monkeypatch):
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "live")
    _fake_review(monkeypatch, action="sell")
    _pm_decides(monkeypatch, "buy", size_pct=0.02)
    updates = graph.portfolio_manager_node(_at_cap_buy())
    assert updates["portfolio_decision"]["action"] == "sell"
    assert updates["portfolio_decision"]["size_pct"] == pytest.approx(0.041604)


def test_mode_off_skips_the_review(monkeypatch):
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "off")
    calls = _fake_review(monkeypatch)
    updates = graph.portfolio_manager_node(_state())
    assert calls == []
    assert "position_review" not in updates


def test_shadow_logs_a_sell_but_the_decision_stays_hold(monkeypatch):
    _fake_review(monkeypatch)
    updates = graph.portfolio_manager_node(_state())
    assert updates["position_review"]["mode"] == "shadow"
    assert updates["position_review"]["decision"]["action"] == "sell"
    assert updates["portfolio_decision"]["action"] == "hold"


def test_live_sells_the_whole_position(monkeypatch):
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "live")
    calls = _fake_review(monkeypatch)
    updates = graph.portfolio_manager_node(_state())
    assert calls[0]["sell_size_pct"] == pytest.approx(0.041604)
    assert updates["portfolio_decision"]["action"] == "sell"
    assert updates["portfolio_decision"]["size_pct"] == pytest.approx(0.041604)


def test_live_sell_passes_the_final_risk_check(monkeypatch):
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "live")
    _fake_review(monkeypatch)
    state = _state()
    state.update(graph.portfolio_manager_node(state))
    final = graph.risk_final_check_node(state)["risk_check_final"]
    assert final["approved"] is True
    assert graph.route_after_risk_final({**state, "risk_check_final": final}) == "proceed"


def test_live_hold_keeps_the_hold(monkeypatch):
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "live")
    _fake_review(monkeypatch, action="hold")
    updates = graph.portfolio_manager_node(_state())
    assert updates["portfolio_decision"]["action"] == "hold"
    assert updates["portfolio_decision"]["size_pct"] == 0.0


def test_a_blocked_sell_is_recorded_without_asking_the_model(monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", True)
    calls = _fake_review(monkeypatch)
    review = graph.portfolio_manager_node(_state())["position_review"]
    assert calls == []
    assert review["decision"] is None
    assert review["sell_check"]["approved"] is False


def test_a_failed_review_keeps_the_forced_hold(monkeypatch):
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "live")

    def boom(**kwargs):
        raise RuntimeError("openai down")

    monkeypatch.setattr(graph, "review_position", boom)
    updates = graph.portfolio_manager_node(_state())
    assert updates["position_review"]["error"] == "openai down"
    assert updates["portfolio_decision"]["action"] == "hold"


def test_entry_reasoning_is_looked_up_from_the_entry_run(monkeypatch):
    audit_logger.log_decision(
        run_id="entry-run", timestamp="2026-09-22", agent_outputs={},
        final_decision={"action": "buy", "reasoning": "MACD crossover"}, execution_result=None,
    )
    calls = _fake_review(monkeypatch)
    graph.portfolio_manager_node(_state())
    assert calls[0]["entry_reasoning"] == "MACD crossover"


def test_review_is_logged_on_the_run_entry(monkeypatch):
    _fake_review(monkeypatch)
    state = _state()
    state.update(graph.portfolio_manager_node(state))
    graph.log_and_end_node(state)
    entry = json.loads(audit_logger.LOG_PATH.read_text().splitlines()[-1])
    assert entry["agent_outputs"]["position_review"]["decision"]["action"] == "sell"


def _fake_llm(monkeypatch, action):
    sent = {}
    parsed = PositionReview(action=action, confidence=0.6, reasoning="r")
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])

    def parse(**kwargs):
        sent.update(kwargs)
        return completion

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)))
    monkeypatch.setattr(portfolio_manager, "OpenAI", lambda **kwargs: client)
    return sent


def _review(entry_reasoning="MACD crossover", trigger="technical_hold"):
    return portfolio_manager.review_position(
        ticker="GOOGL", position=_position(), sell_size_pct=0.04,
        technical_signal={"signal": "hold"}, sentiment_signal={"sentiment": "neutral"},
        entry_reasoning=entry_reasoning, today=date(2026, 9, 29), trigger=trigger,
    )


def test_review_prompt_shows_the_position_and_why_it_was_bought(monkeypatch):
    sent = _fake_llm(monkeypatch, "hold")
    _review()
    prompt = sent["messages"][1]["content"]
    assert "12 shares, entry $362.40, now $346.70 (unrealized -4.33%)" in prompt
    assert "held 7 days (opened 2026-09-22)" in prompt
    assert "1.27% below its highest close since entry ($351.16)" in prompt
    assert "why it was bought: MACD crossover" in prompt


@pytest.mark.parametrize("trigger", ["technical_hold", "at_cap_buy"])
def test_review_prompt_says_why_the_review_is_running(monkeypatch, trigger):
    sent = _fake_llm(monkeypatch, "hold")
    _review(trigger=trigger)
    assert "technical signal says hold" not in sent["messages"][0]["content"]
    note = portfolio_manager.REVIEW_TRIGGER_NOTES[trigger]
    assert f"Why this review: {note}" in sent["messages"][1]["content"]


def test_missing_entry_reasoning_says_so(monkeypatch):
    sent = _fake_llm(monkeypatch, "hold")
    _review(entry_reasoning=None)
    assert "why it was bought: not recorded" in sent["messages"][1]["content"]


@pytest.mark.parametrize("action, size", [("sell", 0.04), ("hold", 0.0)])
def test_size_is_set_in_code_not_by_the_model(monkeypatch, action, size):
    _fake_llm(monkeypatch, action)
    assert _review()["size_pct"] == size


def test_review_schema_cannot_return_a_buy():
    with pytest.raises(ValidationError):
        PositionReview(action="buy", confidence=0.9, reasoning="x")


def test_summary_line_for_a_shadow_sell():
    review = {"mode": "shadow", "decision": {"action": "sell", "confidence": 0.7}}
    lines = run_daily._position_review_lines(
        [{"ticker": "GOOGL", "final_state": {"position_review": review}, "error": None}]
    )
    assert lines == ["POSITION REVIEW (shadow): GOOGL SELL (confidence 0.7) — no order placed"]


def test_summary_line_labels_an_at_cap_buy_review():
    review = {"mode": "shadow", "trigger": "at_cap_buy",
              "decision": {"action": "hold", "confidence": 0.8}}
    lines = run_daily._position_review_lines(
        [{"ticker": "PG", "final_state": {"position_review": review}, "error": None}]
    )
    assert lines == ["POSITION REVIEW (shadow, at-cap buy): PG HOLD (confidence 0.8)"]
