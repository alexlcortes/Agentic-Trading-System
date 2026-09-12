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

import uuid
from datetime import datetime, timezone
from typing import Optional, TypedDict

import pandas as pd
from langgraph.graph import END, START, StateGraph

from agents.market_data_agent import get_price_data
from agents.portfolio_manager import synthesize_decision
from agents.risk_manager import check_trade
from agents.sentiment_agent import get_news_sentiment
from agents.technical_agent import get_technical_signal
from config import settings
from config.settings import RiskLimits
from logs.audit_logger import log_decision


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

    human_approved: bool
    human_gate_note: str
    execution_result: Optional[dict]


def _limits(state: TradingState) -> RiskLimits:
    return state.get("risk_limits") or RiskLimits()


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
    return {"portfolio_decision": decision}


def risk_final_check_node(state: TradingState) -> dict:
    """The authoritative gate — re-checks whatever action/size the
    portfolio manager actually decided on, not the precheck candidate."""
    limits = _limits(state)
    decision = state["portfolio_decision"]

    proposed_trade = {
        "ticker": decision["ticker"],
        "action": decision["action"],
        "size_pct": decision["size_pct"],
    }
    risk_check_final = check_trade(proposed_trade, state["portfolio_state"], limits)
    updates: dict = {"risk_check_final": risk_check_final}

    if not risk_check_final["approved"]:
        forced = dict(decision)
        reasons = "; ".join(risk_check_final["reasons"])
        forced["action"] = "hold"
        forced["size_pct"] = 0.0
        forced["reasoning"] += f" [OVERRIDDEN: final risk check rejected this trade — {reasons}]"
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

    # size_pct -> shares, using the latest close as a sizing approximation
    # (the market order itself will fill at the actual current price).
    latest_price = float(state["price_data"]["Close"].iloc[-1])
    equity = float(state["portfolio_state"]["equity"])
    qty = int((decision["size_pct"] * equity) // latest_price)

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
        "human_gate_note": state.get("human_gate_note"),
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
