"""Rotation (shadow): when the portfolio holds max_open_positions, a buy
signal on any other ticker is rejected outright — a forced hold with no LLM
call — so nothing ever asks whether it beats something already held. After
the watchlist loop, when every blocked candidate and every holding has
today's signals, this asks the portfolio manager whether one swap is worth
making, sizes it in code, runs both legs past the risk manager, and logs
the result as type "rotation". It never places an order (ROTATION_MODE
has no live setting yet).
"""

import logging
from datetime import date

from agents.portfolio_manager import propose_rotation
from agents.risk_manager import REASON_MAX_OPEN_POSITIONS, check_trade
from config import settings
from config.settings import RiskLimits
from execution.alpaca_executor import get_portfolio_state
from logs.audit_logger import log_rotation

logger = logging.getLogger(__name__)

SIGNAL_KEYS = ("technical_signal", "sentiment_signal", "fundamentals_signal")


def blocked_candidates(results: list[dict]) -> list[dict]:
    """The run_daily results whose buy the precheck rejected only because
    the portfolio is full."""
    return [
        entry
        for entry in results
        if not entry["error"]
        and (entry["final_state"].get("risk_check") or {}).get("reason_code") == REASON_MAX_OPEN_POSITIONS
    ]


def _signals(final_state: dict) -> dict:
    return {key: final_state.get(key) for key in SIGNAL_KEYS}


def _size_swap(proposal: dict, portfolio_state: dict, candidate_prices: dict, limits: RiskLimits) -> dict:
    """Both legs through the risk manager: the sell against today's
    portfolio, the buy against the portfolio as it would be after the sell.
    The buy is checked at max_position_pct — the size a live swap would ask
    for is a decision for when this goes live."""
    equity = float(portfolio_state["equity"])
    sell_ticker, buy_ticker = proposal["sell_ticker"], proposal["buy_ticker"]
    position = portfolio_state["position_details"][sell_ticker]
    sell_check = check_trade(
        {
            "ticker": sell_ticker,
            "action": "sell",
            "size_pct": float(portfolio_state["open_positions"][sell_ticker]) / equity,
            "price": float(position["current_price"]),
        },
        portfolio_state,
        limits,
    )
    after_sell = {
        **portfolio_state,
        "open_positions": {t: v for t, v in portfolio_state["open_positions"].items() if t != sell_ticker},
        "position_details": {t: p for t, p in portfolio_state["position_details"].items() if t != sell_ticker},
    }
    buy_check = check_trade(
        {
            "ticker": buy_ticker,
            "action": "buy",
            "size_pct": limits.max_position_pct,
            "price": candidate_prices[buy_ticker],
        },
        after_sell,
        limits,
    )
    return {
        "sell_check": sell_check,
        "buy_check": buy_check,
        "would_execute": sell_check["approved"] and buy_check["approved"] and buy_check["adjusted_size"] > 0,
    }


def shadow_rotation(
    results: list[dict], limits: RiskLimits, get_state=get_portfolio_state, today: date | None = None
) -> dict | None:
    """None when there is nothing to consider; otherwise the logged record.
    Never raises: a failure here must not cost the run."""
    if settings.ROTATION_MODE != "shadow":
        return None
    candidates = blocked_candidates(results)
    if not candidates:
        return None

    record = {
        "mode": "shadow",
        "candidates": [
            {"ticker": c["ticker"], "technical_confidence": c["final_state"]["technical_signal"].get("confidence")}
            for c in candidates
        ],
        "proposal": None,
    }
    try:
        # Fetched after the loop, so it reflects anything the run itself traded.
        portfolio_state = get_state()
        held = portfolio_state.get("position_details") or {}
        record["holdings"] = list(held)
        if len(portfolio_state["open_positions"]) < limits.max_open_positions:
            record["skipped"] = "a slot opened during the run — the portfolio is no longer full"
        else:
            states = {e["ticker"]: e["final_state"] for e in results if not e["error"]}
            proposal = propose_rotation(
                candidates=[{"ticker": c["ticker"], **_signals(c["final_state"])} for c in candidates],
                holdings=[
                    {
                        "ticker": ticker,
                        "position": position,
                        "signals": _signals(states[ticker]) if ticker in states else None,
                    }
                    for ticker, position in held.items()
                ],
                today=today,
            )
            record["proposal"] = proposal
            if proposal["action"] == "swap":
                prices = {c["ticker"]: float(c["final_state"]["price_data"]["Close"].iloc[-1]) for c in candidates}
                record.update(_size_swap(proposal, portfolio_state, prices, limits))
    except Exception as exc:
        logger.exception("Shadow rotation failed — continuing with the normal run")
        record["error"] = str(exc)

    log_rotation(record)
    return record
