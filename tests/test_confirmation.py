"""Two-in-a-row rule: a review sell or rotation swap only counts once the
previous daily run proposed the same thing."""

import json
from datetime import date

import pytest

import run_daily
from orchestration import confirmation

MONDAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(confirmation, "LAST_PROPOSALS_PATH", tmp_path / "last_proposals.json")


def _saved_on(day, sells=("XOM",), swap=None):
    confirmation.save_current(list(sells), swap, today=day)


def test_no_previous_run_confirms_nothing():
    assert confirmation.review_sell_confirmed("XOM", today=MONDAY) is False


def test_friday_confirms_monday():
    _saved_on(date(2026, 10, 2))
    assert confirmation.review_sell_confirmed("XOM", today=MONDAY) is True


def test_a_run_past_the_gap_confirms_nothing():
    _saved_on(date(2026, 9, 30))  # 5 days before Monday
    assert confirmation.review_sell_confirmed("XOM", today=MONDAY) is False


def test_a_rerun_the_same_day_does_not_confirm_itself():
    _saved_on(MONDAY)
    assert confirmation.review_sell_confirmed("XOM", today=MONDAY) is False


def test_an_unreadable_file_confirms_nothing():
    confirmation.LAST_PROPOSALS_PATH.write_text("{not json")
    assert confirmation.review_sell_confirmed("XOM", today=MONDAY) is False


def test_a_swap_needs_the_same_pair():
    _saved_on(date(2026, 10, 2), sells=(), swap={"sell": "XOM", "buy": "MSFT"})
    assert confirmation.rotation_swap_confirmed("XOM", "MSFT", today=MONDAY) is True
    assert confirmation.rotation_swap_confirmed("XOM", "JPM", today=MONDAY) is False


def _entry(ticker, action):
    review = {"decision": {"action": action}} if action else None
    return {"ticker": ticker, "error": None, "final_state": {"position_review": review}}


def test_the_run_saves_its_review_sells_and_swap():
    results = [_entry("XOM", "sell"), _entry("AAPL", "hold"), _entry("JPM", None),
               {"ticker": "V", "error": "boom", "final_state": None}]
    rotation = {"proposal": {"action": "swap", "sell_ticker": "XOM", "buy_ticker": "MSFT"}}
    run_daily._save_proposals(results, rotation)
    saved = json.loads(confirmation.LAST_PROPOSALS_PATH.read_text())
    assert saved["review_sells"] == ["XOM"]
    assert saved["rotation_swap"] == {"sell": "XOM", "buy": "MSFT"}
    assert saved["run_date"] == date.today().isoformat()


def test_a_run_with_no_swap_saves_none():
    run_daily._save_proposals([], {"proposal": {"action": "no_swap"}})
    assert json.loads(confirmation.LAST_PROPOSALS_PATH.read_text())["rotation_swap"] is None
