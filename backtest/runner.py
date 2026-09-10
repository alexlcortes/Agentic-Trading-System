"""Replay the full agent pipeline day-by-day over historical data.

KNOWN LIMITATION (read before trusting results): sentiment and
fundamentals both depend on "current" free-tier data sources (Tavily
news search, yfinance .info) with no historical point-in-time access.
Calling them during a backtest would leak today's information into a
simulated past day — lookahead bias that would silently invalidate the
whole test. So this backtest replaces sentiment with an explicit
"no information" stub (confidence=0.0) and omits fundamentals entirely.
This validates the technical-signal + risk-manager side of the
strategy; it does NOT validate sentiment's real contribution to live
decisions.

Execution is simulated, not real: a filled order uses the NEXT trading
day's Open price (decide after day D's close using day D's data, fill
at D+1's open) — using day D's own close would be a mild lookahead/
optimism bias. Fills are also cash-constrained here (can't spend more
than the simulated cash balance), a simulation-level safety layer on
top of check_trade's equity-pct constraint, since check_trade has no
concept of literal cash.

Every simulated decision is logged through the same audit_logger as
live trading (log_decision), with run_id prefixed "backtest-" so the
two are distinguishable in logs/trades.jsonl.
"""

import logging
from datetime import date, datetime, timezone

import pandas as pd

from agents.market_data_agent import TickerDataError, get_historical_price_data
from agents.portfolio_manager import synthesize_decision
from agents.risk_manager import check_trade
from agents.technical_agent import MIN_REQUIRED_ROWS, get_technical_signal
from config.settings import RiskLimits
from logs.audit_logger import log_decision

logger = logging.getLogger(__name__)

WARMUP_CALENDAR_DAYS = 120  # matches market_data_agent's live default lookback

NEUTRAL_SENTIMENT_STUB = {
    "sentiment": "neutral",
    "confidence": 0.0,
    "key_headlines": [],
    "reasoning": (
        "Sentiment unavailable during backtesting — free-tier news search has "
        "no historical point-in-time headlines, only current ones. Using a "
        "0-confidence stub rather than a real neutral reading so the "
        "portfolio manager doesn't weight this input."
    ),
}


def _fetch_all(tickers: list[str], eval_start: date, eval_end: date) -> dict[str, pd.DataFrame]:
    warmup_start = eval_start - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
    data = {}
    for ticker in tickers:
        data[ticker] = get_historical_price_data(ticker, warmup_start, eval_end)
    return data


def _trading_days_in_window(price_data: dict[str, pd.DataFrame], eval_start: date, eval_end: date) -> list:
    all_dates = set()
    for df in price_data.values():
        for ts in df.index:
            d = ts.date()
            if eval_start <= d < eval_end:
                all_dates.add(d)
    return sorted(all_dates)


def _mark_to_market(cash: float, positions: dict, closes_today: dict[str, float]) -> float:
    equity = cash
    for ticker, pos in positions.items():
        if pos["qty"] > 0 and ticker in closes_today:
            equity += pos["qty"] * closes_today[ticker]
    return equity


def run_backtest(
    tickers: list[str] | None,
    eval_start: date,
    eval_end: date,
    initial_equity: float = 100_000.0,
    risk_limits: RiskLimits | None = None,
) -> dict:
    """Replay the pipeline over [eval_start, eval_end) (end exclusive,
    matching yfinance's own convention) and report performance vs. an
    equal-weight buy-and-hold baseline on the same tickers.

    tickers=None uses risk_limits.watchlist (or RiskLimits()'s default
    watchlist if risk_limits isn't given either) — the config's watchlist
    is the single source of truth for "which tickers can this system
    trade," not a second list that has to be kept in sync with it by hand.
    """
    limits = risk_limits or RiskLimits()
    tickers = tickers if tickers is not None else limits.watchlist
    price_data = _fetch_all(tickers, eval_start, eval_end)
    sim_days = _trading_days_in_window(price_data, eval_start, eval_end)

    if not sim_days:
        raise ValueError(f"No trading days found in [{eval_start}, {eval_end}) for {tickers}")

    cash = initial_equity
    positions: dict[str, dict] = {t: {"qty": 0.0, "avg_price": 0.0} for t in tickers}
    pending_fills: list[dict] = []  # orders decided on day D, filled at D+1's open

    equity_curve: list[tuple] = []
    risk_precheck_rejections = 0
    risk_final_rejections = 0
    closed_trades: list[float] = []  # realized P&L per closed round trip
    previous_day_equity = initial_equity  # baseline for each day's daily_realized_pnl

    for day_idx, sim_day in enumerate(sim_days):
        is_last_day = day_idx == len(sim_days) - 1
        closes_today: dict[str, float] = {}

        # 1. Fill any orders decided on the previous day, at today's open.
        still_pending = []
        for order in pending_fills:
            ticker = order["ticker"]
            df = price_data[ticker]
            todays_rows = df[df.index.date == sim_day]
            if todays_rows.empty:
                still_pending.append(order)  # ticker didn't trade today, try again next day
                continue

            fill_price = float(todays_rows["Open"].iloc[0])
            pos = positions[ticker]

            if order["action"] == "buy":
                affordable_qty = int(cash // fill_price)
                qty = min(order["qty"], affordable_qty)
                if qty <= 0:
                    logger.info("Backtest: skipping buy for %s on %s — insufficient cash", ticker, sim_day)
                else:
                    cost = qty * fill_price
                    new_qty = pos["qty"] + qty
                    pos["avg_price"] = ((pos["qty"] * pos["avg_price"]) + (qty * fill_price)) / new_qty
                    pos["qty"] = new_qty
                    cash -= cost
                order["filled_qty"] = qty
                order["fill_price"] = fill_price
            elif order["action"] == "sell":
                qty = min(order["qty"], pos["qty"])
                if qty <= 0:
                    logger.info("Backtest: skipping sell for %s on %s — no position held", ticker, sim_day)
                else:
                    proceeds = qty * fill_price
                    realized_pnl = (fill_price - pos["avg_price"]) * qty
                    closed_trades.append(realized_pnl)
                    pos["qty"] -= qty
                    if pos["qty"] <= 0:
                        pos["qty"] = 0.0
                        pos["avg_price"] = 0.0
                    cash += proceeds
                order["filled_qty"] = qty
                order["fill_price"] = fill_price

            log_decision(
                run_id=f"backtest-{order['ticker']}-{order['decided_on']}-fill",
                timestamp=datetime.combine(sim_day, datetime.min.time(), tzinfo=timezone.utc),
                agent_outputs={"note": "simulated fill of prior day's decision"},
                final_decision=order["decision"],
                execution_result={
                    "status": "filled" if order.get("filled_qty", 0) > 0 else "skipped",
                    "filled_qty": order.get("filled_qty", 0),
                    "fill_price": order.get("fill_price"),
                },
            )

        pending_fills = still_pending

        # 2. Decide today's actions for each ticker, using data through today's close only.
        # Boolean masking on .index.date (not .loc[:Timestamp]) sidesteps tz-aware
        # vs. tz-naive comparison errors from yfinance's tz-aware index.
        slices_today = {t: price_data[t][price_data[t].index.date <= sim_day] for t in tickers}
        open_positions_value = {
            t: positions[t]["qty"] * float(slices_today[t]["Close"].iloc[-1])
            for t in tickers
            if positions[t]["qty"] > 0 and not slices_today[t].empty
        }
        equity_snapshot = cash + sum(open_positions_value.values())
        # Same approximation as execution/alpaca_executor.py's get_portfolio_state
        # (equity - last_equity): today's equity change vs. yesterday's close,
        # not a pure realized-only figure, computed once per day and applied
        # account-wide to every ticker's check that day (not reset per ticker).
        daily_pnl_so_far = equity_snapshot - previous_day_equity

        for ticker in tickers:
            slice_df = slices_today[ticker]
            closes_today[ticker] = float(slice_df["Close"].iloc[-1]) if not slice_df.empty else None

            if len(slice_df) < MIN_REQUIRED_ROWS:
                continue  # still in warmup for this ticker

            try:
                technical_signal = get_technical_signal(slice_df)
            except (ValueError, TickerDataError) as exc:
                logger.warning("Backtest: technical signal failed for %s on %s: %s", ticker, sim_day, exc)
                continue

            portfolio_state = {
                "equity": equity_snapshot,
                "open_positions": open_positions_value,
                "daily_realized_pnl": daily_pnl_so_far,
            }

            action = technical_signal["signal"]
            if action == "hold":
                proposed = {"ticker": ticker, "action": "hold", "size_pct": 0.0}
            else:
                proposed = {"ticker": ticker, "action": action, "size_pct": limits.max_position_pct}
            risk_check = check_trade(proposed, portfolio_state, limits)
            if not risk_check["approved"]:
                risk_precheck_rejections += 1

            decision = synthesize_decision(
                ticker=ticker,
                technical_signal=technical_signal,
                sentiment_signal=NEUTRAL_SENTIMENT_STUB,
                risk_check=risk_check,
                fundamentals_signal=None,
            )

            final_check = check_trade(
                {"ticker": decision["ticker"], "action": decision["action"], "size_pct": decision["size_pct"]},
                portfolio_state,
                limits,
            )
            if not final_check["approved"]:
                risk_final_rejections += 1
                decision = dict(decision)
                decision["action"] = "hold"
                decision["size_pct"] = 0.0
            elif final_check["adjusted_size"] < decision["size_pct"]:
                decision = dict(decision)
                decision["size_pct"] = final_check["adjusted_size"]

            execution_result = None
            if decision["action"] != "hold" and not is_last_day:
                target_qty = int((decision["size_pct"] * equity_snapshot) // closes_today[ticker])
                if target_qty > 0:
                    order = {
                        "ticker": ticker,
                        "action": decision["action"],
                        "qty": target_qty,
                        "decided_on": sim_day.isoformat(),
                        "decision": decision,
                    }
                    pending_fills.append(order)
                    execution_result = {"status": "pending_fill_next_open", "qty": target_qty}

            log_decision(
                run_id=f"backtest-{ticker}-{sim_day.isoformat()}",
                timestamp=datetime.combine(sim_day, datetime.min.time(), tzinfo=timezone.utc),
                agent_outputs={
                    "technical_signal": technical_signal,
                    "sentiment_signal": NEUTRAL_SENTIMENT_STUB,
                    "risk_check": risk_check,
                    "risk_check_final": final_check,
                },
                final_decision=decision,
                execution_result=execution_result,
            )

        # 3. Mark the whole portfolio to market at today's close for the equity curve.
        today_equity = _mark_to_market(cash, positions, closes_today)
        equity_curve.append((sim_day, today_equity))
        previous_day_equity = today_equity

    final_equity = equity_curve[-1][1]
    total_return = (final_equity / initial_equity) - 1

    peak = equity_curve[0][1]
    max_drawdown = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        max_drawdown = max(max_drawdown, drawdown)

    wins = sum(1 for pnl in closed_trades if pnl > 0)
    win_rate = wins / len(closed_trades) if closed_trades else None

    baseline_per_ticker = initial_equity / len(tickers)
    baseline_final = 0.0
    for ticker in tickers:
        df = price_data[ticker]
        window_df = df[(df.index.date >= eval_start) & (df.index.date < eval_end)]
        start_price = float(window_df["Close"].iloc[0])
        end_price = float(window_df["Close"].iloc[-1])
        shares = baseline_per_ticker / start_price
        baseline_final += shares * end_price
    buy_and_hold_return = (baseline_final / initial_equity) - 1

    return {
        "tickers": tickers,
        "eval_start": eval_start.isoformat(),
        "eval_end": eval_end.isoformat(),
        "trading_days": len(sim_days),
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "total_return": total_return,
        "buy_and_hold_return": buy_and_hold_return,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "closed_trades": len(closed_trades),
        "risk_precheck_rejections": risk_precheck_rejections,
        "risk_final_rejections": risk_final_rejections,
        "equity_curve": equity_curve,
    }
