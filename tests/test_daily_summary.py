import pytest

import run_daily


def test_a_model_decision_shows_its_confidence():
    decision = {"action": "hold", "confidence": 0.82}
    assert run_daily._confidence_text(decision, {"reason_code": "max_position_pct_exceeded"}) == "0.82"


@pytest.mark.parametrize("reason_code, label", [
    ("hold_requested", "technical hold"),
    ("max_open_positions_exceeded", "portfolio full"),
    ("no_existing_position_to_sell", "no position to sell"),
    ("daily_loss_limit", "daily_loss_limit"),  # unlabelled codes show as-is
])
def test_a_forced_hold_says_why_it_has_no_confidence(reason_code, label):
    decision = {"action": "hold", "confidence": None}
    text = run_daily._confidence_text(decision, {"reason_code": reason_code})
    assert text == f"n/a (no model call: {label})"


def test_a_forced_hold_with_no_risk_check_still_reads():
    assert run_daily._confidence_text({"confidence": None}, None) == "n/a (no model call: forced hold)"
