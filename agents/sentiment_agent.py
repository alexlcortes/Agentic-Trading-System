import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal
from urllib.parse import urlparse

from openai import OpenAI
from pydantic import BaseModel, Field
from tavily import TavilyClient

from config import settings

MAX_HEADLINES = 8
# Ask Tavily for more than we keep: on 2026-09-24, 7 of SPY's 8 results were
# quote/option-chain pages, which _filter_results drops.
SEARCH_RESULTS = 20
# Enforced here from each result's own published_date, not just trusted to
# the search API's `days` parameter.
MAX_HEADLINE_AGE_DAYS = 7

# Quote pages and option-contract listings come back from a news search
# because their titles say "News" and they're updated constantly, so they
# always look recent. They carry no information about the stock.
_LISTING_TITLE = re.compile(
    r"stock (price|quote)\b.*\b(price|quote|history|forecast)"
    r"|quote.*history"
    r"|\b[A-Z]{1,5}\d{6}[CP]\d{8}\b",
    re.IGNORECASE,
)
_LISTING_URL = re.compile(r"/quote/|/stock/[A-Z0-9.\-]+-US\b", re.IGNORECASE)


class SentimentResult(BaseModel):
    sentiment: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)
    key_headlines: list[str]
    reasoning: str


SYSTEM_PROMPT = (
    "You are a financial news sentiment classifier. Given dated headlines "
    "about a stock, classify overall sentiment as it would relate to "
    "near-term price direction. Base your answer only on the headlines "
    "provided — do not invent facts.\n\n"
    "Each headline is tagged with its publish date, age in days, and source. "
    "Relative time words in a headline (\"today\", \"Friday\", \"this week\") "
    "refer to its publish date, not to the as-of date. Give more weight to "
    "newer headlines; an event from several days ago may already be priced "
    "in. If only a few headlines carry real news about this company, lower "
    "your confidence accordingly rather than reading a trend into them."
)


def _parse_published(value) -> datetime | None:
    if not value:
        return None
    try:
        published = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            published = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published


def _is_listing_page(result: dict) -> bool:
    return bool(
        _LISTING_TITLE.search(result.get("title") or "")
        or _LISTING_URL.search(result.get("url") or "")
    )


def _filter_results(results: list[dict], now: datetime) -> tuple[list[dict], dict]:
    """Deterministic news filter, run before any LLM sees the headlines.

    Returns (headlines, dropped): headlines are the kept results, newest
    first, as {"title", "source", "published", "age_days"}; dropped counts
    each rejection reason so the audit log shows what was filtered out.
    """
    dropped = {"undated": 0, "stale": 0, "listing_page": 0, "duplicate": 0}
    kept: list[tuple[datetime, dict]] = []
    seen_titles: set[str] = set()

    for result in results:
        title = (result.get("title") or "").strip()
        if not title:
            continue
        published = _parse_published(result.get("published_date"))
        if published is None:
            dropped["undated"] += 1
            continue
        age_days = (now - published).total_seconds() / 86400
        if age_days > MAX_HEADLINE_AGE_DAYS:
            dropped["stale"] += 1
            continue
        if _is_listing_page(result):
            dropped["listing_page"] += 1
            continue
        key = re.sub(r"\W+", " ", title).strip().lower()
        if key in seen_titles:
            dropped["duplicate"] += 1
            continue
        seen_titles.add(key)
        kept.append((
            published,
            {
                "title": title,
                "source": urlparse(result.get("url") or "").netloc.removeprefix("www."),
                "published": published.date().isoformat(),
                "age_days": max(0, int(age_days)),
            },
        ))

    kept.sort(key=lambda pair: pair[0], reverse=True)
    return [headline for _, headline in kept[:MAX_HEADLINES]], dropped


def _build_user_message(ticker: str, headlines: list[dict], now: datetime) -> str:
    lines = [
        f"- [{h['published']}, {h['age_days']}d ago, {h['source'] or 'unknown source'}] {h['title']}"
        for h in headlines
    ]
    return (
        f"Ticker: {ticker}\nAs of: {now.date().isoformat()}\n\n"
        "Headlines (newest first, with publish dates):\n" + "\n".join(lines)
    )


def _fetch_headlines(ticker: str, now: datetime) -> tuple[list[dict], dict]:
    client = TavilyClient(api_key=settings.TAVILY_API_KEY)
    response = client.search(
        query=f"{ticker} stock news",
        topic="news",
        days=MAX_HEADLINE_AGE_DAYS,
        max_results=SEARCH_RESULTS,
    )
    return _filter_results(response.get("results", []), now)


def get_news_sentiment(ticker: str) -> dict:
    """Fetch recent headlines for `ticker` and have the LLM classify sentiment.

    Returns a dict matching SentimentResult's schema, plus "headlines" (what
    the model was shown, with dates) and "headlines_dropped" (what the filter
    removed) for the audit log. If no headlines survive filtering, returns a
    neutral, zero-confidence result rather than guessing.
    """
    ticker = ticker.strip().upper()
    now = datetime.now(timezone.utc)
    headlines, dropped = _fetch_headlines(ticker, now)
    audit = {"headlines": headlines, "headlines_dropped": dropped}

    if not headlines:
        return {
            **SentimentResult(
                sentiment="neutral",
                confidence=0.0,
                key_headlines=[],
                reasoning=(
                    f"No recent news headlines found for {ticker} after filtering "
                    f"(dropped: {dropped})."
                ),
            ).model_dump(),
            **audit,
        }

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    completion = client.chat.completions.parse(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(ticker, headlines, now)},
        ],
        response_format=SentimentResult,
    )

    result = completion.choices[0].message.parsed
    return {**result.model_dump(), **audit}
