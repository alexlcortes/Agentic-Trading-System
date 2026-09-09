from typing import Literal

import pandas as pd
from openai import OpenAI
from pydantic import BaseModel, Field

from config import settings

RSI_PERIOD = 14
SMA_SHORT_WINDOW = 20
SMA_LONG_WINDOW = 50
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

MIN_REQUIRED_ROWS = SMA_LONG_WINDOW + 2  # need a prior day too, to detect a crossover


class TechnicalSignal(BaseModel):
    signal: Literal["buy", "sell", "hold"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


def _compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder's smoothing, the standard RSI averaging method
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _compute_sma_crossover(
    close: pd.Series, short_window: int = SMA_SHORT_WINDOW, long_window: int = SMA_LONG_WINDOW
) -> tuple[pd.Series, pd.Series]:
    sma_short = close.rolling(window=short_window).mean()
    sma_long = close.rolling(window=long_window).mean()
    return sma_short, sma_long


def _compute_macd(
    close: pd.Series, fast: int = MACD_FAST, slow: int = MACD_SLOW, signal: int = MACD_SIGNAL
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _crossover_state(sma_short: pd.Series, sma_long: pd.Series) -> str:
    diff_today = sma_short.iloc[-1] - sma_long.iloc[-1]
    diff_yesterday = sma_short.iloc[-2] - sma_long.iloc[-2]

    if diff_yesterday <= 0 < diff_today:
        return "golden_cross"  # short crossed above long — bullish
    if diff_yesterday >= 0 > diff_today:
        return "death_cross"  # short crossed below long — bearish
    return "above" if diff_today > 0 else "below"


def _summarize_indicators(price_data: pd.DataFrame) -> dict:
    close = price_data["Close"]

    rsi = _compute_rsi(close)
    sma_short, sma_long = _compute_sma_crossover(close)
    macd_line, signal_line, histogram = _compute_macd(close)

    return {
        "rsi_14": round(rsi.iloc[-1], 2),
        "sma_20": round(sma_short.iloc[-1], 2),
        "sma_50": round(sma_long.iloc[-1], 2),
        "sma_crossover_state": _crossover_state(sma_short, sma_long),
        "macd_line": round(macd_line.iloc[-1], 4),
        "macd_signal_line": round(signal_line.iloc[-1], 4),
        "macd_histogram": round(histogram.iloc[-1], 4),
        "latest_close": round(close.iloc[-1], 2),
    }


def get_technical_signal(price_data: pd.DataFrame) -> dict:
    """Compute RSI/SMA-crossover/MACD from OHLCV data and have the LLM
    interpret them into a buy/sell/hold signal.

    Indicator math stays in plain pandas; the LLM only ever sees the
    already-computed numbers, never raw price data, and its only job is
    to interpret them, not calculate anything.
    """
    if len(price_data) < MIN_REQUIRED_ROWS:
        raise ValueError(
            f"Need at least {MIN_REQUIRED_ROWS} rows of price data to compute "
            f"indicators reliably (SMA{SMA_LONG_WINDOW} plus one prior day for "
            f"crossover detection), got {len(price_data)}"
        )

    indicators = _summarize_indicators(price_data)

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a technical analysis signal interpreter. You will be "
                    "given already-computed technical indicators for a stock — "
                    "RSI(14), a 20/50-day SMA crossover state, and MACD. Interpret "
                    "these indicators together into a single buy/sell/hold signal. "
                    "Do not recompute or second-guess the numbers themselves, and "
                    "do not invent data you were not given — reason only from the "
                    "indicators provided."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Technical indicators:\n"
                    f"- Latest close: {indicators['latest_close']}\n"
                    f"- RSI(14): {indicators['rsi_14']} "
                    "(>70 typically overbought, <30 typically oversold)\n"
                    f"- SMA(20): {indicators['sma_20']}, SMA(50): {indicators['sma_50']}\n"
                    f"- SMA crossover state: {indicators['sma_crossover_state']} "
                    "(golden_cross = short crossed above long just now, bullish; "
                    "death_cross = short crossed below long just now, bearish; "
                    "above/below = no cross today, just current relative position)\n"
                    f"- MACD line: {indicators['macd_line']}, "
                    f"signal line: {indicators['macd_signal_line']}, "
                    f"histogram: {indicators['macd_histogram']}"
                ),
            },
        ],
        response_format=TechnicalSignal,
    )

    result = completion.choices[0].message.parsed
    return result.model_dump()
