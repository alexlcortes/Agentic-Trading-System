"""Commit order for a day's decisions, shared by run_daily and the backtest
so both fill open position slots the same way."""


def is_buy(state: dict) -> bool:
    return (state.get("portfolio_decision") or {}).get("action") == "buy"


def rank_for_commit(decided: list[dict]) -> list[dict]:
    """Buys by the portfolio manager's confidence, highest first, then
    everything else. Pending buys count toward max_open_positions, so this
    order decides which buys get the open slots. Before it, slots went by
    watchlist order — on 2026-10-13 MSFT and V took the last ones while
    ABBV, the strongest technical buy, was blocked. Ties keep input order.

    Each item needs "portfolio_decision"; a decide_trade state works as is.
    """
    return sorted(
        decided,
        key=lambda s: (0, -s["portfolio_decision"]["confidence"]) if is_buy(s) else (1, 0.0),
    )
