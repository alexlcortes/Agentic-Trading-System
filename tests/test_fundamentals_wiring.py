"""Fundamentals: agents/fundamentals_agent.py was built in Phase 2 but never
added to the graph, so fundamentals_signal was None in every live decision
and the portfolio manager's fundamentals section never appeared."""

import pandas as pd
import pytest

from agents import risk_manager
from config import settings
from orchestration import graph

FLAG = {"unusual": True, "flags": ["P/E far above sector"], "reasoning": "r"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(risk_manager, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "off")


@pytest.fixture
def pm_inputs(monkeypatch):
    monkeypatch.setattr(graph, "get_price_data", lambda t: pd.DataFrame({"Close": [100.0]}))
    monkeypatch.setattr(graph, "get_news_sentiment", lambda t: {"sentiment": "neutral", "confidence": 0.5})
    monkeypatch.setattr(
        graph, "get_technical_signal", lambda prices: {"signal": "hold", "confidence": 0.8, "reasoning": "t"}
    )
    seen = []

    def synthesize(ticker, **kwargs):
        seen.append(kwargs["fundamentals_signal"])
        return {"ticker": ticker, "action": "hold", "size_pct": 0.0, "confidence": 1.0, "reasoning": "r"}

    monkeypatch.setattr(graph, "synthesize_decision", synthesize)
    return seen


def _decide():
    portfolio = {"equity": 100_000.0, "open_positions": {}, "position_details": {}, "daily_realized_pnl": 0.0}
    return graph.decide_trade("ABBV", portfolio)


def test_fundamentals_reach_the_portfolio_manager(monkeypatch, pm_inputs):
    monkeypatch.setattr(graph, "get_fundamentals", lambda t: FLAG)
    state = _decide()
    assert pm_inputs == [FLAG]
    assert state["fundamentals_signal"] == FLAG


def test_a_fundamentals_failure_still_decides_without_them(monkeypatch, pm_inputs):
    def broken(ticker):
        raise RuntimeError("OpenAI timeout")

    monkeypatch.setattr(graph, "get_fundamentals", broken)
    state = _decide()
    assert pm_inputs == [None]
    assert state["portfolio_decision"]["action"] == "hold"


def test_a_hanging_fundamentals_fetch_times_out_and_still_decides(monkeypatch, pm_inputs):
    # yfinance .info has no timeout of its own; a PG call took 694s on
    # 2026-11-11. A stalled fetch must not hold up the ticker or the run.
    import threading
    import time

    release = threading.Event()

    def hangs(ticker):
        release.wait(30)
        return FLAG

    monkeypatch.setattr(settings, "FUNDAMENTALS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(graph, "get_fundamentals", hangs)
    started = time.monotonic()
    try:
        state = _decide()
    finally:
        release.set()  # let the abandoned worker thread finish
    assert time.monotonic() - started < 5
    assert pm_inputs == [None]
    assert state["fundamentals_signal"] is None
    assert state["portfolio_decision"]["action"] == "hold"


def test_a_fast_fetch_is_unaffected_by_the_deadline(monkeypatch, pm_inputs):
    monkeypatch.setattr(settings, "FUNDAMENTALS_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(graph, "get_fundamentals", lambda t: FLAG)
    assert _decide()["fundamentals_signal"] == FLAG
