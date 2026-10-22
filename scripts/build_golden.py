"""Builds the golden test from the days run_daily recorded in logs/replay/
(see orchestration/replay.py). Scheduled once, Nov 22 2026, by
scripts/launchd/com.agentictradingsystem.golden-snapshot.plist; run by hand
to rebuild after a deliberate behavior change.

    uv run python scripts/build_golden.py [--out REPORT] [--update] [--check]

For every recorded day it replays the day through the current code and
compares that with what the day actually did. A day that doesn't reproduce
means the code changed after it was recorded (its commit is listed) or the
replay misses something; either way, read it before trusting the snapshot.

Then it copies the days to tests/golden/days/ and writes the replays to
tests/golden/snapshot.json, which tests/test_golden.py compares against from
then on. An existing snapshot is only replaced with --update, so a rebuild
is always a deliberate decision. --check only reports.

The report also counts what the window covered (buys, sells, blocked buys,
reviews, rotations, exit flags): the snapshot only guards situations that
happened in it.
"""

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestration.replay import FIXTURE_VERSION, REPLAY_DIR, replay_day  # noqa: E402

GOLDEN_DIR = ROOT / "tests" / "golden"
SNAPSHOT_PATH = GOLDEN_DIR / "snapshot.json"


def _differences(replayed: dict, actual: dict) -> list[str]:
    lines = []
    if replayed["orders"] != actual["orders"]:
        lines.append(f"orders: actual {actual['orders']} / replay {replayed['orders']}")
    for ticker in sorted(set(replayed["tickers"]) | set(actual["tickers"])):
        a, r = actual["tickers"].get(ticker), replayed["tickers"].get(ticker)
        if a != r:
            lines.append(f"{ticker}: actual {a} / replay {r}")
    for key in ("rotation", "exits"):
        if replayed[key] != actual[key]:
            lines.append(f"{key}: actual {actual[key]} / replay {replayed[key]}")
    return lines


def _coverage(snapshots: dict[str, dict]) -> list[str]:
    counts = Counter()
    for day in snapshots.values():
        counts.update(f"{o['side']} orders" for o in day["orders"])
        for outcome in day["tickers"].values():
            if "error" in outcome:
                counts["ticker errors"] += 1
                continue
            counts[f"decisions: {outcome['action']}"] += 1
            if outcome["final_check"] not in (None, "within_limits", "hold_requested"):
                counts[f"final check: {outcome['final_check']}"] += 1
            if outcome["review"]:
                review = outcome["review"]
                counts[f"position reviews: {review['action'] or 'no decision'}"] += 1
                if review["confirmed"]:
                    counts["position reviews: confirmed sells"] += 1
        rotation = day["rotation"]
        if rotation:
            if rotation["proposal"]:
                counts[f"rotations: {rotation['proposal']['action']}"] += 1
            else:
                counts["rotations: skipped" if rotation["skipped"] else "rotations: error"] += 1
            if rotation["confirmed"]:
                counts["rotations: confirmed swaps"] += 1
        counts.update(f"exit rules: {code}" for code in day["exits"].values() if code != "no_exit")
    return [f"  {name}: {n}" for name, n in sorted(counts.items())]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, help="also write the report here")
    parser.add_argument("--update", action="store_true", help="replace an existing snapshot")
    parser.add_argument("--check", action="store_true", help="report only, write nothing")
    args = parser.parse_args()

    fixtures = {}
    for path in sorted(REPLAY_DIR.glob("*.json")):
        fixture = json.loads(path.read_text())
        if fixture.get("version") == FIXTURE_VERSION:
            fixtures[path] = fixture

    report = [f"Golden snapshot build — {len(fixtures)} recorded days in {REPLAY_DIR}"]
    if not fixtures:
        report.append("Nothing recorded — is run_daily writing logs/replay/? Nothing written.")
        return _finish(report, args.out, 1)

    snapshots, mismatched = {}, []
    for path, fixture in fixtures.items():
        day = fixture["run_date"]
        snapshots[day] = replay_day(fixture)
        diffs = _differences(snapshots[day], fixture["actual"])
        status = "reproduces" if not diffs else f"DOES NOT REPRODUCE ({len(diffs)} differences)"
        report.append(f"{day} (commit {fixture.get('commit')}): {status}")
        report += [f"    {line}" for line in diffs]
        if diffs:
            mismatched.append(day)

    report += ["", "Coverage across the window:", *_coverage(snapshots)]
    commits = sorted({f.get("commit") or "unknown" for f in fixtures.values()})
    report.append(f"Recorded on commits: {', '.join(commits)}")
    if mismatched:
        report.append(
            f"{len(mismatched)} day(s) don't reproduce: read the differences above before "
            "trusting the snapshot. A commit that changed behavior mid-window explains them; "
            "anything else is a gap in the replay."
        )

    if args.check:
        report.append("--check: nothing written.")
        return _finish(report, args.out, 0)
    if SNAPSHOT_PATH.exists() and not args.update:
        report.append(f"{SNAPSHOT_PATH} already exists — pass --update to replace it. Nothing written.")
        return _finish(report, args.out, 1)

    days_dir = GOLDEN_DIR / "days"
    if days_dir.exists():
        shutil.rmtree(days_dir)
    days_dir.mkdir(parents=True)
    for path, fixture in fixtures.items():
        shutil.copy(path, days_dir / f"{fixture['run_date']}.json")
    SNAPSHOT_PATH.write_text(json.dumps(snapshots, indent=2, sort_keys=True) + "\n")
    report.append(f"Wrote {len(snapshots)} days to {days_dir} and the snapshot to {SNAPSHOT_PATH}.")
    report.append("Review the report, then commit tests/golden/. From then on, `uv run pytest` checks it.")
    return _finish(report, args.out, 0)


def _finish(report: list[str], out: Path | None, code: int) -> int:
    text = "\n".join(report) + "\n"
    print(text, end="")
    if out:
        out.write_text(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
