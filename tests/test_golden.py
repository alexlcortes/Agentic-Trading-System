"""Golden test: every recorded day in tests/golden/days/ replayed through the
current code must match tests/golden/snapshot.json. A failure means behavior
changed. If you meant it to, rebuild with
`uv run python scripts/build_golden.py --update` and commit the new snapshot
with the change; if not, it's a regression. See orchestration/replay.py.
"""

import json
from pathlib import Path

import pytest

from orchestration.replay import replay_day

GOLDEN_DIR = Path(__file__).parent / "golden"
SNAPSHOT_PATH = GOLDEN_DIR / "snapshot.json"

SNAPSHOT = json.loads(SNAPSHOT_PATH.read_text()) if SNAPSHOT_PATH.exists() else {}


@pytest.mark.skipif(not SNAPSHOT, reason="no golden snapshot yet — scripts/build_golden.py builds it (scheduled Nov 22 2026)")
@pytest.mark.parametrize("day", sorted(SNAPSHOT) or ["none"])
def test_replay_matches_snapshot(day):
    fixture = json.loads((GOLDEN_DIR / "days" / f"{day}.json").read_text())
    assert replay_day(fixture) == SNAPSHOT[day]
