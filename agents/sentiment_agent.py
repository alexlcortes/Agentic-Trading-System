from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field
from tavily import TavilyClient

from config import settings

MAX_HEADLINES = 8


class SentimentResult(BaseModel):
    sentiment: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    key_headlines: list[str]
    reasoning: str


def _fetch_headlines(ticker: str) -> list[str]:
    client = TavilyClient(api_key=settings.TAVILY_API_KEY)
    response = client.search(
        query=f"{ticker} stock news",
        topic="news",
        days=7,
        max_results=MAX_HEADLINES,
    )
    return [result["title"] for result in response.get("results", []) if result.get("title")]


def get_news_sentiment(ticker: str) -> dict:
    """Fetch recent headlines for `ticker` and have the LLM classify sentiment.

    Returns a dict matching SentimentResult's schema. If no headlines are
    found, returns a neutral, low-confidence result rather than guessing.
    """
    ticker = ticker.strip().upper()
    headlines = _fetch_headlines(ticker)

    if not headlines:
        return SentimentResult(
            sentiment="neutral",
            confidence=0.0,
            key_headlines=[],
            reasoning=f"No recent news headlines found for {ticker}.",
        ).model_dump()

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a financial news sentiment classifier. Given recent "
                    "headlines about a stock, classify overall sentiment as it "
                    "would relate to near-term price direction. Base your answer "
                    "only on the headlines provided — do not invent facts."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Ticker: {ticker}\n\nRecent headlines:\n"
                    + "\n".join(f"- {h}" for h in headlines)
                ),
            },
        ],
        response_format=SentimentResult,
    )

    result = completion.choices[0].message.parsed
    return result.model_dump()
