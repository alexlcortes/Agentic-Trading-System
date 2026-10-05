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

Exit rules (agents/exit_rules.py) run in shadow mode during this window:
every held position is evaluated each run and logged as type "exit_check",
but no order is ever placed from them. They're a separate question from the
checkpoint above — "would these rules have helped?" — answered by following
each ticker's later exit_check entries after a would_exit:
    grep '"would_exit": true' logs/trades.jsonl
Any such line followed by a real sell execution_result for the same ticker
means a shadow exit leaked into trading, which should be impossible.

Rotation (orchestration/rotation.py) is shadow-only too: when buys were
blocked because the portfolio is full, it logs whether the portfolio
manager would swap one holding for one of them, as type "rotation":
    grep '"type": "rotation"' logs/trades.jsonl

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
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import settings
from config.settings import RiskLimits
from execution.alpaca_executor import _get_client, get_portfolio_state, reconcile_pending_orders
from agents.exit_rules import check_exit
from logs.audit_logger import log_exit_check, log_reconciliation
from orchestration.confirmation import save_current
from orchestration.graph import run_trading_cycle
from orchestration.rotation import blocked_candidates, shadow_rotation

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

# The launchd job (see scripts/launchd/) is scheduled for 16:30 local time but,
# unlike cron, launchd catches up a StartCalendarInterval fire that was missed
# because the machine was asleep — it runs as soon as the machine wakes, even
# if that's the next morning. This script only ever intends to run "shortly
# after today's close," so any invocation before market-close hour couldn't
# be that — it's a stale catch-up for a day that's already gone. Abort rather
# than trade on yesterday's already-stale daily bar or duplicate the next
# real scheduled run.
CATCHUP_CUTOFF_HOUR = 16


def _woke_up_too_late() -> bool:
    return datetime.now().hour < CATCHUP_CUTOFF_HOUR


# How long to wait for the network before giving up on today's run. A laptop
# on a dead or captive-portal network (e.g. while traveling) fails DNS for
# every API, and without this the run crashed on its first Alpaca call and
# the day was lost. ~20 minutes covers a flaky connection or a machine that
# woke before Wi-Fi rejoined, and keeps the run well before midnight.
NETWORK_RETRY_ATTEMPTS = 10
NETWORK_RETRY_DELAY_SECONDS = 120


class NetworkUnavailableError(RuntimeError):
    pass


def _wait_for_network() -> None:
    """Block until Alpaca is reachable, retrying only on connection-level
    failures (DNS, refused, timeout). An API error such as bad credentials
    is not a network problem and is raised immediately — retrying it would
    just delay the same failure."""
    for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
        try:
            _get_client().get_clock()
            if attempt > 1:
                logger.info("Network is back after %d attempts — continuing the run.", attempt)
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == NETWORK_RETRY_ATTEMPTS:
                raise NetworkUnavailableError(
                    f"Alpaca unreachable after {attempt} attempts: {exc}"
                ) from exc
            logger.warning(
                "Alpaca unreachable (attempt %d/%d), retrying in %ds: %s",
                attempt, NETWORK_RETRY_ATTEMPTS, NETWORK_RETRY_DELAY_SECONDS, exc,
            )
            time.sleep(NETWORK_RETRY_DELAY_SECONDS)


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


def _shadow_exit_checks(limits: RiskLimits) -> list[dict]:
    """Evaluate the exit rules against every held position and log the
    result, without trading on it (EXIT_REVIEW_MODE=shadow). Runs before
    the watchlist loop so it sees positions as of the start of this run.
    Never raises: a bug here must not cost a day of real trading decisions."""
    if settings.EXIT_REVIEW_MODE != "shadow":
        return []
    try:
        details = get_portfolio_state().get("position_details", {})
        checks = []
        for ticker, position in details.items():
            result = check_exit(position, limits)
            log_exit_check(ticker, "shadow", position, result, datetime.now(timezone.utc))
            checks.append({"ticker": ticker, "position": position, "result": result})
        return checks
    except Exception:
        logger.exception("Shadow exit checks failed — continuing with the normal run")
        return [{"ticker": None, "error": True}]


def _exit_summary_lines(exit_checks: list[dict]) -> list[str]:
    if not exit_checks:
        return []
    if any(c.get("error") for c in exit_checks):
        return ["EXIT RULES (shadow): ERROR — see run_daily.log"]
    lines = []
    for c in exit_checks:
        result = c["result"]
        if result["exit"] or result["review"]:
            label = "WOULD EXIT" if result["exit"] else "REVIEW"
            lines.append(
                f"EXIT RULES (shadow): {c['ticker']} {label} [{result['reason_code']}] "
                f"{'; '.join(result['reasons'])} — no order placed"
            )
    if not lines:
        plpcs = ", ".join(
            f"{c['ticker']} {c['position']['unrealized_plpc']:+.2%}" for c in exit_checks
        )
        lines.append(f"EXIT RULES (shadow): {len(exit_checks)} positions checked, none triggered ({plpcs})")
    return lines


def _position_review_lines(entries: list[dict]) -> list[str]:
    lines = []
    for entry in entries:
        review = (entry.get("final_state") or {}).get("position_review")
        if not review:
            continue
        trigger = ", at-cap buy" if review.get("trigger") == "at_cap_buy" else ""
        label = f"POSITION REVIEW ({review['mode']}{trigger}): {entry['ticker']}"
        if review.get("error"):
            lines.append(f"{label} ERROR — {review['error']}")
        elif review.get("decision") is None:
            lines.append(f"{label} skipped — {'; '.join(review['sell_check']['reasons'])}")
        else:
            decision = review["decision"]
            if decision["action"] != "sell":
                lines.append(f"{label} HOLD (confidence {decision['confidence']})")
                continue
            day = "2nd day in a row" if review.get("confirmed") else "1st day, needs a 2nd"
            if review["mode"] == "shadow":
                suffix = " — no order placed"
            elif not review.get("confirmed"):
                suffix = " — not sold until confirmed"
            else:
                suffix = ""
            lines.append(f"{label} SELL (confidence {decision['confidence']}, {day}){suffix}")
    return lines

def _save_proposals(results: list[dict], rotation: dict | None) -> None:
    """Record this run's review sells and rotation swap, so the next run can
    tell a repeat from a first day (orchestration.confirmation). Never raises."""
    try:
        review_sells = [
            entry["ticker"]
            for entry in results
            if not entry["error"]
            and ((entry["final_state"].get("position_review") or {}).get("decision") or {}).get("action") == "sell"
        ]
        proposal = (rotation or {}).get("proposal") or {}
        swap = (
            {"sell": proposal["sell_ticker"], "buy": proposal["buy_ticker"]}
            if proposal.get("action") == "swap"
            else None
        )
        save_current(review_sells, swap)
    except Exception:
        logger.exception("Saving this run's proposals failed — tomorrow's will all count as a first day")


def _blocked_lines(entries: list[dict]) -> list[str]:
    """A forced hold reads like any other hold in the per-ticker lines, so
    name the buys the full portfolio turned away."""
    return [
        f"BLOCKED (portfolio full): {entry['ticker']} technical buy "
        f"(confidence {entry['final_state']['technical_signal'].get('confidence')})"
        for entry in blocked_candidates(entries)
    ]


def _rotation_lines(rotation: dict | None) -> list[str]:
    if not rotation:
        return []
    label = f"ROTATION ({rotation['mode']}):"
    if rotation.get("error"):
        return [f"{label} ERROR — {rotation['error']}"]
    if rotation.get("skipped"):
        return [f"{label} skipped — {rotation['skipped']}"]
    proposal = rotation["proposal"]
    if proposal["action"] != "swap":
        return [f"{label} NO SWAP (confidence {proposal['confidence']})"]
    day = "2nd day in a row" if rotation.get("confirmed") else "1st day, needs a 2nd"
    line = (
        f"{label} SWAP {proposal['sell_ticker']} → {proposal['buy_ticker']} "
        f"(confidence {proposal['confidence']}, {day}) — no order placed"
    )
    if not rotation["would_execute"]:
        reasons = rotation["sell_check"]["reasons"] + rotation["buy_check"]["reasons"]
        line += f" [risk check would block: {'; '.join(reasons)}]"
    return [line]


def _write_summary(
    entries: list[dict],
    skip_reason: str | None = None,
    exit_checks: list[dict] | None = None,
    rotation: dict | None = None,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    lines = [f"\n=== {timestamp} ==="]

    if skip_reason:
        lines.append(skip_reason)
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

    lines += _blocked_lines(entries)
    lines += _rotation_lines(rotation)
    lines += _position_review_lines(entries)
    lines += _exit_summary_lines(exit_checks or [])

    with open(SUMMARY_LOG_PATH, "a") as f:
        f.write("\n".join(lines) + "\n")


def run_once() -> list[dict]:
    if _woke_up_too_late():
        now = datetime.now()
        logger.warning(
            "Started at %s, before the %d:00 catch-up cutoff — this looks like a "
            "wake-triggered run for a day already missed, not today's scheduled run. "
            "Aborting; the next regularly scheduled run will pick up normally.",
            now.strftime("%Y-%m-%d %H:%M"), CATCHUP_CUTOFF_HOUR,
        )
        _write_summary([], skip_reason="Woke up past the catch-up cutoff — aborting, too late for today.")
        return []

    is_paper = _startup_safety_check()

    if is_paper:
        settings.auto_execute = True  # unattended execution — safe only in paper mode
    else:
        logger.warning(
            "Running against a LIVE Alpaca endpoint — auto_execute=%s, manual "
            "approval required for every trade.",
            settings.auto_execute,
        )

    try:
        _wait_for_network()
    except NetworkUnavailableError as exc:
        logger.error("%s — skipping today's run.", exc)
        _write_summary([], skip_reason=f"Network unreachable — skipped today's run. {exc}")
        raise

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
        _write_summary([], skip_reason="Market is currently open — skipping (run this after close).")
        return []

    limits = RiskLimits()
    if not is_paper:
        # Deliberately separate from RiskLimits' paper-validated default —
        # live starts much smaller and should be manually ramped up only as
        # a real live track record accumulates, never silently inherited
        # from whatever was validated on paper.
        limits = replace(limits, max_position_pct=settings.LIVE_MAX_POSITION_PCT)
        logger.warning("Live max_position_pct overridden to %.4f", limits.max_position_pct)

    exit_checks = _shadow_exit_checks(limits)

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

    rotation = shadow_rotation(results, limits)
    _save_proposals(results, rotation)
    _write_summary(results, exit_checks=exit_checks, rotation=rotation)
    return results


if __name__ == "__main__":
    try:
        outcomes = run_once()
    except NetworkUnavailableError:
        sys.exit(1)
    if any(entry["error"] for entry in outcomes):
        sys.exit(1)
