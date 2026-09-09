"""Deterministic risk checks. NO LLM CALLS ANYWHERE IN THIS FILE.

This is the one place in the system where trading rules are fixed code,
not a model's judgment call. Every other agent can be wrong sometimes;
this function is what keeps "wrong" from becoming "expensive."

Contract:
    proposed_trade = {
        "ticker": str,
        "action": "buy" | "sell" | "hold",
        "size_pct": float,   # requested size as a fraction of equity, e.g. 0.05 = 5%
    }
    portfolio_state = {
        "equity": float,                        # total account equity, in dollars
        "open_positions": {ticker: value, ...}, # current positions, dollar market value
        "daily_realized_pnl": float,            # today's realized P&L in dollars (negative = loss)
    }

Design notes (read before changing this file):
    - kill_switch is read live from config.settings at call time, not cached,
      so flipping it externally takes effect on the very next call.
    - Both the kill switch and the daily-loss halt block ALL actions,
      including sells. This is a deliberate but debatable choice: you could
      argue a sell should still be allowed during a halt, since closing a
      position reduces risk rather than adding it. This file takes the
      stricter reading (a halt is a halt) because that's the literal
      behavior the spec describes — revisit this if you decide otherwise.
    - max_open_positions and max_position_pct only ever constrain buys.
      A sell can never be blocked for "too many positions," and is instead
      capped at the size of the position actually held (can't sell more
      than you own).
"""

from config import settings
from config.settings import RiskLimits

VALID_ACTIONS = ("buy", "sell", "hold")


def _validate_proposed_trade(proposed_trade: dict) -> None:
    if "ticker" not in proposed_trade or not proposed_trade["ticker"]:
        raise ValueError("proposed_trade must include a non-empty 'ticker'")
    if proposed_trade.get("action") not in VALID_ACTIONS:
        raise ValueError(f"proposed_trade['action'] must be one of {VALID_ACTIONS}")
    if float(proposed_trade.get("size_pct", 0.0)) < 0:
        raise ValueError("proposed_trade['size_pct'] must not be negative")


def check_trade(proposed_trade: dict, portfolio_state: dict, limits: RiskLimits) -> dict:
    _validate_proposed_trade(proposed_trade)

    ticker = proposed_trade["ticker"]
    action = proposed_trade["action"]
    requested_size_pct = float(proposed_trade.get("size_pct", 0.0))

    equity = float(portfolio_state.get("equity", 0.0))
    open_positions: dict[str, float] = portfolio_state.get("open_positions", {}) or {}
    daily_realized_pnl = float(portfolio_state.get("daily_realized_pnl", 0.0))

    # Absolute stops — checked first, block every action type including sells.
    if settings.kill_switch:
        return {
            "approved": False,
            "adjusted_size": 0.0,
            "reasons": ["kill switch is active — all trading halted"],
        }

    if equity <= 0:
        return {
            "approved": False,
            "adjusted_size": 0.0,
            "reasons": ["account equity is zero or unknown — cannot size any trade safely"],
        }

    daily_loss_pct = max(0.0, -daily_realized_pnl / equity)
    if daily_loss_pct >= limits.max_daily_loss_pct:
        return {
            "approved": False,
            "adjusted_size": 0.0,
            "reasons": [
                f"daily realized loss {daily_loss_pct:.2%} has reached/exceeded "
                f"max_daily_loss_pct ({limits.max_daily_loss_pct:.2%}) — trading halted for the day"
            ],
        }

    if action == "hold":
        return {
            "approved": True,
            "adjusted_size": 0.0,
            "reasons": ["hold requested — no position change"],
        }

    existing_value = float(open_positions.get(ticker, 0.0))
    adjusted_size_pct = requested_size_pct
    reasons: list[str] = []

    if action == "buy":
        is_new_position = existing_value <= 0
        if is_new_position and len(open_positions) >= limits.max_open_positions:
            return {
                "approved": False,
                "adjusted_size": 0.0,
                "reasons": [
                    f"opening a new {ticker} position would exceed max_open_positions "
                    f"({limits.max_open_positions})"
                ],
            }

        max_position_dollars = limits.max_position_pct * equity
        room_dollars = max(0.0, max_position_dollars - existing_value)
        requested_dollars = requested_size_pct * equity

        if requested_dollars > room_dollars:
            adjusted_size_pct = room_dollars / equity
            reasons.append(
                f"requested size {requested_size_pct:.2%} of equity would push {ticker} "
                f"past max_position_pct ({limits.max_position_pct:.2%}); resized to "
                f"{adjusted_size_pct:.2%}"
            )

        if adjusted_size_pct <= 0:
            return {
                "approved": False,
                "adjusted_size": 0.0,
                "reasons": reasons or [f"{ticker} is already at or above max_position_pct — no room to add"],
            }

    elif action == "sell":
        existing_pct = existing_value / equity
        if existing_value <= 0:
            return {
                "approved": False,
                "adjusted_size": 0.0,
                "reasons": [f"no existing {ticker} position to sell"],
            }
        if requested_size_pct > existing_pct:
            adjusted_size_pct = existing_pct
            reasons.append(
                f"requested sell size {requested_size_pct:.2%} exceeds current {ticker} "
                f"position ({existing_pct:.2%}); capped to full position size"
            )

    if not reasons:
        reasons.append("trade within all risk limits")

    return {
        "approved": True,
        "adjusted_size": round(adjusted_size_pct, 6),
        "reasons": reasons,
    }
