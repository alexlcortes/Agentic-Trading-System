import pytest

from agents import risk_manager
from agents.risk_manager import (
    REASON_DAILY_LOSS_HALT,
    REASON_MAX_POSITION_PCT,
    REASON_POSITION_RESIZED,
    REASON_WITHIN_LIMITS,
    check_trade,
)
from config import settings
from config.settings import RiskLimits


@pytest.fixture(autouse=True)
def no_kill_switch(tmp_path, monkeypatch):
    # Never let a real KILL_SWITCH file or env var in the checkout decide a test.
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(risk_manager, "KILL_SWITCH_FILE", tmp_path / "KILL_SWITCH")


def _state(equity, positions=None, pnl=0.0):
    return {"equity": equity, "open_positions": positions or {}, "daily_realized_pnl": pnl}


def _buy(size_pct=0.05):
    return {"ticker": "X", "action": "buy", "size_pct": size_pct}


def test_daily_loss_halt_at_exactly_the_limit():
    # $2604.24 / $130,212 is exactly 2%, but computes as 0.019999999999999997.
    result = check_trade(_buy(), _state(130212.0, pnl=-2604.24), RiskLimits())
    assert result["reason_code"] == REASON_DAILY_LOSS_HALT


def test_daily_loss_just_under_the_limit_still_trades():
    result = check_trade(_buy(), _state(130212.0, pnl=-2604.23), RiskLimits())
    assert result["reason_code"] != REASON_DAILY_LOSS_HALT


def test_position_a_cent_under_cap_is_rejected_not_approved_at_zero():
    result = check_trade(_buy(), _state(100000.0, {"X": 4999.99}), RiskLimits())
    assert result["approved"] is False
    assert result["reason_code"] == REASON_MAX_POSITION_PCT


def test_position_exactly_at_cap_is_rejected():
    result = check_trade(_buy(), _state(100000.0, {"X": 5000.0}), RiskLimits())
    assert result["reason_code"] == REASON_MAX_POSITION_PCT


def test_real_room_under_cap_still_resizes():
    result = check_trade(_buy(), _state(100000.0, {"X": 4000.0}), RiskLimits())
    assert result["approved"] is True
    assert result["adjusted_size"] == 0.01
    assert result["reason_code"] == REASON_POSITION_RESIZED


def test_buy_within_limits_is_untouched():
    result = check_trade(_buy(0.03), _state(100000.0), RiskLimits())
    assert result == {
        "approved": True, "adjusted_size": 0.03,
        "reasons": ["trade within all risk limits"], "reason_code": REASON_WITHIN_LIMITS,
    }
