"""Deterministic exit rules for held positions. NO LLM CALLS ANYWHERE IN THIS FILE.

The exit-side counterpart to agents/risk_manager.py: fixed rules that fire
the same way every time and can't be argued out of by a model. Not wired
into the graph yet — see the rollout note below.

Contract:
    position = {                    # one entry of portfolio_state["position_details"]
        "avg_entry_price": float,
        "current_price": float,
        "unrealized_plpc": float,   # e.g. -0.08 = down 8% from entry
        "opened_at": "YYYY-MM-DD",
        "high_water_mark": float,   # highest close since entry
        ...
    }

    returns {
        "exit": bool,               # True = sell the whole position, no LLM override
        "review": bool,             # True = flag for the (future) LLM thesis review
        "reasons": [str, ...],
        "reason_code": str,
    }

Design notes:
    - Rules are checked in severity order and the first hard exit wins, so
      reason_code always names the rule that actually forced the sell.
    - The trailing stop only arms once the position has been up at least
      trailing_stop_activation_pct from entry. Before that, the plain
      stop-loss is the only downside rule — otherwise a position that never
      went anywhere would get stopped out at -10% from a high-water mark
      that is just its entry price, which is a looser stop-loss wearing a
      different name.
    - max_holding_days never forces a sell. A position going nowhere for a
      month is a question ("is the reason we bought this still true?"),
      not an answer, so it only sets review=True.
    - Rollout: these rules run in shadow mode first (log what would have
      exited, trade nothing) so the paper trial isn't measuring two
      different systems halfway through.
"""

from datetime import date

from config.settings import RiskLimits

REASON_STOP_LOSS = "stop_loss"
REASON_TRAILING_STOP = "trailing_stop"
REASON_MAX_HOLDING_DAYS = "max_holding_days"
REASON_NO_EXIT = "no_exit"

# Thresholds are compared with this tolerance so "exactly at the limit"
# fires: in floats, 92/100 - 1 is -0.07999999999999996, which would miss
# an 8% stop-loss by a rounding error.
EPSILON = 1e-9


def check_exit(position: dict, limits: RiskLimits, today: date | None = None) -> dict:
    today = today or date.today()

    entry_price = float(position["avg_entry_price"])
    current_price = float(position["current_price"])
    plpc = float(position["unrealized_plpc"])
    high_water_mark = max(float(position.get("high_water_mark") or 0.0), current_price)

    if plpc <= -limits.stop_loss_pct + EPSILON:
        return {
            "exit": True,
            "review": False,
            "reasons": [
                f"down {-plpc:.2%} from entry, at/over the {limits.stop_loss_pct:.2%} stop-loss"
            ],
            "reason_code": REASON_STOP_LOSS,
        }

    trailing_armed = high_water_mark >= entry_price * (1 + limits.trailing_stop_activation_pct) - EPSILON
    drawdown = 1 - current_price / high_water_mark
    if trailing_armed and drawdown >= limits.trailing_stop_pct - EPSILON:
        return {
            "exit": True,
            "review": False,
            "reasons": [
                f"down {drawdown:.2%} from high-water mark {high_water_mark:.2f}, at/over the "
                f"{limits.trailing_stop_pct:.2%} trailing stop"
            ],
            "reason_code": REASON_TRAILING_STOP,
        }

    opened_at = position.get("opened_at")
    if opened_at is not None:
        days_held = (today - date.fromisoformat(opened_at)).days
        if days_held >= limits.max_holding_days:
            return {
                "exit": False,
                "review": True,
                "reasons": [
                    f"held {days_held} days, at/over the {limits.max_holding_days}-day review threshold"
                ],
                "reason_code": REASON_MAX_HOLDING_DAYS,
            }

    return {"exit": False, "review": False, "reasons": ["no exit rule triggered"], "reason_code": REASON_NO_EXIT}
