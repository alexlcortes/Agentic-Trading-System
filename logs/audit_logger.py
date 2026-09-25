import json
from datetime import datetime, timezone
from pathlib import Path

# __file__-relative, not a "logs/trades.jsonl" string relative to cwd — this
# module gets called from wherever a process happens to be launched (a cron
# job's cwd is not guaranteed), so the log path must not depend on that.
LOG_PATH = Path(__file__).parent / "trades.jsonl"


def _serialize_timestamp(timestamp) -> str:
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.isoformat()
    return str(timestamp)


def log_decision(
    run_id: str,
    timestamp,
    agent_outputs: dict,
    final_decision: dict,
    execution_result: dict | None,
) -> None:
    """Append one JSON line recording the full decision trail for one run.

    Captures every agent's raw output (technical signal, sentiment signal,
    risk manager's reasons, portfolio manager's reasoning), not just the
    final action, for EVERY run — including runs where the decision was
    to do nothing. A regulator (or you, six months from now, staring at a
    trade that looks wrong) needs to be able to tell whether an outcome
    was a bad signal, a bug, or a risk-limit edge case — that's only
    possible if the reasoning is captured every time, not just when
    something got traded.
    """
    entry = {
        "run_id": run_id,
        "timestamp": _serialize_timestamp(timestamp),
        "agent_outputs": agent_outputs,
        "final_decision": final_decision,
        "execution_result": execution_result,
    }

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_reconciliation(
    order_id: str,
    original_run_id: str,
    ticker: str,
    resolved_result: dict,
) -> None:
    """Append a follow-up entry recording what a previously-queued order
    actually did (see execution.alpaca_executor.reconcile_pending_orders).

    trades.jsonl is append-only — the original run's execution_result is
    never rewritten, since it truthfully reflects what was known at the
    time (a poll still open because the market was closed). The eventual
    outcome is logged here as its own entry, linked back to that run by
    order_id/original_run_id, so both the initial and final state stay on
    the record.
    """
    entry = {
        "type": "reconciliation",
        "order_id": order_id,
        "original_run_id": original_run_id,
        "ticker": ticker,
        "timestamp": _serialize_timestamp(datetime.now(timezone.utc)),
        "resolved_execution_result": resolved_result,
    }

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_human_override(
    run_id: str,
    ticker: str,
    requested_size_pct: float,
    timestamp,
    result: dict,
    requested_qty: int | None = None,
    requested_notional: float | None = None,
) -> None:
    """Append an entry recording a human-override request and its outcome
    (see agents.human_override) — approved, declined, or timed out, and by
    whom, so a bypassed max_position_pct cap is on the record exactly like
    every other decision.
    """
    entry = {
        "type": "human_override",
        "run_id": run_id,
        "ticker": ticker,
        "requested_size_pct": requested_size_pct,
        "requested_qty": requested_qty,
        "requested_notional": requested_notional,
        "timestamp": _serialize_timestamp(timestamp),
        "result": result,
    }

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def log_exit_check(
    ticker: str,
    mode: str,
    position: dict,
    result: dict,
    timestamp,
) -> None:
    """Append an entry recording one exit-rule evaluation of a held
    position (see agents.exit_rules and run_daily.py). Logged for EVERY
    held position on every run, not just the ones that trigger — judging
    whether the rules help needs the price path of positions they left
    alone too, and a later entry for the same ticker shows what happened
    after a would_exit.

    `mode` is "shadow" while these are log-only: would_exit=True means no
    order was placed.
    """
    entry = {
        "type": "exit_check",
        "mode": mode,
        "ticker": ticker,
        "timestamp": _serialize_timestamp(timestamp),
        "would_exit": result["exit"],
        "review": result["review"],
        "reason_code": result["reason_code"],
        "reasons": result["reasons"],
        "position": position,
    }

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
