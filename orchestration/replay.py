"""Golden test: record what each daily run took from outside the code, then
replay those days through whatever the code is now and compare the outcome
with a saved snapshot (tests/test_golden.py, built by scripts/build_golden.py).

trades.jsonl alone can't be replayed: it has no portfolio state, no price
the orders were sized at, and only the final decision — after the risk check
may have overridden or resized what the model actually answered. So every
run_daily run also writes logs/replay/<date>.json with:

    portfolio_state      as fetched before the run (pending buys included)
    previous_proposals   logs/last_proposals.json as the run found it
    tickers              each ticker's last close and technical, sentiment
                         and fundamentals signals, or the error it failed with
    answers              every portfolio manager model answer, raw
                         (agents.portfolio_manager.recorded_answers)
    human_overrides      any override answer, by ticker
    actual               what the run did, in the same shape as a replay

What gets replayed is everything in code: the risk checks, sizing, forced
holds, ranking, slot allocation, position review, confirmation, rotation and
exit rules, through the real run_daily.run_once. The agents' outputs and the
model's answers are fixed inputs, so a change to a prompt or to
sentiment_agent's confidence caps is not covered here.

A change that makes the code ask the model something the recorded run never
asked (say, a higher cap turns a forced hold into a real decision) has no
answer to replay: that ticker or review fails with "no recorded model
answer", which shows up in the snapshot diff instead of being guessed at.
"""

import copy
import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from agents import portfolio_manager
from orchestration import confirmation

logger = logging.getLogger(__name__)

REPLAY_DIR = Path(__file__).parent.parent / "logs" / "replay"
FIXTURE_VERSION = 1


class NoRecordedAnswer(RuntimeError):
    pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent.parent, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return None


class DayRecorder:
    """Started after run_once's preflight checks, saved after the rotation."""

    def __init__(self):
        self.previous_proposals = _read_json(confirmation.LAST_PROPOSALS_PATH)
        portfolio_manager.recorded_answers = {}

    def save(
        self, limits, portfolio_state: dict, decided: list[dict], failed: list[dict],
        results: list[dict], rotation: dict | None, exit_checks: list[dict],
    ) -> None:
        """Never raises: recording must not cost a day of trading."""
        try:
            tickers = {
                state["ticker"]: {
                    "last_close": float(state["price_data"]["Close"].iloc[-1]),
                    "technical_signal": state.get("technical_signal"),
                    "sentiment_signal": state.get("sentiment_signal"),
                    "fundamentals_signal": state.get("fundamentals_signal"),
                }
                for state in decided
            }
            tickers.update({entry["ticker"]: {"error": entry["error"]} for entry in failed})
            human_overrides = {
                entry["ticker"]: entry["final_state"]["human_override_result"]
                for entry in results
                if not entry["error"] and entry["final_state"].get("human_override_result")
            }
            actual_orders = [
                {"ticker": entry["ticker"], "side": result["side"], "qty": result["requested_qty"]}
                for entry in results
                if not entry["error"]
                and (result := entry["final_state"].get("execution_result"))
                and "requested_qty" in result
            ]
            fixture = {
                "version": FIXTURE_VERSION,
                "run_date": date.today().isoformat(),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "commit": _git_commit(),
                "limits": asdict(limits),
                "portfolio_state": portfolio_state,
                "previous_proposals": self.previous_proposals,
                "tickers": tickers,
                "answers": portfolio_manager.recorded_answers or {},
                "human_overrides": human_overrides,
                "actual": snapshot(results, rotation, exit_checks, actual_orders),
            }
            REPLAY_DIR.mkdir(parents=True, exist_ok=True)
            path = REPLAY_DIR / f"{fixture['run_date']}.json"
            path.write_text(json.dumps(fixture, indent=2, default=str))
        except Exception:
            logger.exception("Recording this run for the golden test failed — trading is unaffected")
        finally:
            portfolio_manager.recorded_answers = None


def _review(review: dict | None) -> dict | None:
    if not review:
        return None
    return {
        "trigger": review.get("trigger"),
        "action": (review.get("decision") or {}).get("action"),
        "confirmed": review.get("confirmed"),
        "error": review.get("error"),
    }


def _ticker_outcome(entry: dict) -> dict:
    if entry["error"]:
        return {"error": entry["error"]}
    state = entry["final_state"]
    decision = state.get("portfolio_decision") or {}
    outcome = {
        "action": decision.get("action"),
        "size_pct": round(float(decision.get("size_pct") or 0.0), 6),
        "confidence": decision.get("confidence"),
        "precheck": (state.get("risk_check") or {}).get("reason_code"),
        "final_check": (state.get("risk_check_final") or {}).get("reason_code"),
        "review": _review(state.get("position_review")),
    }
    override = state.get("human_override_result")
    if override:
        outcome["override_approved"] = override.get("approved")
    return outcome


def _rotation(record: dict | None) -> dict | None:
    if not record:
        return None
    proposal = record.get("proposal") or {}
    return {
        "candidates": [c["ticker"] for c in record.get("candidates", [])],
        "skipped": record.get("skipped"),
        "error": record.get("error"),
        "proposal": {k: proposal.get(k) for k in ("action", "sell_ticker", "buy_ticker")} if proposal else None,
        "would_execute": record.get("would_execute"),
        "confirmed": record.get("confirmed"),
    }


def snapshot(results: list[dict], rotation: dict | None, exit_checks: list[dict], orders: list[dict]) -> dict:
    """What a day's run did, without the model's prose or the risk manager's
    wording, so the snapshot only changes when behavior does."""
    return {
        "orders": orders,
        "tickers": {entry["ticker"]: _ticker_outcome(entry) for entry in sorted(results, key=lambda e: e["ticker"])},
        "rotation": _rotation(rotation),
        "exits": {
            c["ticker"]: c["result"]["reason_code"] if "result" in c else "error"
            for c in exit_checks
        },
    }


class _Broker:
    """The recorded starting portfolio, plus this replay's orders. Like
    get_portfolio_state after close: a submitted buy is a pending order that
    counts as exposure at the price it was sized at; a pending sell changes
    nothing until it fills."""

    def __init__(self, portfolio_state: dict):
        self.portfolio_state = copy.deepcopy(portfolio_state)
        self.orders: list[dict] = []

    def state(self) -> dict:
        return copy.deepcopy(self.portfolio_state)

    def submit(self, ticker, side, qty, run_id=None, reference_price=None):
        self.orders.append({"ticker": ticker, "side": side, "qty": float(qty)})
        if side == "buy":
            open_positions = self.portfolio_state["open_positions"]
            open_positions[ticker] = open_positions.get(ticker, 0.0) + qty * reference_price
        return {
            "order_id": f"replay-{len(self.orders)}",
            "status": "queued_for_next_session",
            "filled_qty": 0.0,
            "filled_avg_price": None,
            "requested_qty": float(qty),
            "side": side,
            "ticker": ticker,
            "error": None,
        }


def replay_day(fixture: dict) -> dict:
    """Run one recorded day through run_daily.run_once as the code stands
    now, with the current RiskLimits and settings, and return its snapshot."""
    import pytest  # dev dependency: only the replay needs it, never a live run

    import run_daily
    from agents import exit_rules, risk_manager
    from config import settings
    from execution import alpaca_executor
    from logs import audit_logger
    from orchestration import graph, rotation

    run_date = date.fromisoformat(fixture["run_date"])
    tickers = fixture["tickers"]
    answers = fixture["answers"]
    overrides = fixture["human_overrides"]
    broker = _Broker(fixture["portfolio_state"])
    captured: dict = {}

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return cls(run_date.year, run_date.month, run_date.day)

    def recorded(ticker: str) -> dict:
        if ticker not in tickers:
            raise RuntimeError(f"no recorded data for {ticker}")
        if "error" in tickers[ticker]:
            raise RuntimeError(tickers[ticker]["error"])
        return tickers[ticker]

    def price_data(ticker):
        frame = pd.DataFrame({"Close": [recorded(ticker)["last_close"]]})
        frame.attrs["ticker"] = ticker
        return frame

    def parse(kind, ticker, system_content, user_content, response_format):
        key = portfolio_manager.answer_key(kind, ticker)
        if key not in answers:
            raise NoRecordedAnswer(f"no recorded model answer for {key}")
        return response_format.model_validate(answers[key])

    def request_override(*, ticker, **_):
        return overrides.get(ticker) or {
            "approved": False, "responder": None, "reason": "no recorded human answer",
        }

    def shadow_rotation(results, limits):
        captured["rotation"] = rotation.shadow_rotation(results, limits, get_state=broker.state, today=run_date)
        return captured["rotation"]

    real_exit_checks = run_daily._shadow_exit_checks

    def exit_checks(limits):
        captured["exit_checks"] = real_exit_checks(limits)
        return captured["exit_checks"]

    with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mp.setattr(audit_logger, "LOG_PATH", tmp / "trades.jsonl")
        mp.setattr(run_daily, "SUMMARY_LOG_PATH", tmp / "daily_summary.log")
        mp.setattr(run_daily, "LAST_COMPLETED_RUN_PATH", tmp / "last_completed_run.json")
        mp.setattr(confirmation, "LAST_PROPOSALS_PATH", tmp / "last_proposals.json")
        mp.setattr(risk_manager, "KILL_SWITCH_FILE", tmp / "KILL_SWITCH")
        mp.setattr(alpaca_executor, "PENDING_ORDERS_PATH", tmp / "pending_orders.json")
        mp.setattr(sys.modules[__name__], "REPLAY_DIR", tmp / "replay")
        if fixture["previous_proposals"] is not None:
            confirmation.LAST_PROPOSALS_PATH.write_text(json.dumps(fixture["previous_proposals"]))

        mp.setattr(confirmation, "date", FrozenDate)
        mp.setattr(exit_rules, "date", FrozenDate)
        mp.setattr(settings, "kill_switch", False)
        mp.setattr(settings, "auto_execute", False)  # run_once sets it for paper

        mp.setattr(run_daily, "_woke_up_too_late", lambda: False)
        mp.setattr(run_daily, "_startup_safety_check", lambda: True)
        mp.setattr(run_daily, "_wait_for_network", lambda: None)
        mp.setattr(run_daily, "reconcile_pending_orders", lambda: [])
        mp.setattr(run_daily, "_get_client", lambda: SimpleNamespace(get_clock=lambda: SimpleNamespace(is_open=False)))
        mp.setattr(run_daily, "get_portfolio_state", broker.state)
        mp.setattr(run_daily, "shadow_rotation", shadow_rotation)
        mp.setattr(run_daily, "_shadow_exit_checks", exit_checks)
        mp.setattr(alpaca_executor, "submit_order", broker.submit)

        mp.setattr(graph, "get_price_data", price_data)
        mp.setattr(graph, "get_technical_signal", lambda prices: copy.deepcopy(recorded(prices.attrs["ticker"])["technical_signal"]))
        mp.setattr(graph, "get_news_sentiment", lambda t: copy.deepcopy(recorded(t)["sentiment_signal"]))
        mp.setattr(graph, "get_fundamentals", lambda t: copy.deepcopy(recorded(t)["fundamentals_signal"]))
        mp.setattr(graph, "request_override", request_override)
        mp.setattr(portfolio_manager, "_parse", parse)

        # A replay is not a run: keep it out of logs/run_daily.log. Anything
        # that goes wrong in it still lands in the snapshot.
        logging.disable(logging.CRITICAL)
        try:
            results = run_daily.run_once(force=True)
        finally:
            logging.disable(logging.NOTSET)

    return snapshot(results, captured.get("rotation"), captured.get("exit_checks", []), broker.orders)
