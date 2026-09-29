"""Sells are sized from held shares, not dollars / price: a sell's size_pct
comes from Alpaca's market value while execution prices it at the latest
yfinance close, so the two could disagree by a share either way.
"""

import pandas as pd
import pytest

from execution import alpaca_executor
from orchestration import graph


def _portfolio(held_qty=12.0, market_value=4_160.40, equity=100_000.0):
    return {
        "equity": equity,
        "open_positions": {"GOOGL": market_value},
        "position_details": {"GOOGL": {"qty": held_qty}} if held_qty else {},
    }


def _sell(size_pct):
    return {"ticker": "GOOGL", "action": "sell", "size_pct": size_pct}


def test_full_exit_sells_every_share_when_close_is_above_alpaca_price():
    # 12 sh at Alpaca's $346.70; dollars / $347.50 close floors to 11.
    assert graph.shares_for(0.041604, 100_000.0, 347.50) == 11
    assert graph._order_qty(_portfolio(), _sell(0.041604), 347.50) == 12


def test_full_exit_never_sells_more_than_is_held_when_close_is_below():
    # dollars / $320 close would be 13 — one share more than is held.
    assert graph.shares_for(0.041604, 100_000.0, 320.0) == 13
    assert graph._order_qty(_portfolio(), _sell(0.041604), 320.0) == 12


def test_full_exit_within_rounding_of_the_exact_fraction():
    # check_trade rounds adjusted_size to 6 decimals, so it can land just under.
    portfolio = _portfolio(market_value=4_160.4049)
    assert graph._order_qty(portfolio, _sell(0.041604), 347.50) == 12


def test_partial_sell_is_sized_by_dollars():
    assert graph._order_qty(_portfolio(), _sell(0.02), 346.70) == 5


def test_partial_sell_is_capped_at_held_shares():
    # Exposure includes an unfilled pending buy; only 3 shares are filled.
    portfolio = _portfolio(held_qty=3.0, market_value=4_160.40)
    assert graph._order_qty(portfolio, _sell(0.03), 346.70) == 3


def test_sell_with_nothing_filled_yet_orders_zero():
    assert graph._order_qty(_portfolio(held_qty=None), _sell(0.041604), 346.70) == 0


def test_buys_are_unchanged():
    buy = {"ticker": "GOOGL", "action": "buy", "size_pct": 0.02}
    assert graph._order_qty(_portfolio(), buy, 346.70) == 5


def test_execution_submits_the_held_quantity(monkeypatch):
    sent = {}

    def fake_submit(ticker, side, qty, run_id=None, reference_price=None):
        sent.update(ticker=ticker, side=side, qty=qty)
        return {"status": "filled"}

    monkeypatch.setattr(alpaca_executor, "submit_order", fake_submit)
    graph.execution_node({
        "run_id": "test",
        "portfolio_state": _portfolio(),
        "portfolio_decision": _sell(0.041604),
        "price_data": pd.DataFrame({"Close": [347.50]}),
    })
    assert sent == {"ticker": "GOOGL", "side": "sell", "qty": 12}
