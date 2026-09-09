import logging

import yfinance as yf
from openai import OpenAI
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

METRIC_KEYS = [
    "trailingPE",
    "forwardPE",
    "sector",
    "industry",
    "marketCap",
    "priceToBook",
    "dividendYield",
    "beta",
    "profitMargins",
]

# Below this many populated metrics, don't bother calling the LLM — free-tier
# yfinance .info is often stale or missing fields entirely, and asking the LLM
# to flag "unusual" values against near-empty data invites hallucination.
MIN_POPULATED_METRICS = 4


class FundamentalsFlag(BaseModel):
    unusual: bool
    flags: list[str]
    reasoning: str


def _fetch_metrics(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info
    except Exception as exc:
        logger.warning("Failed to fetch fundamentals info for %s: %s", ticker, exc)
        return {}
    return {key: info.get(key) for key in METRIC_KEYS}


def get_fundamentals(ticker: str) -> dict:
    """Pull basic fundamentals via yfinance and have the LLM flag anything
    unusual relative to typical sector norms.

    This is a minor, advisory input — not a primary trading signal.
    Free-source fundamentals data (yfinance's .info) is frequently stale
    or incomplete, so treat this agent's output as a soft flag, never as
    grounds on its own for a trade decision.
    """
    ticker = ticker.strip().upper()
    metrics = _fetch_metrics(ticker)
    populated = {k: v for k, v in metrics.items() if v is not None}

    if len(populated) < MIN_POPULATED_METRICS:
        return FundamentalsFlag(
            unusual=False,
            flags=[],
            reasoning=(
                f"Insufficient fundamentals data available for {ticker} "
                f"({len(populated)}/{len(METRIC_KEYS)} metrics populated) — "
                "skipping analysis rather than guessing from partial data."
            ),
        ).model_dump()

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a fundamentals sanity-checker, not a valuation model. "
                    "Given basic metrics for a stock, flag anything that looks "
                    "unusual relative to typical norms for its sector/industry — "
                    "e.g. a P/E far outside sector norms, an unusually high beta, "
                    "or a priceToBook far from typical. This data may be stale or "
                    "incomplete; if a metric is missing, do not guess its value. "
                    "Only flag things you have reasonable general knowledge to "
                    "judge as unusual — do not fabricate sector benchmark numbers."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Ticker: {ticker}\n\nMetrics:\n"
                    + "\n".join(f"- {k}: {v}" for k, v in metrics.items())
                ),
            },
        ],
        response_format=FundamentalsFlag,
    )

    result = completion.choices[0].message.parsed
    return result.model_dump()
