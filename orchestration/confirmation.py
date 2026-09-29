"""Two-in-a-row rule for model-proposed exits. A position review sell or a
rotation swap only counts once the previous daily run proposed the same
thing — the same ticker to sell, or the same sell → buy pair. On borderline
positions the model's answer splits across identical calls (see
PROMPT_PATTERNS.md #10 and #11), so a single answer is closer to a coin flip
than a decision; a repeat on the next day's data is not.

    logs/last_proposals.json = {
        "run_date": "YYYY-MM-DD",
        "review_sells": [ticker, ...],
        "rotation_swap": {"sell": ticker, "buy": ticker} | null,
    }

Written once per run, after the watchlist loop and rotation, so every read
during a run sees the previous run. A record older than MAX_GAP_DAYS
confirms nothing: "two in a row" means consecutive trading days, not the
last two runs whenever they happened.
"""

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

LAST_PROPOSALS_PATH = Path(__file__).parent.parent / "logs" / "last_proposals.json"

# Friday to Monday is 3 days; a Monday market holiday makes it 4.
MAX_GAP_DAYS = 4


def load_previous(today: date | None = None) -> dict:
    """The previous run's proposals, or an empty record when there is none,
    it can't be read, or it is too old to count as the previous trading day."""
    empty = {"run_date": None, "review_sells": [], "rotation_swap": None}
    today = today or date.today()
    try:
        with open(LAST_PROPOSALS_PATH) as f:
            previous = json.load(f)
        run_date = date.fromisoformat(previous["run_date"])
    except FileNotFoundError:
        return empty
    except Exception:
        logger.exception("Unreadable %s — treating every proposal as a first day", LAST_PROPOSALS_PATH)
        return empty
    if run_date >= today or (today - run_date).days > MAX_GAP_DAYS:
        return empty
    return {**empty, **previous}


def save_current(review_sells: list[str], rotation_swap: dict | None, today: date | None = None) -> None:
    LAST_PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LAST_PROPOSALS_PATH, "w") as f:
        json.dump(
            {
                "run_date": (today or date.today()).isoformat(),
                "review_sells": sorted(review_sells),
                "rotation_swap": rotation_swap,
            },
            f,
            indent=2,
        )


def review_sell_confirmed(ticker: str, today: date | None = None) -> bool:
    return ticker in load_previous(today)["review_sells"]


def rotation_swap_confirmed(sell: str, buy: str, today: date | None = None) -> bool:
    return load_previous(today)["rotation_swap"] == {"sell": sell, "buy": buy}
