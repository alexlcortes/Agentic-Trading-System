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
