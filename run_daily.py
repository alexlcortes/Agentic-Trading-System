"""Runs the full trading pipeline once per trading day against Alpaca —
meant to be invoked by cron or a scheduled task, scheduled shortly after
market close, FOR PAPER TRADING. Live mode is supported by the code but
is deliberately not cron-safe — see the live-mode note below.

CHECKPOINT before ever pointing this at live capital (see Phase 9/10 in
agentic-trading-system-build-sequence.md): run this unattended for 4-8
weeks and confirm no unhandled crashes, no silent failures, the risk
manager was never bypassed, and the logged reasoning still makes sense
when read back — regardless of P&L. To check the risk manager was never
bypassed:
    grep '"approved": false' logs/trades.jsonl
then manually confirm none of those lines also have a non-null,
successfully-filled execution_result — the graph's design (Phase 5)
should make that combination impossible by construction, but the whole
point of this checkpoint is to verify that in practice, not just assume
the code does what it's supposed to.

LIVE MODE: this script never forces auto_execute=True except in paper
mode. If ALPACA_BASE_URL points at live and auto_execute=True (e.g. left
over from paper testing), it refuses to run at all — live trading always
starts in manual-approval mode, no exceptions. This means live mode is
NOT cron-safe: human_gate's input() will block (and fail under cron,
with no attached tty) waiting for someone to actually confirm each trade.
That's intentional, not a bug — there's no remote/async approval
mechanism yet (a known, deliberately deferred follow-up project), so
"live" necessarily means "run this interactively, with a human present."
max_position_pct is also overridden to LIVE_MAX_POSITION_PCT (a small
fraction of whatever was paper-validated) rather than inheriting
RiskLimits' paper-tested default.
"""

import logging
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from config.settings import RiskLimits
from execution.alpaca_executor import _get_client, get_portfolio_state, reconcile_pending_orders
from logs.audit_logger import log_reconciliation
from orchestration.graph import run_trading_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "logs" / "run_daily.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

SUMMARY_LOG_PATH = Path(__file__).parent / "logs" / "daily_summary.log"


def _startup_safety_check() -> bool:
    """Returns True if this run is against paper (safe to force unattended
    auto-execution). Returns False for live — meaning live is allowed to
    run, but ONLY in manual-approval mode, no exceptions, even if
    AUTO_EXECUTE=true is still set in .env from paper testing.

    This intentionally does NOT try to make live mode cron/unattended-safe.
    human_gate's input() will block (and fail with EOFError under cron,
    with no attached tty) if there's no one there to answer it — that's
    correct, not a bug: this script has no remote/async approval mechanism
    yet, so "live mode" necessarily means "run this interactively, with a
    human present," until that's built as its own project.
    """
    is_paper = "paper" in settings.ALPACA_BASE_URL.lower()
    if not is_paper and settings.auto_execute:
        raise RuntimeError(
            "run_daily.py refuses to run: ALPACA_BASE_URL points at a LIVE endpoint "
            "and auto_execute=True. Live trading always starts in manual-approval "
            "mode, no exceptions — set AUTO_EXECUTE=false (or unset it) in .env."
        )
    return is_paper


def _write_summary(entries: list[dict], skipped: bool = False) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    lines = [f"\n=== {timestamp} ==="]

    if skipped:
        lines.append("Market is currently open — skipping (run this after close).")
    elif not entries:
        lines.append("No tickers processed.")
    else:
        for entry in entries:
            if entry["error"]:
                lines.append(f"{entry['ticker']}: ERROR — {entry['error']}")
                continue
            decision = entry["final_state"].get("portfolio_decision", {}) or {}
            execution = entry["final_state"].get("execution_result")
            lines.append(
                f"{entry['ticker']}: action={decision.get('action')} "
                f"size_pct={decision.get('size_pct')} "
                f"confidence={decision.get('confidence')} "
                f"execution={execution}"
            )

    with open(SUMMARY_LOG_PATH, "a") as f:
        f.write("\n".join(lines) + "\n")


def run_once() -> list[dict]:
    is_paper = _startup_safety_check()

    if is_paper:
        settings.auto_execute = True  # unattended execution — safe only in paper mode
    else:
        logger.warning(
            "Running against a LIVE Alpaca endpoint — auto_execute=%s, manual "
            "approval required for every trade.",
            settings.auto_execute,
        )

    # Confirm what any order queued by a prior run (submitted after close,
    # see execution/alpaca_executor.py) actually did — checked every run,
    # regardless of today's market state, since it's resolving a past run's
    # order, not placing one now.
    for resolved in reconcile_pending_orders():
        log_reconciliation(
            order_id=resolved["order_id"],
            original_run_id=resolved["run_id"],
            ticker=resolved["ticker"],
            resolved_result=resolved["result"],
        )
        logger.info(
            "Reconciled queued order %s for %s (from run %s): status=%s filled_qty=%s",
            resolved["order_id"], resolved["ticker"], resolved["run_id"],
            resolved["result"]["status"], resolved["result"]["filled_qty"],
        )

    if _get_client().get_clock().is_open:
        logger.info("Market is currently open — skipping this run (schedule it after close).")
        _write_summary([], skipped=True)
        return []

    limits = RiskLimits()
    if not is_paper:
        # Deliberately separate from RiskLimits' paper-validated default —
        # live starts much smaller and should be manually ramped up only as
        # a real live track record accumulates, never silently inherited
        # from whatever was validated on paper.
        limits = replace(limits, max_position_pct=settings.LIVE_MAX_POSITION_PCT)
        logger.warning("Live max_position_pct overridden to %.4f", limits.max_position_pct)

    results: list[dict] = []

    for ticker in limits.watchlist:
        try:
            portfolio_state = get_portfolio_state()
            final_state = run_trading_cycle(ticker, portfolio_state, risk_limits=limits)
            results.append({"ticker": ticker, "final_state": final_state, "error": None})
        except Exception as exc:
            # Never let one ticker's failure silently kill the rest of the day's
            # run, or go unlogged — both are exactly what the Phase 9 checkpoint
            # ("no unhandled crashes or silent failures") is checking for.
            logger.exception("run_daily: %s failed", ticker)
            results.append({"ticker": ticker, "final_state": None, "error": str(exc)})

    _write_summary(results)
    return results


if __name__ == "__main__":
    outcomes = run_once()
    if any(entry["error"] for entry in outcomes):
        sys.exit(1)
