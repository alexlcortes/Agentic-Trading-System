import json

import pytest

import run_daily
from config import settings
from config.settings import RiskLimits
from logs import audit_logger


def _position(plpc, opened_at="2026-09-20"):
    entry = 100.0
    current = entry * (1 + plpc)
    return {
        "qty": 10.0, "avg_entry_price": entry, "current_price": current,
        "unrealized_plpc": plpc, "opened_at": opened_at, "entry_run_id": None,
        "high_water_mark": max(entry, current),
    }


@pytest.fixture
def trades_log(tmp_path, monkeypatch):
    path = tmp_path / "trades.jsonl"
    monkeypatch.setattr(audit_logger, "LOG_PATH", path)
    monkeypatch.setattr(settings, "EXIT_REVIEW_MODE", "shadow")
    return path


def _with_positions(monkeypatch, details):
    monkeypatch.setattr(run_daily, "get_portfolio_state", lambda: {"position_details": details})


def test_every_held_position_is_logged(trades_log, monkeypatch):
    _with_positions(monkeypatch, {"XOM": _position(-0.09), "PG": _position(0.01)})
    run_daily._shadow_exit_checks(RiskLimits())

    entries = {e["ticker"]: e for e in map(json.loads, trades_log.read_text().splitlines())}
    assert set(entries) == {"XOM", "PG"}
    assert entries["XOM"]["type"] == "exit_check"
    assert entries["XOM"]["mode"] == "shadow"
    assert entries["XOM"]["would_exit"] is True
    assert entries["XOM"]["reason_code"] == "stop_loss"
    assert entries["PG"]["would_exit"] is False


def test_off_mode_does_nothing(trades_log, monkeypatch):
    monkeypatch.setattr(settings, "EXIT_REVIEW_MODE", "off")
    _with_positions(monkeypatch, {"XOM": _position(-0.09)})
    assert run_daily._shadow_exit_checks(RiskLimits()) == []
    assert not trades_log.exists()


def test_failure_never_raises(trades_log, monkeypatch):
    def broken():
        raise RuntimeError("broker down")
    monkeypatch.setattr(run_daily, "get_portfolio_state", broken)
    checks = run_daily._shadow_exit_checks(RiskLimits())
    assert run_daily._exit_summary_lines(checks) == ["EXIT RULES (shadow): ERROR — see run_daily.log"]


def test_summary_names_triggers_and_says_no_order(trades_log, monkeypatch):
    _with_positions(monkeypatch, {"XOM": _position(-0.09), "PG": _position(0.01)})
    lines = run_daily._exit_summary_lines(run_daily._shadow_exit_checks(RiskLimits()))
    assert len(lines) == 1
    assert "XOM WOULD EXIT [stop_loss]" in lines[0]
    assert "no order placed" in lines[0]


def test_summary_when_nothing_triggers(trades_log, monkeypatch):
    _with_positions(monkeypatch, {"PG": _position(0.01)})
    lines = run_daily._exit_summary_lines(run_daily._shadow_exit_checks(RiskLimits()))
    assert lines == ["EXIT RULES (shadow): 1 positions checked, none triggered (PG +1.00%)"]


def _flagged(ticker="AAPL"):
    result = {"exit": False, "review": True, "reason_code": "max_holding_days",
              "reasons": ["held 32 days, at/over the 30-day review threshold"]}
    return [{"ticker": ticker, "position": _position(0.03), "result": result}]


def _entry(review=None, signal="hold", action="hold", error=None):
    if error:
        return {"ticker": "AAPL", "final_state": None, "error": error}
    final_state = {"technical_signal": {"signal": signal}, "portfolio_decision": {"action": action}}
    if review is not None:
        final_state["position_review"] = review
    return {"ticker": "AAPL", "final_state": final_state, "error": None}


def _flag_line(entries):
    [line] = run_daily._exit_summary_lines(_flagged(), entries)
    prefix = ("EXIT RULES (shadow): AAPL REVIEW [max_holding_days] "
              "held 32 days, at/over the 30-day review threshold — ")
    assert line.startswith(prefix)
    return line[len(prefix):]


def test_a_holding_flag_reports_the_reviews_answer():
    review = {"mode": "shadow", "decision": {"action": "hold", "confidence": 0.84}}
    assert _flag_line([_entry(review)]) == "answered by position review: HOLD (confidence 0.84)"


def test_a_holding_flag_reports_a_review_sell():
    review = {"mode": "shadow", "decision": {"action": "sell", "confidence": 0.7}}
    assert _flag_line([_entry(review)]) == "answered by position review: SELL (confidence 0.7)"


def test_a_holding_flag_says_when_the_portfolio_manager_decided_instead():
    assert _flag_line([_entry(signal="sell", action="hold")]) == (
        "not reviewed — technical signal was sell, so the portfolio manager decided it (hold)"
    )


def test_a_holding_flag_reports_a_failed_or_skipped_review():
    assert _flag_line([_entry({"mode": "shadow", "error": "openai down", "decision": None})]) == (
        "position review failed — openai down"
    )
    skipped = {"mode": "shadow", "decision": None, "sell_check": {"reasons": ["kill switch active"]}}
    assert _flag_line([_entry(skipped)]) == "position review skipped — kill switch active"


def test_a_holding_flag_with_no_run_or_a_failed_run():
    assert _flag_line([]) == "not reviewed — no run for this ticker today"
    assert _flag_line([_entry(error="boom")]) == "not reviewed — this ticker's run failed"
