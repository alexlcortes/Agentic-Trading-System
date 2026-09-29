"""LangGraph orchestration for the trading pipeline.

Flow:
    START -> market_data ─┐
    START -> sentiment ───┤
    market_data -> technical
    [technical, sentiment] -> risk_precheck   (fan-in join)
    risk_precheck -> portfolio_manager
    portfolio_manager -> risk_final_check
    risk_final_check -> human_gate | log_and_end   (skip if rejected/hold)
    human_gate -> execution | log_and_end          (skip if human says no)
    execution -> log_and_end
    log_and_end -> END

Two risk checks, not one (a deliberate deviation from a single risk_manager
step): risk_precheck gives the portfolio manager's LLM an informative
ceiling based on technical's suggested action; risk_final_check is the
authoritative gate, re-run against whatever action/size the LLM actually
decided on, since a buy-sized ceiling isn't meaningful if the LLM ends up
proposing a sell (or vice versa). See PROMPT_PATTERNS.md and
agents/portfolio_manager.py's docstring for the full reasoning.

Every run appends one entry to logs/trades.jsonl via log_and_end_node,
including runs that end in a hold or a rejection — not just executed
trades.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, TypedDict

import pandas as pd
from langgraph.graph import END, START, StateGraph

from agents.human_override import request_override
from agents.market_data_agent import get_price_data
from agents.portfolio_manager import review_position, synthesize_decision
from agents.risk_manager import REASON_MAX_POSITION_PCT, check_trade, shares_for
from agents.sentiment_agent import get_news_sentiment
from agents.technical_agent import get_technical_signal
from config import settings
from config.settings import RiskLimits
from logs.audit_logger import log_decision
from orchestration.confirmation import review_sell_confirmed

logger = logging.getLogger(__name__)


class TradingState(TypedDict, total=False):
    run_id: str
    ticker: str
    portfolio_state: dict  # {"equity", "open_positions", "daily_realized_pnl"} — caller-supplied
    risk_limits: RiskLimits

    price_data: pd.DataFrame
    technical_signal: dict
    sentiment_signal: dict
    fundamentals_signal: Optional[dict]

    risk_check: dict  # precheck, ceiling for the portfolio manager's prompt
    portfolio_decision: dict
    risk_check_final: dict  # authoritative gate on the actual final decision
    position_review: Optional[dict]  # held position, technical hold or at-cap buy; see _position_review

    human_approved: bool
    human_gate_note: str
    human_override_result: Optional[dict]
    execution_result: Optional[dict]


def _limits(state: TradingState) -> RiskLimits:
    return state.get("risk_limits") or RiskLimits()


def _latest_price(state: TradingState) -> float:
    # Latest close as a sizing approximation (the market order itself fills
    # at the actual current price). Risk checks and execution share it.
    return float(state["price_data"]["Close"].iloc[-1])


def market_data_node(state: TradingState) -> dict:
    price_data = get_price_data(state["ticker"])
    return {"price_data": price_data}


def sentiment_node(state: TradingState) -> dict:
    sentiment_signal = get_news_sentiment(state["ticker"])
    return {"sentiment_signal": sentiment_signal}


def technical_node(state: TradingState) -> dict:
    technical_signal = get_technical_signal(state["price_data"])
    return {"technical_signal": technical_signal}


def risk_precheck_node(state: TradingState) -> dict:
    limits = _limits(state)
    action = state["technical_signal"]["signal"]

    if action == "hold":
        proposed_trade = {"ticker": state["ticker"], "action": "hold", "size_pct": 0.0}
    else:
        # Maximal candidate: not a real trade yet, just discovering the true
        # ceiling for whatever technical's suggested direction is.
        proposed_trade = {
            "ticker": state["ticker"],
            "action": action,
            "size_pct": limits.max_position_pct,
            "price": _latest_price(state),
        }

    risk_check = check_trade(proposed_trade, state["portfolio_state"], limits)
    return {"risk_check": risk_check}


def portfolio_manager_node(state: TradingState) -> dict:
    decision = synthesize_decision(
        ticker=state["ticker"],
        technical_signal=state["technical_signal"],
        sentiment_signal=state["sentiment_signal"],
        risk_check=state["risk_check"],
        fundamentals_signal=state.get("fundamentals_signal"),
    )
    updates = {"portfolio_decision": decision}

    review = _position_review(state)
    if review is not None:
        updates["position_review"] = review
        reviewed = review.get("decision")
        if review["mode"] == "live" and reviewed:
            updates["portfolio_decision"] = _apply_review(decision, reviewed, review.get("confirmed"))
    return updates


def _apply_review(decision: dict, reviewed: dict, confirmed: bool | None) -> dict:
    """Live mode. A sell the previous run also proposed exits the position.
    A first-day sell doesn't, but it does stop an at-cap buy from adding to
    a position the review wants out of. A review hold only replaces a hold:
    after an at-cap buy the decision may be a buy bound for the human
    override, and "keep what you hold" says nothing against adding."""
    if reviewed["action"] == "sell":
        if confirmed:
            return reviewed
        if decision["action"] == "buy":
            return {
                **decision,
                "action": "hold",
                "size_pct": 0.0,
                "reasoning": decision["reasoning"] + (
                    " [Position review proposed selling (first day, awaiting a second) "
                    "— not adding in the meantime]"
                ),
            }
        return decision
    return reviewed if decision["action"] == "hold" else decision


def _review_trigger(state: TradingState) -> Optional[str]:
    """Why a held position gets a review, or None if it doesn't: the technical
    signal says hold, or it says buy but the position is already at its cap —
    both end without anything weighing the position itself."""
    signal = state["technical_signal"]["signal"]
    if signal == "hold":
        return "technical_hold"
    if signal == "buy" and state["risk_check"].get("reason_code") == REASON_MAX_POSITION_PCT:
        return "at_cap_buy"
    return None


def _position_review(state: TradingState) -> Optional[dict]:
    """For a held position the technical signal says to hold, or says to buy
    past its cap, ask the portfolio manager whether to keep it (see
    settings.POSITION_REVIEW_MODE). Only ever proposes hold or a full exit.

    Never raises: in shadow mode a failure here must not cost the run, and in
    live mode the fallback is the forced hold the run already has.
    """
    mode = settings.POSITION_REVIEW_MODE
    ticker = state["ticker"]
    portfolio_state = state["portfolio_state"]
    position = (portfolio_state.get("position_details") or {}).get(ticker)
    if mode not in ("shadow", "live") or position is None:
        return None
    trigger = _review_trigger(state)
    if trigger is None:
        return None

    try:
        equity = float(portfolio_state["equity"])
        existing_value = float(portfolio_state["open_positions"][ticker])
        sell_check = check_trade(
            {
                "ticker": ticker,
                "action": "sell",
                "size_pct": existing_value / equity,
                "price": _latest_price(state),
            },
            portfolio_state,
            _limits(state),
        )
        if not sell_check["approved"]:
            return {"mode": mode, "trigger": trigger, "sell_check": sell_check, "decision": None}

        decision = review_position(
            ticker=ticker,
            position=position,
            trigger=trigger,
            sell_size_pct=sell_check["adjusted_size"],
            technical_signal=state["technical_signal"],
            sentiment_signal=state["sentiment_signal"],
            fundamentals_signal=state.get("fundamentals_signal"),
        )
        review = {"mode": mode, "trigger": trigger, "sell_check": sell_check, "decision": decision}
        if decision["action"] == "sell":
            review["confirmed"] = review_sell_confirmed(ticker)
        return review
    except Exception as exc:
        logger.exception("Position review failed for %s — keeping the forced hold", ticker)
        return {"mode": mode, "trigger": trigger, "error": str(exc), "decision": None}


def risk_final_check_node(state: TradingState) -> dict:
    """The authoritative gate — re-checks whatever action/size the
    portfolio manager actually decided on, not the precheck candidate."""
    limits = _limits(state)
    decision = state["portfolio_decision"]

    proposed_trade = {
        "ticker": decision["ticker"],
        "action": decision["action"],
        "size_pct": decision["size_pct"],
        "price": _latest_price(state),
    }
    risk_check_final = check_trade(proposed_trade, state["portfolio_state"], limits)
    updates: dict = {"risk_check_final": risk_check_final}

    if not risk_check_final["approved"]:
        override_result = None
        # Only a buy blocked purely on max_position_pct is override-eligible —
        # kill switch, daily loss halt, and max_open_positions stay hard stops.
        if (
            risk_check_final.get("reason_code") == REASON_MAX_POSITION_PCT
            and decision["action"] == "buy"
        ):
            portfolio_state = state["portfolio_state"]
            equity = float(portfolio_state.get("equity", 0.0))
            existing_value = float(
                (portfolio_state.get("open_positions") or {}).get(decision["ticker"], 0.0)
            )
            if decision["size_pct"] > limits.max_override_size_pct:
                # Never ask a human to approve a size this large — it's far more
                # likely a unit error (5.0 meaning "5%") than a real intent, and
                # an approval tap shouldn't be the only thing standing between
                # a bad number and the broker.
                override_result = {
                    "approved": False,
                    "responder": None,
                    "reason": (
                        f"size_pct={decision['size_pct']:.4f} exceeds max_override_size_pct="
                        f"{limits.max_override_size_pct:.4f} — treated as malformed, not sent for approval"
                    ),
                }
            else:
                price = _latest_price(state)
                qty = shares_for(decision["size_pct"], equity, price)
                override_result = request_override(
                    run_id=state["run_id"],
                    ticker=decision["ticker"],
                    requested_size_pct=decision["size_pct"],
                    requested_qty=qty,
                    requested_notional=qty * price,
                    equity=equity,
                    existing_pct=(existing_value / equity) if equity else 0.0,
                    max_position_pct=limits.max_position_pct,
                    reasoning=decision["reasoning"],
                )
            updates["human_override_result"] = override_result

        if override_result and override_result["approved"]:
            approved = dict(decision)
            approved["reasoning"] += (
                f" [Human override approved by {override_result.get('responder') or 'unknown'}"
                " — max_position_pct bypassed for this trade]"
            )
            updates["portfolio_decision"] = approved
            updates["risk_check_final"] = {
                **risk_check_final,
                "approved": True,
                "adjusted_size": decision["size_pct"],
                "reasons": risk_check_final["reasons"]
                + ["human override approved — max_position_pct bypassed for this trade"],
            }
        else:
            forced = dict(decision)
            reasons = "; ".join(risk_check_final["reasons"])
            forced["action"] = "hold"
            forced["size_pct"] = 0.0
            note = f" [OVERRIDDEN: final risk check rejected this trade — {reasons}]"
            if override_result is not None:
                note += f" [human override declined/unavailable — {override_result['reason']}]"
            forced["reasoning"] += note
            updates["portfolio_decision"] = forced
    elif risk_check_final["adjusted_size"] < decision["size_pct"]:
        resized = dict(decision)
        reasons = "; ".join(risk_check_final["reasons"])
        resized["size_pct"] = risk_check_final["adjusted_size"]
        resized["reasoning"] += f" [Final risk check resized size_pct — {reasons}]"
        updates["portfolio_decision"] = resized

    return updates


def route_after_risk_final(state: TradingState) -> str:
    decision = state["portfolio_decision"]
    if not state["risk_check_final"]["approved"] or decision["action"] == "hold":
        return "skip"
    return "proceed"


def human_gate_node(state: TradingState) -> dict:
    decision = state["portfolio_decision"]

    if settings.auto_execute:
        return {"human_approved": True, "human_gate_note": "auto_execute=True — skipped manual confirmation"}

    print(f"\nProposed trade for {decision['ticker']}:")
    print(f"  action:     {decision['action']}")
    print(f"  size_pct:   {decision['size_pct']:.2%}")
    print(f"  confidence: {decision['confidence']}")
    print(f"  reasoning:  {decision['reasoning']}")
    answer = input("Approve this trade? [y/n]: ").strip().lower()
    approved = answer == "y"

    return {
        "human_approved": approved,
        "human_gate_note": "manually approved" if approved else "manually rejected",
    }


def route_after_human_gate(state: TradingState) -> str:
    return "proceed" if state.get("human_approved") else "skip"


# A sell sized at or above the position's share of equity means "exit the
# whole position". The risk manager rounds adjusted_size to 6 decimals, so
# a full exit can come back up to 5e-7 under the exact fraction.
FULL_EXIT_TOLERANCE = 1e-6


def _order_qty(portfolio_state: dict, decision: dict, price: float) -> int:
    """Whole shares to order. Buys are dollars / price. Sells are held shares:
    a sell's size_pct comes from Alpaca's market value but price is the latest
    yfinance close, so dollars / price can land one share off the real
    position either way — leaving a 1-share stub on a full exit, or selling
    shares that aren't held (including a pending buy that hasn't filled,
    which open_positions counts as exposure)."""
    equity = float(portfolio_state["equity"])
    qty = shares_for(decision["size_pct"], equity, price)
    if decision["action"] != "sell":
        return qty

    ticker = decision["ticker"]
    position = (portfolio_state.get("position_details") or {}).get(ticker) or {}
    held = int(float(position.get("qty", 0.0)))
    existing_pct = float((portfolio_state.get("open_positions") or {}).get(ticker, 0.0)) / equity
    if decision["size_pct"] >= existing_pct - FULL_EXIT_TOLERANCE:
        return held
    return min(qty, held)

def execution_node(state: TradingState) -> dict:
    decision = state["portfolio_decision"]
    try:
        from execution.alpaca_executor import submit_order
    except ImportError:
        print(
            "[execution] execution/alpaca_executor.py not yet built (Phase 6) — "
            f"would have submitted: {decision['action']} {decision['ticker']} "
            f"at {decision['size_pct']:.2%} of equity."
        )
        return {
            "execution_result": {
                "status": "not_implemented",
                "note": "execution/alpaca_executor.py not yet built (Phase 6)",
            }
        }

    latest_price = _latest_price(state)
    qty = _order_qty(state["portfolio_state"], decision, latest_price)

    # Backstop: risk_final_check already rejects anything under one share, but
    # a sell also lands here when none of the position has filled yet.
    if qty <= 0:
        print(
            f"[execution] computed qty=0 for {decision['ticker']} "
            f"(size_pct={decision['size_pct']:.4f} too small at price {latest_price}) "
            "— skipping order submission."
        )
        return {
            "execution_result": {
                "status": "skipped_zero_qty",
                "note": "size_pct rounded down to zero shares at the current price",
            }
        }

    result = submit_order(
        decision["ticker"], decision["action"], qty=qty,
        run_id=state["run_id"], reference_price=latest_price,
    )
    return {"execution_result": result}


def log_and_end_node(state: TradingState) -> dict:
    agent_outputs = {
        "technical_signal": state.get("technical_signal"),
        "sentiment_signal": state.get("sentiment_signal"),
        "fundamentals_signal": state.get("fundamentals_signal"),
        "risk_check": state.get("risk_check"),
        "risk_check_final": state.get("risk_check_final"),
        "human_override_result": state.get("human_override_result"),
        "human_gate_note": state.get("human_gate_note"),
        "position_review": state.get("position_review"),
    }

    log_decision(
        run_id=state["run_id"],
        timestamp=datetime.now(timezone.utc),
        agent_outputs=agent_outputs,
        final_decision=state.get("portfolio_decision"),
        execution_result=state.get("execution_result"),
    )
    print(f"\n[log] run {state['run_id']} appended to logs/trades.jsonl")
    return {}


def build_graph():
    graph = StateGraph(TradingState)

    graph.add_node("market_data", market_data_node)
    graph.add_node("sentiment", sentiment_node)
    graph.add_node("technical", technical_node)
    graph.add_node("risk_precheck", risk_precheck_node)
    graph.add_node("portfolio_manager", portfolio_manager_node)
    graph.add_node("risk_final_check", risk_final_check_node)
    graph.add_node("human_gate", human_gate_node)
    graph.add_node("execution", execution_node)
    graph.add_node("log_and_end", log_and_end_node)

    graph.add_edge(START, "market_data")
    graph.add_edge(START, "sentiment")
    graph.add_edge("market_data", "technical")
    graph.add_edge(["technical", "sentiment"], "risk_precheck")
    graph.add_edge("risk_precheck", "portfolio_manager")
    graph.add_edge("portfolio_manager", "risk_final_check")

    graph.add_conditional_edges(
        "risk_final_check",
        route_after_risk_final,
        {"proceed": "human_gate", "skip": "log_and_end"},
    )
    graph.add_conditional_edges(
        "human_gate",
        route_after_human_gate,
        {"proceed": "execution", "skip": "log_and_end"},
    )
    graph.add_edge("execution", "log_and_end")
    graph.add_edge("log_and_end", END)

    return graph.compile()


def run_trading_cycle(
    ticker: str, portfolio_state: dict, risk_limits: RiskLimits | None = None
) -> dict:
    app = build_graph()
    initial_state: TradingState = {
        "run_id": uuid.uuid4().hex,
        "ticker": ticker,
        "portfolio_state": portfolio_state,
    }
    if risk_limits is not None:
        initial_state["risk_limits"] = risk_limits
    return app.invoke(initial_state)
