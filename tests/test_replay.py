"""The golden test's harness (orchestration/replay.py): a day run_daily
records must replay to exactly what that day did, with the run date frozen,
and a change to the code must show up as a different snapshot.
"""

import json
from datetime import date, timedelta
from functools import partial
from types import SimpleNamespace

import pandas as pd
import pytest

import run_daily
from agents import exit_rules, portfolio_manager, risk_manager
from agents.portfolio_manager import PortfolioDecision, PositionReview
from config import settings
from config.settings import RiskLimits
from execution import alpaca_executor
from logs import audit_logger
from orchestration import confirmation, graph, replay, rotation

RUN_DATE = date(2026, 10, 1)
HELD = ["AAPL", "GOOGL", "PG", "SPY", "XOM"]
BUYS = {"MSFT": 0.75, "V": 0.72, "ABBV": 0.90}
WATCHLIST = [*HELD, *BUYS]


class RunDate(date):
    @classmethod
    def today(cls):
        return cls(RUN_DATE.year, RUN_DATE.month, RUN_DATE.day)


def _limits():
    # One open slot: ABBV takes it, MSFT and V become rotation candidates.
    return RiskLimits(watchlist=WATCHLIST, max_open_positions=6)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(risk_manager, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(audit_logger, "LOG_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(confirmation, "LAST_PROPOSALS_PATH", tmp_path / "last_proposals.json")
    monkeypatch.setattr(run_daily, "SUMMARY_LOG_PATH", tmp_path / "daily_summary.log")
    monkeypatch.setattr(run_daily, "LAST_COMPLETED_RUN_PATH", tmp_path / "last_completed_run.json")
    monkeypatch.setattr(settings, "POSITION_REVIEW_MODE", "live")
    monkeypatch.setattr(settings, "ROTATION_MODE", "shadow")
    monkeypatch.setattr(settings, "EXIT_REVIEW_MODE", "shadow")
    monkeypatch.setattr(run_daily, "RiskLimits", _limits)


def _portfolio():
    position = {
        "qty": 40.0, "avg_entry_price": 100.0, "current_price": 100.0, "unrealized_plpc": 0.0,
        # Held 31 days on the run date: the exit rules flag it for review.
        "opened_at": (RUN_DATE - timedelta(days=31)).isoformat(),
        "entry_run_id": "entry", "high_water_mark": 100.0,
    }
    return {
        "equity": 100_000.0,
        "open_positions": {t: 4_000.0 for t in HELD},
        "position_details": {t: dict(position) for t in HELD},
        "daily_realized_pnl": 0.0,
    }


def _model(**kwargs):
    """The portfolio manager's model: buys the candidates, wants out of XOM,
    and would swap PG for MSFT."""
    fmt = kwargs["response_format"]
    ticker = kwargs["messages"][1]["content"].splitlines()[0].removeprefix("Ticker: ")
    if fmt is PortfolioDecision:
        parsed = fmt(ticker=ticker, action="buy", size_pct=0.04, confidence=BUYS[ticker], reasoning="r")
    elif fmt is PositionReview:
        parsed = fmt(
            case_for_owning="f", case_against_owning="a",
            action="sell" if ticker == "XOM" else "hold", confidence=0.7, reasoning="r",
        )
    else:
        parsed = fmt(action="swap", sell_ticker="PG", buy_ticker="MSFT", confidence=0.8, reasoning="r")
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])


def _record(monkeypatch) -> dict:
    """One real run_once on RUN_DATE with the outside world faked, as the
    daily job would record it."""
    portfolio = _portfolio()

    def submit(ticker, side, qty, run_id, reference_price):
        if side == "buy":
            portfolio["open_positions"][ticker] = qty * reference_price
        return {"status": "queued_for_next_session", "side": side, "requested_qty": float(qty), "ticker": ticker}

    with monkeypatch.context() as mp:
        for module in (replay, confirmation, exit_rules, run_daily):
            mp.setattr(module, "date", RunDate)
        confirmation.save_current(["XOM"], None, today=RUN_DATE - timedelta(days=1))

        mp.setattr(run_daily, "_woke_up_too_late", lambda: False)
        mp.setattr(run_daily, "_startup_safety_check", lambda: True)
        mp.setattr(run_daily, "_wait_for_network", lambda: None)
        mp.setattr(run_daily, "reconcile_pending_orders", lambda: [])
        mp.setattr(run_daily, "_get_client", lambda: SimpleNamespace(get_clock=lambda: SimpleNamespace(is_open=False)))
        mp.setattr(run_daily, "get_portfolio_state", lambda: json.loads(json.dumps(portfolio)))
        mp.setattr(
            run_daily, "shadow_rotation",
            partial(rotation.shadow_rotation, get_state=lambda: json.loads(json.dumps(portfolio)), today=RUN_DATE),
        )
        mp.setattr(alpaca_executor, "submit_order", submit)
        mp.setattr(settings, "auto_execute", False)

        mp.setattr(graph, "get_news_sentiment", lambda t: {"sentiment": "neutral", "confidence": 0.5})
        mp.setattr(graph, "get_fundamentals", lambda t: None)
        # Held tickers get a technical hold, so the position review runs.
        mp.setattr(
            graph, "get_technical_signal",
            lambda prices: {"signal": prices.attrs["signal"], "confidence": 0.8, "reasoning": "t"},
        )

        def price_data(ticker):
            frame = pd.DataFrame({"Close": [100.0]})
            frame.attrs["signal"] = "hold" if ticker in HELD else "buy"
            return frame

        mp.setattr(graph, "get_price_data", price_data)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=_model)))
        mp.setattr(portfolio_manager, "OpenAI", lambda **kwargs: client)

        run_daily.run_once()

    return json.loads((replay.REPLAY_DIR / f"{RUN_DATE.isoformat()}.json").read_text())


def test_the_recorded_day_did_what_the_scenario_says(monkeypatch):
    actual = _record(monkeypatch)["actual"]
    # ABBV, the strongest buy, takes the one open slot; XOM's review sell is
    # its second day in a row, so live mode sells it.
    assert actual["orders"] == [
        {"ticker": "ABBV", "side": "buy", "qty": 40.0},
        {"ticker": "XOM", "side": "sell", "qty": 40.0},
    ]
    assert actual["tickers"]["MSFT"]["final_check"] == risk_manager.REASON_MAX_OPEN_POSITIONS
    assert actual["tickers"]["XOM"]["review"]["confirmed"] is True
    assert actual["rotation"]["proposal"] == {"action": "swap", "sell_ticker": "PG", "buy_ticker": "MSFT"}
    assert set(actual["exits"].values()) == {exit_rules.REASON_MAX_HOLDING_DAYS}


def test_a_replay_reproduces_the_recorded_day(monkeypatch):
    fixture = _record(monkeypatch)
    # Replayed today, not on RUN_DATE: the confirmation and the 30-day
    # review only come out the same if the replay freezes the date.
    assert replay.replay_day(fixture) == fixture["actual"]


def test_a_code_change_changes_the_snapshot(monkeypatch):
    fixture = _record(monkeypatch)
    monkeypatch.setattr(run_daily, "RiskLimits", lambda: RiskLimits(watchlist=WATCHLIST, max_open_positions=7))
    orders = [o["ticker"] for o in replay.replay_day(fixture)["orders"]]
    assert orders == ["ABBV", "MSFT", "XOM"]


def test_a_question_the_recorded_run_never_asked_shows_as_an_error(monkeypatch):
    fixture = _record(monkeypatch)
    del fixture["answers"]["decision:MSFT"]
    outcome = replay.replay_day(fixture)["tickers"]["MSFT"]
    assert outcome == {"error": "no recorded model answer for decision:MSFT"}


def test_a_ticker_added_since_the_recording_has_no_data(monkeypatch):
    fixture = _record(monkeypatch)
    monkeypatch.setattr(run_daily, "RiskLimits", lambda: RiskLimits(watchlist=[*WATCHLIST, "LIN"], max_open_positions=6))
    assert replay.replay_day(fixture)["tickers"]["LIN"] == {"error": "no recorded data for LIN"}


def test_recording_never_breaks_the_run(monkeypatch):
    monkeypatch.setattr(replay, "snapshot", lambda *a: 1 / 0)
    _record_path = replay.REPLAY_DIR / f"{RUN_DATE.isoformat()}.json"
    with pytest.raises(FileNotFoundError):
        _record(monkeypatch)
    assert not _record_path.exists()
    assert portfolio_manager.recorded_answers is None
