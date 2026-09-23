from datetime import date

import pytest

from agents.exit_rules import (
    REASON_MAX_HOLDING_DAYS,
    REASON_NO_EXIT,
    REASON_STOP_LOSS,
    REASON_TRAILING_STOP,
    check_exit,
)
from config.settings import RiskLimits

TODAY = date(2026, 9, 22)


def _position(entry=100.0, current=100.0, hwm=None, opened_at="2026-09-20"):
    return {
        "avg_entry_price": entry,
        "current_price": current,
        "unrealized_plpc": current / entry - 1,
        "opened_at": opened_at,
        "high_water_mark": hwm if hwm is not None else max(entry, current),
    }


@pytest.fixture
def limits():
    return RiskLimits()  # stop 8%, trail 10% armed at +5%, review at 30 days


def test_flat_position_does_nothing(limits):
    result = check_exit(_position(), limits, TODAY)
    assert result == {
        "exit": False, "review": False,
        "reasons": ["no exit rule triggered"], "reason_code": REASON_NO_EXIT,
    }


def test_stop_loss_fires_at_threshold(limits):
    result = check_exit(_position(current=92.0), limits, TODAY)
    assert result["exit"] is True
    assert result["reason_code"] == REASON_STOP_LOSS


def test_stop_loss_does_not_fire_just_above_threshold(limits):
    assert check_exit(_position(current=92.5), limits, TODAY)["exit"] is False


def test_trailing_stop_fires_after_activation(limits):
    # Ran up to +20%, now 10% off that high but still +8% overall.
    result = check_exit(_position(current=108.0, hwm=120.0), limits, TODAY)
    assert result["exit"] is True
    assert result["reason_code"] == REASON_TRAILING_STOP


def test_trailing_stop_not_armed_below_activation(limits):
    # Peaked at +4% (below the +5% activation), now 10% off that peak.
    # Only the plain stop-loss should apply, and -6.4% doesn't reach it.
    result = check_exit(_position(current=93.6, hwm=104.0), limits, TODAY)
    assert result["exit"] is False


def test_stop_loss_wins_over_trailing_stop(limits):
    # Both rules would fire; reason_code must name the more severe one.
    result = check_exit(_position(current=90.0, hwm=110.0), limits, TODAY)
    assert result["reason_code"] == REASON_STOP_LOSS


def test_current_price_above_stale_hwm_is_used_as_hwm(limits):
    # hwm hasn't been synced yet today; current price is the new high.
    result = check_exit(_position(current=130.0, hwm=110.0), limits, TODAY)
    assert result["exit"] is False


def test_max_holding_days_flags_review_but_never_sells(limits):
    result = check_exit(_position(opened_at="2026-08-23"), limits, TODAY)  # 30 days
    assert result["exit"] is False
    assert result["review"] is True
    assert result["reason_code"] == REASON_MAX_HOLDING_DAYS


def test_hard_exit_wins_over_holding_period_review(limits):
    result = check_exit(_position(current=90.0, opened_at="2026-01-01"), limits, TODAY)
    assert result["exit"] is True
    assert result["reason_code"] == REASON_STOP_LOSS


def test_missing_opened_at_skips_holding_rule(limits):
    result = check_exit(_position(opened_at=None), limits, TODAY)
    assert result["reason_code"] == REASON_NO_EXIT
