import pytest

from agents import risk_manager
from agents.risk_manager import (
    REASON_BELOW_ONE_SHARE,
    REASON_DAILY_LOSS_HALT,
    REASON_MAX_POSITION_PCT,
    REASON_POSITION_RESIZED,
    REASON_SELL_CAPPED,
    REASON_WITHIN_LIMITS,
    check_trade,
    shares_for,
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


def _buy(size_pct=0.05, price=None):
    trade = {"ticker": "X", "action": "buy", "size_pct": size_pct}
    if price is not None:
        trade["price"] = price
    return trade


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


def test_room_under_one_share_is_a_cap_rejection():
    # 2026-09-24 SPY: ~$390 of room under the cap, one share ~$765.73.
    result = check_trade(_buy(price=765.73), _state(100000.0, {"X": 4610.0}), RiskLimits())
    assert result["approved"] is False
    assert result["adjusted_size"] == 0.0
    assert result["reason_code"] == REASON_MAX_POSITION_PCT


def test_requested_size_under_one_share_is_rejected():
    result = check_trade(_buy(0.0034, price=765.73), _state(100000.0), RiskLimits())
    assert result["approved"] is False
    assert result["reason_code"] == REASON_BELOW_ONE_SHARE


def test_resized_room_of_at_least_one_share_is_approved():
    result = check_trade(_buy(price=765.73), _state(100000.0, {"X": 4000.0}), RiskLimits())
    assert result["approved"] is True
    assert result["reason_code"] == REASON_POSITION_RESIZED
    assert shares_for(result["adjusted_size"], 100000.0, 765.73) == 1


def test_exactly_one_share_is_approved():
    result = check_trade(_buy(0.01, price=1000.0), _state(100000.0), RiskLimits())
    assert result["approved"] is True
    assert result["reason_code"] == REASON_WITHIN_LIMITS


def test_sell_under_one_share_is_rejected():
    trade = {"ticker": "X", "action": "sell", "size_pct": 0.001, "price": 500.0}
    result = check_trade(trade, _state(100000.0, {"X": 3000.0}), RiskLimits())
    assert result["approved"] is False
    assert result["reason_code"] == REASON_BELOW_ONE_SHARE


def test_capped_sell_of_whole_position_is_approved():
    trade = {"ticker": "X", "action": "sell", "size_pct": 0.05, "price": 500.0}
    result = check_trade(trade, _state(100000.0, {"X": 3000.0}), RiskLimits())
    assert result["approved"] is True
    assert result["reason_code"] == REASON_SELL_CAPPED


def test_no_price_skips_the_share_check():
    result = check_trade(_buy(0.0034), _state(100000.0), RiskLimits())
    assert result["approved"] is True


def test_non_positive_price_is_invalid():
    with pytest.raises(ValueError):
        check_trade(_buy(price=0.0), _state(100000.0), RiskLimits())
