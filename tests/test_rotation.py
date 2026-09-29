"""Rotation: with the portfolio at max_open_positions, a buy on any other
ticker used to end in a forced hold that nothing weighed against what was
held. The shadow rotation asks the portfolio manager whether one swap is
worth it, runs both legs past the risk manager, and logs it — never trades.
"""

import json
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

import run_daily
from agents import portfolio_manager, risk_manager
from agents.portfolio_manager import _rotation_schema
from config import settings
from config.settings import RiskLimits
from logs import audit_logger
from orchestration import rotation

HELD = ["AAPL", "GOOGL", "PG", "SPY", "XOM"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(risk_manager, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(audit_logger, "LOG_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(settings, "ROTATION_MODE", "shadow")


def _position(price=100.0):
    return {
        "qty": 45.0, "avg_entry_price": 104.0, "current_price": price,
        "unrealized_plpc": price / 104.0 - 1, "opened_at": "2026-09-22",
        "entry_run_id": "entry", "high_water_mark": 106.0,
    }


def _portfolio(held=HELD):
    return {
        "equity": 100_000.0,
        "open_positions": {t: 4_500.0 for t in held},
        "position_details": {t: _position() for t in held},
        "daily_realized_pnl": 0.0,
    }


def _entry(ticker, signal="hold", reason_code="hold_requested"):
    return {
        "ticker": ticker,
        "error": None,
        "final_state": {
            "technical_signal": {"signal": signal, "confidence": 0.8, "reasoning": "t"},
            "sentiment_signal": {"sentiment": "neutral", "confidence": 0.4, "reasoning": "s"},
            "fundamentals_signal": None,
            "risk_check": {"approved": reason_code == "hold_requested", "reason_code": reason_code},
            "price_data": pd.DataFrame({"Close": [400.0]}),
        },
    }


def _blocked(ticker):
    return _entry(ticker, "buy", risk_manager.REASON_MAX_OPEN_POSITIONS)


def _results():
    return [_entry(t) for t in HELD] + [_blocked("MSFT"), _entry("JPM", "sell", "no_existing_position_to_sell")]


def _fake_proposal(monkeypatch, action="swap", sell="GOOGL", buy="MSFT"):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {
            "action": action,
            "sell_ticker": sell if action == "swap" else None,
            "buy_ticker": buy if action == "swap" else None,
            "confidence": 0.7, "reasoning": "r",
        }

    monkeypatch.setattr(rotation, "propose_rotation", fake)
    return calls


def _run(results=None, portfolio=None):
    return rotation.shadow_rotation(
        results or _results(), RiskLimits(), get_state=lambda: portfolio or _portfolio()
    )


def _logged():
    lines = audit_logger.LOG_PATH.read_text().splitlines() if audit_logger.LOG_PATH.exists() else []
    return [json.loads(line) for line in lines]


def test_only_buys_blocked_by_a_full_portfolio_are_candidates():
    at_cap = _entry("PG", "buy", risk_manager.REASON_MAX_POSITION_PCT)
    assert [e["ticker"] for e in rotation.blocked_candidates(_results() + [at_cap])] == ["MSFT"]


def test_nothing_blocked_means_no_call_and_no_log(monkeypatch):
    calls = _fake_proposal(monkeypatch)
    assert _run(results=[_entry(t) for t in HELD]) is None
    assert calls == [] and _logged() == []


def test_mode_off_skips_it(monkeypatch):
    monkeypatch.setattr(settings, "ROTATION_MODE", "off")
    calls = _fake_proposal(monkeypatch)
    assert _run() is None
    assert calls == []


def test_candidates_and_holdings_reach_the_model_with_todays_signals(monkeypatch):
    calls = _fake_proposal(monkeypatch, action="no_swap")
    _run()
    assert [c["ticker"] for c in calls[0]["candidates"]] == ["MSFT"]
    assert [h["ticker"] for h in calls[0]["holdings"]] == HELD
    assert calls[0]["holdings"][0]["signals"]["technical_signal"]["signal"] == "hold"


def test_a_holding_with_no_run_today_has_no_signals(monkeypatch):
    calls = _fake_proposal(monkeypatch, action="no_swap")
    _run(results=[e for e in _results() if e["ticker"] != "XOM"])
    xom = next(h for h in calls[0]["holdings"] if h["ticker"] == "XOM")
    assert xom["signals"] is None


def test_no_swap_is_logged_without_risk_checks(monkeypatch):
    _fake_proposal(monkeypatch, action="no_swap")
    record = _run()
    assert record["proposal"]["action"] == "no_swap"
    assert "sell_check" not in record
    assert _logged()[0]["type"] == "rotation"


def test_a_swap_sells_the_whole_holding_and_buys_into_the_freed_slot(monkeypatch):
    _fake_proposal(monkeypatch)
    record = _run()
    assert record["sell_check"]["approved"] is True
    assert record["sell_check"]["adjusted_size"] == pytest.approx(0.045)
    assert record["buy_check"]["approved"] is True
    assert record["buy_check"]["adjusted_size"] == pytest.approx(0.05)
    assert record["would_execute"] is True
    assert _logged()[0]["proposal"]["buy_ticker"] == "MSFT"


def test_a_swap_the_risk_manager_would_block_says_so(monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", True)
    _fake_proposal(monkeypatch)
    record = _run()
    assert record["would_execute"] is False


def test_a_slot_freed_during_the_run_skips_the_model(monkeypatch):
    calls = _fake_proposal(monkeypatch)
    record = _run(portfolio=_portfolio(HELD[:4]))
    assert calls == []
    assert "no longer full" in record["skipped"]


def test_a_failure_is_logged_not_raised(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("openai down")

    monkeypatch.setattr(rotation, "propose_rotation", boom)
    record = _run()
    assert record["error"] == "openai down"
    assert _logged()[0]["error"] == "openai down"


# --- the model call ---------------------------------------------------------

def _fake_llm(monkeypatch, **fields):
    sent = {}

    def parse(**kwargs):
        sent.update(kwargs)
        parsed = kwargs["response_format"](**{"confidence": 0.6, "reasoning": "r", **fields})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)))
    monkeypatch.setattr(portfolio_manager, "OpenAI", lambda **kwargs: client)
    return sent


def _propose():
    signals = {
        "technical_signal": {"signal": "buy", "confidence": 0.8, "reasoning": "breakout"},
        "sentiment_signal": {"sentiment": "neutral", "confidence": 0.4, "reasoning": "s"},
        "fundamentals_signal": None,
    }
    return portfolio_manager.propose_rotation(
        candidates=[{"ticker": "MSFT", **signals}],
        holdings=[
            {"ticker": "GOOGL", "position": _position(), "signals": {**signals, "technical_signal": {"signal": "hold"}}},
            {"ticker": "XOM", "position": _position(), "signals": None},
        ],
        today=date(2026, 9, 29),
    )


def test_prompt_lays_out_candidates_and_holdings(monkeypatch):
    sent = _fake_llm(monkeypatch, action="no_swap", sell_ticker=None, buy_ticker=None)
    _propose()
    prompt = sent["messages"][1]["content"]
    assert "=== Candidate: MSFT ===" in prompt and "- reasoning: breakout" in prompt
    assert "=== Holding: GOOGL ===" in prompt
    assert "held 7 days (opened 2026-09-22)" in prompt
    assert "No signals today (not analyzed in this run)." in prompt


def test_a_swap_comes_back_as_plain_tickers(monkeypatch):
    _fake_llm(monkeypatch, action="swap", sell_ticker="GOOGL", buy_ticker="MSFT")
    proposal = _propose()
    assert (proposal["action"], proposal["sell_ticker"], proposal["buy_ticker"]) == ("swap", "GOOGL", "MSFT")


def test_a_swap_missing_a_side_is_no_swap(monkeypatch):
    _fake_llm(monkeypatch, action="swap", sell_ticker="GOOGL", buy_ticker=None)
    proposal = _propose()
    assert (proposal["action"], proposal["sell_ticker"]) == ("no_swap", None)


def test_schema_only_accepts_listed_tickers():
    schema = _rotation_schema(["MSFT"], ["GOOGL"])
    with pytest.raises(ValueError):
        schema(action="swap", sell_ticker="JPM", buy_ticker="MSFT", confidence=0.5, reasoning="r")
    with pytest.raises(ValueError):
        schema(action="swap", sell_ticker="GOOGL", buy_ticker="GOOGL", confidence=0.5, reasoning="r")


# --- the daily summary ------------------------------------------------------

def test_summary_names_blocked_buys():
    assert run_daily._blocked_lines(_results()) == [
        "BLOCKED (portfolio full): MSFT technical buy (confidence 0.8)"
    ]


def test_summary_line_for_a_swap(monkeypatch):
    _fake_proposal(monkeypatch)
    assert run_daily._rotation_lines(_run()) == [
        "ROTATION (shadow): SWAP GOOGL → MSFT (confidence 0.7) — no order placed"
    ]


def test_summary_line_for_a_swap_the_risk_manager_would_block(monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", True)
    _fake_proposal(monkeypatch)
    line = run_daily._rotation_lines(_run())[0]
    assert line.startswith("ROTATION (shadow): SWAP GOOGL → MSFT (confidence 0.7) — no order placed [risk check would block:")


def test_summary_line_for_no_swap(monkeypatch):
    _fake_proposal(monkeypatch, action="no_swap")
    assert run_daily._rotation_lines(_run()) == ["ROTATION (shadow): NO SWAP (confidence 0.7)"]
