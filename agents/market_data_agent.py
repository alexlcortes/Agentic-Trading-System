import logging
import time
from datetime import date
from typing import Callable

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 2.0


class TickerDataError(Exception):
    """Raised when price data for a ticker cannot be retrieved."""


def _fetch_with_retry(ticker: str, fetch_fn: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = fetch_fn()
        except Exception as exc:  # yfinance raises varied exception types on network/rate-limit errors
            last_error = exc
            logger.warning(
                "Attempt %d/%d failed fetching %s: %s", attempt, MAX_RETRIES, ticker, exc
            )
        else:
            if data.empty:
                raise TickerDataError(
                    f"No price data returned for '{ticker}' — likely invalid or delisted ticker, "
                    "or no trading days in the requested range"
                )
            return data[["Open", "High", "Low", "Close", "Volume"]]

        if attempt < MAX_RETRIES:
            backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            time.sleep(backoff)

    raise TickerDataError(
        f"Failed to fetch price data for '{ticker}' after {MAX_RETRIES} attempts"
    ) from last_error


def get_price_data(ticker: str, lookback_days: int = 120) -> pd.DataFrame:
    """Fetch OHLCV data for `ticker` over the trailing `lookback_days` from
    today. For live trading — the returned window always ends "now".

    Default of 120 calendar days leaves enough trading days for a stable
    50-day SMA (technical_agent needs 52+ rows) after accounting for
    weekends/holidays.

    Retries with exponential backoff on transient/rate-limit failures.
    Raises TickerDataError for invalid or delisted tickers (empty result
    after a successful request), or if all retries are exhausted.
    """
    if not ticker or not ticker.strip():
        raise TickerDataError("ticker must be a non-empty string")

    ticker = ticker.strip().upper()
    period = f"{lookback_days}d"
    return _fetch_with_retry(ticker, lambda: yf.Ticker(ticker).history(period=period, auto_adjust=False))


def get_historical_price_data(ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Fetch OHLCV data for `ticker` over an explicit [start_date, end_date]
    range — for backtesting, where the window is anchored to specific
    historical dates rather than "N days back from today".

    Same retry/error-handling behavior as get_price_data.
    """
    if not ticker or not ticker.strip():
        raise TickerDataError("ticker must be a non-empty string")
    if start_date >= end_date:
        raise ValueError(f"start_date ({start_date}) must be before end_date ({end_date})")

    ticker = ticker.strip().upper()
    return _fetch_with_retry(
        ticker,
        lambda: yf.Ticker(ticker).history(start=start_date, end=end_date, auto_adjust=False),
    )
