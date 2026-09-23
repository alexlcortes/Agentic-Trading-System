"""Per-position facts Alpaca doesn't track for us: when a position was
opened, which run opened it, and the highest price seen since.

Alpaca's positions API already gives avg_entry_price, current_price and
unrealized_plpc — those are always read fresh from the broker, never
stored here. This file only holds what the broker can't tell us:

    logs/positions_meta.json = {
        ticker: {
            "opened_at": "YYYY-MM-DD",     # first day we saw the position held
            "entry_run_id": str | None,    # run that placed the opening buy (links
                                           # back to its reasoning in trades.jsonl)
            "high_water_mark": float,      # highest current_price seen at any run
        }
    }

Lifecycle:
    - A ticker's entry is created the first time it shows up as held, and
      dropped as soon as it's no longer held (fully sold). Re-buying later
      starts a fresh entry — a new position, a new thesis.
    - Adding to an existing position keeps the original opened_at and
      entry_run_id (Alpaca's avg_entry_price already blends the adds in).
    - high_water_mark is sampled once per run, after the close, so it's the
      highest *closing* price since entry, not the intraday high. The
      trailing stop in agents/exit_rules.py is therefore a close-to-close
      rule, which is consistent with this system only deciding off
      completed daily bars.
"""

import json
from pathlib import Path

POSITIONS_META_PATH = Path(__file__).parent.parent / "logs" / "positions_meta.json"


def load_meta() -> dict:
    if not POSITIONS_META_PATH.exists():
        return {}
    with open(POSITIONS_META_PATH) as f:
        return json.load(f)


def save_meta(meta: dict) -> None:
    POSITIONS_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(POSITIONS_META_PATH, "w") as f:
        json.dump(meta, f, indent=2)


def sync_meta(meta: dict, current_prices: dict[str, float], today: str) -> dict:
    """Pure: return an updated copy of `meta` for the currently held tickers.

    `current_prices` is {ticker: current_price} for every *filled* position
    (queued-but-unfilled buys must not be passed in — they have no entry
    price yet and aren't positions). New tickers get an entry, held ones
    get their high-water mark raised, and tickers no longer held are dropped.
    """
    updated = {}
    for ticker, price in current_prices.items():
        entry = dict(meta.get(ticker) or {})
        entry.setdefault("opened_at", today)
        entry.setdefault("entry_run_id", None)
        entry["high_water_mark"] = max(float(entry.get("high_water_mark") or 0.0), float(price))
        updated[ticker] = entry
    return updated


def record_entry(ticker: str, run_id: str, today: str) -> None:
    """Link a filled buy to the run that placed it. Only fills in
    entry_run_id for a position that doesn't have one yet — an add to an
    existing position must not overwrite the original entry's run."""
    meta = load_meta()
    entry = meta.setdefault(ticker, {"opened_at": today, "entry_run_id": None, "high_water_mark": 0.0})
    if entry.get("entry_run_id") is None:
        entry["entry_run_id"] = run_id
    save_meta(meta)
