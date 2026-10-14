"""Buy ranking: run_daily used to run each ticker's full cycle in watchlist
order, so the open position slots went to whichever buys came first. On
2026-10-13 MSFT (technical 0.72) and V (0.74) took the last slots while
ABBV (0.90) was blocked. Now every ticker is decided first, then committed
strongest buy first.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

import run_daily
from agents import risk_manager
from config import settings
from config.settings import RiskLimits
from execution import alpaca_executor
from logs import audit_logger
from orchestration import confirmation, graph, rotation

HELD = ["AAPL", "GOOGL", "PG", "SPY", "XOM"]
PRICE = 100.0

# The 2026-10-13 buys, in watchlist order, with portfolio manager confidence.
BUYS = {"MSFT": 0.75, "V": 0.72, "LIN": 0.89, "AMT": 0.80, "ABBV": 0.90}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(risk_manager, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(audit_logger, "LOG_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(confirmation, "LAST_PROPOSALS_PATH", tmp_path / "last_proposals.json")
    monkeypatch.setattr(run_daily, "SUMMARY_LOG_PATH", tmp_path / "daily_summary.log")
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "off")
    monkeypatch.setattr(settings, "ROTATION_MODE", "off")


def _decided(ticker, action, confidence):
    return {"ticker": ticker, "portfolio_decision": {"action": action, "confidence": confidence}}


def test_buys_commit_first_strongest_first():
    decided = [
        _decided("MSFT", "buy", 0.75),
        _decided("JPM", "hold", 1.0),
        _decided("V", "buy", 0.72),
        _decided("UNP", "sell", 0.80),
        _decided("ABBV", "buy", 0.90),
    ]
    assert [s["ticker"] for s in run_daily._rank_decisions(decided)] == ["ABBV", "MSFT", "V", "JPM", "UNP"]


def test_ties_keep_watchlist_order():
    decided = [_decided("A", "buy", 0.8), _decided("B", "buy", 0.8)]
    assert [s["ticker"] for s in run_daily._rank_decisions(decided)] == ["A", "B"]


def test_ranking_line_lists_buys_in_commit_order():
    ranked = run_daily._rank_decisions([_decided("A", "buy", 0.7), _decided("B", "buy", 0.9), _decided("C", "hold", 1.0)])
    assert run_daily._ranking_lines(ranked) == ["BUY RANKING (portfolio manager confidence): B 0.9, A 0.7"]


def test_buy_blocked_at_commit_is_a_rotation_candidate():
    entry = {
        "ticker": "V", "error": None,
        "final_state": {
            "risk_check": {"reason_code": risk_manager.REASON_WITHIN_LIMITS},
            "risk_check_final": {"reason_code": risk_manager.REASON_MAX_OPEN_POSITIONS},
        },
    }
    assert rotation.blocked_candidates([entry]) == [entry]


class _Broker:
    """Portfolio state that counts submitted buys as exposure, as pending
    orders do in get_portfolio_state."""

    def __init__(self):
        self.open_positions = {t: 4_000.0 for t in HELD}
        self.submitted = []

    def state(self):
        return {
            "equity": 100_000.0,
            "open_positions": dict(self.open_positions),
            "position_details": {},
            "daily_realized_pnl": 0.0,
        }

    def submit(self, ticker, side, qty, run_id, reference_price):
        self.submitted.append(ticker)
        self.open_positions[ticker] = qty * reference_price
        return {"status": "queued_for_next_session", "side": side, "requested_qty": qty, "ticker": ticker}


@pytest.fixture
def run(monkeypatch):
    """run_once over the real decide and commit graphs, with the agents and
    the broker faked."""
    broker = _Broker()
    watchlist = ["AAPL", *BUYS]
    monkeypatch.setattr(run_daily, "RiskLimits", lambda: RiskLimits(watchlist=watchlist, max_open_positions=8))
    monkeypatch.setattr(run_daily, "_woke_up_too_late", lambda: False)
    monkeypatch.setattr(run_daily, "_startup_safety_check", lambda: True)
    monkeypatch.setattr(run_daily, "_wait_for_network", lambda: None)
    monkeypatch.setattr(run_daily, "reconcile_pending_orders", lambda: [])
    monkeypatch.setattr(run_daily, "_get_client", lambda: SimpleNamespace(get_clock=lambda: SimpleNamespace(is_open=False)))
    monkeypatch.setattr(run_daily, "_shadow_exit_checks", lambda limits: [])
    monkeypatch.setattr(run_daily, "get_portfolio_state", broker.state)
    monkeypatch.setattr(alpaca_executor, "submit_order", broker.submit)
    monkeypatch.setattr(settings, "auto_execute", False)  # run_once sets it for paper

    monkeypatch.setattr(graph, "get_price_data", lambda t: pd.DataFrame({"Close": [PRICE]}))
    monkeypatch.setattr(graph, "get_news_sentiment", lambda t: {"sentiment": "neutral", "confidence": 0.5})
    monkeypatch.setattr(
        graph, "get_technical_signal",
        lambda prices: {"signal": "buy", "confidence": 0.8, "reasoning": "t"},
    )

    def synthesize(ticker, **_):
        if ticker not in BUYS:
            return {"ticker": ticker, "action": "hold", "size_pct": 0.0, "confidence": 1.0, "reasoning": "r"}
        return {"ticker": ticker, "action": "buy", "size_pct": 0.04, "confidence": BUYS[ticker], "reasoning": "r"}

    monkeypatch.setattr(graph, "synthesize_decision", synthesize)

    def go():
        return broker, run_daily.run_once()

    return go


def test_open_slots_go_to_the_strongest_buys(run):
    broker, results = run()
    # 5 held, 8 allowed: the 3 strongest of the 5 buys get the slots.
    assert broker.submitted == ["ABBV", "LIN", "AMT"]

    by_ticker = {e["ticker"]: e["final_state"] for e in results}
    for ticker in ("MSFT", "V"):
        state = by_ticker[ticker]
        assert state["risk_check"]["reason_code"] == risk_manager.REASON_WITHIN_LIMITS
        assert state["risk_check_final"]["reason_code"] == risk_manager.REASON_MAX_OPEN_POSITIONS
        assert state["portfolio_decision"]["action"] == "hold"
        assert state.get("execution_result") is None
    assert [e["ticker"] for e in rotation.blocked_candidates(results)] == ["MSFT", "V"]


def test_every_ticker_is_logged_once(run):
    _, results = run()
    logged = [line for line in audit_logger.LOG_PATH.read_text().splitlines()]
    assert len(logged) == len(results) == 6
    assert all(e["error"] is None for e in results)


def test_a_failed_decision_does_not_stop_the_others(run, monkeypatch):
    real = graph.get_price_data

    def flaky(ticker):
        if ticker == "LIN":
            raise ValueError("no price data")
        return real(ticker)

    monkeypatch.setattr(graph, "get_price_data", flaky)
    broker, results = run()
    assert broker.submitted == ["ABBV", "AMT", "MSFT"]
    assert results[-1] == {"ticker": "LIN", "final_state": None, "error": "no price data"}
