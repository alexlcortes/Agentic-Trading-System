"""Crash-safe writes for the JSON state files later runs depend on.

pending_orders.json and positions_meta.json used to be rewritten in place
(open "w", then json.dump). The watchdog in scripts/run_daily_with_n8n.sh
ends an overrunning run with SIGTERM and then SIGKILL, and Python dies on
either without running cleanup. A kill partway through a write left a
truncated file, and every later run failed reading it until someone fixed
the file by hand. Here the data goes to a temporary file in the same directory,
and os.replace swaps it in, an atomic rename on the same filesystem. A
reader sees the old file or the new one, never part of either.
"""

import json
import os
import tempfile
from pathlib import Path


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Includes KeyboardInterrupt. A SIGKILL can still leave the temp file
        # behind; it's named after the real file and never read.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
