import logging
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from config import settings

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0
POLL_TIMEOUT_SECONDS = 30.0

TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.DONE_FOR_DAY,
    OrderStatus.STOPPED,
    OrderStatus.SUSPENDED,
}


def _get_client() -> TradingClient:
    """Paper vs. live is driven entirely by ALPACA_BASE_URL (.env) — this
    module never chooses an endpoint itself, so switching environments is
    a config-only change, never a code change. `paper` is inferred from
    the same URL rather than set independently, so there's only one
    setting to get right, not two that could drift out of sync."""
    is_paper = "paper" in settings.ALPACA_BASE_URL.lower()
    return TradingClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
        paper=is_paper,
        url_override=settings.ALPACA_BASE_URL,
    )


def get_portfolio_state() -> dict:
    """Fetch real account state from Alpaca, shaped to match check_trade's
    portfolio_state contract: {"equity", "open_positions", "daily_realized_pnl"}.

    daily_realized_pnl is approximated as equity - last_equity (Alpaca's
    equity as of the previous session's close) — this is actually equity
    *change* since yesterday's close (realized + unrealized), not a pure
    realized-only figure, since Alpaca doesn't expose one directly. This
    is the more protective choice for a daily-loss circuit breaker: an
    account down today on paper losses should still trip the halt, not
    just one down on closed trades.
    """
    client = _get_client()
    account = client.get_account()
    positions = client.get_all_positions()

    equity = float(account.equity)
    last_equity = float(account.last_equity)

    return {
        "equity": equity,
        "open_positions": {p.symbol: float(p.market_value) for p in positions},
        "daily_realized_pnl": equity - last_equity,
    }


def submit_order(ticker: str, side: str, qty: float) -> dict:
    """Submit a market order and poll until it reaches a terminal state.

    Returns a structured result dict, never raises on broker-side failures
    (rejections, partial fills, timeouts) — those are reported in the
    returned status/error fields so callers (and the audit log) always get
    a structured outcome. Only raises for invalid input (bad side/qty) or
    if order submission itself fails to reach the broker at all.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty!r}")

    client = _get_client()
    order_request = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )

    try:
        order = client.submit_order(order_data=order_request)
    except Exception as exc:
        logger.error("Order submission failed for %s %s %s: %s", side, qty, ticker, exc)
        return {
            "order_id": None,
            "status": "submission_failed",
            "filled_qty": 0.0,
            "filled_avg_price": None,
            "requested_qty": float(qty),
            "side": side,
            "ticker": ticker,
            "error": str(exc),
        }

    order_id = order.id
    elapsed = 0.0
    while order.status not in TERMINAL_STATUSES and elapsed < POLL_TIMEOUT_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS
        order = client.get_order_by_id(order_id)

    if order.status in TERMINAL_STATUSES:
        status = order.status.value
    elif order.status == OrderStatus.PARTIALLY_FILLED:
        # Explicitly distinguished from a generic timeout: there IS real
        # fill exposure here, which matters to whoever reads this result.
        status = "partially_filled_timeout"
        logger.warning(
            "Order %s for %s still partially filled after %.0fs poll timeout",
            order_id, ticker, POLL_TIMEOUT_SECONDS,
        )
    else:
        status = "poll_timeout"
        logger.warning(
            "Order %s for %s did not reach a terminal state within %.0fs (last status: %s)",
            order_id, ticker, POLL_TIMEOUT_SECONDS, order.status,
        )

    filled_qty = float(order.filled_qty) if order.filled_qty is not None else 0.0
    filled_avg_price = (
        float(order.filled_avg_price) if order.filled_avg_price is not None else None
    )

    error = None
    if order.status == OrderStatus.REJECTED:
        error = "order rejected by broker"
    elif status == "poll_timeout":
        error = f"order did not reach a terminal state within {POLL_TIMEOUT_SECONDS:.0f}s"

    if order.status == OrderStatus.PARTIALLY_FILLED or status == "partially_filled_timeout":
        logger.info("Order %s partially filled: %s/%s", order_id, filled_qty, qty)

    return {
        "order_id": str(order_id),
        "status": status,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price,
        "requested_qty": float(qty),
        "side": side,
        "ticker": ticker,
        "error": error,
    }
