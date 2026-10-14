"""The backtest used to read the portfolio once per day and never count that
day's queued buys, so six buys on one day all saw the same position count
and it could open past max_open_positions. It also went in watchlist order.
It now decides every ticker, then commits strongest buy first, as run_daily
does."""

from datetime import date

import pandas as pd
import pytest

from agents import risk_manager
from backtest import runner
from config import settings
from config.settings import RiskLimits

# Watchlist order, with the portfolio manager's confidence for each buy.
CONFIDENCE = {"A": 0.70, "B": 0.95, "C": 0.75, "D": 0.90, "E": 0.80, "F": 0.85}
EVAL_START, EVAL_END = date(2026, 3, 2), date(2026, 3, 7)  # Mon-Fri, 5 trading days


@pytest.fixture
def backtest(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(risk_manager, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")

    days = pd.bdate_range(end="2026-03-06", periods=70)
    prices = pd.DataFrame({"Open": 100.0, "Close": 100.0}, index=days)
    monkeypatch.setattr(runner, "_fetch_all", lambda tickers, start, end: {t: prices for t in tickers})
    monkeypatch.setattr(
        runner, "get_technical_signal", lambda df: {"signal": "buy", "confidence": 0.8, "reasoning": "t"}
    )

    def synthesize(ticker, risk_check, **_):
        if not risk_check["approved"]:
            return {"ticker": ticker, "action": "hold", "size_pct": 0.0, "confidence": None, "reasoning": "r"}
        return {"ticker": ticker, "action": "buy", "size_pct": 0.04, "confidence": CONFIDENCE[ticker], "reasoning": "r"}

    monkeypatch.setattr(runner, "synthesize_decision", synthesize)
    logged = []
    monkeypatch.setattr(runner, "log_decision", lambda **entry: logged.append(entry))

    def run():
        runner.run_backtest(
            list(CONFIDENCE), EVAL_START, EVAL_END, risk_limits=RiskLimits(max_open_positions=3)
        )
        return [
            e["final_decision"]["ticker"]
            for e in logged
            if (e["execution_result"] or {}).get("status") == "pending_fill_next_open"
        ]

    return run


def test_only_max_open_positions_are_ever_opened(backtest):
    queued = backtest()
    assert len(set(queued)) == 3


def test_the_slots_go_to_the_strongest_buys(backtest):
    assert sorted(set(backtest())) == ["B", "D", "F"]
