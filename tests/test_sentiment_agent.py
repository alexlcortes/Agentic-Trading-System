from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agents import sentiment_agent
from agents.sentiment_agent import (
    MAX_HEADLINES,
    SentimentResult,
    _build_user_message,
    _confidence_cap,
    _evidence_score,
    _filter_results,
    get_news_sentiment,
)

NOW = datetime(2026, 9, 24, 20, 30, tzinfo=timezone.utc)


def _result(title, url="https://example.com/news/a", published="Wed, 23 Sep 2026 16:00:00 GMT"):
    return {"title": title, "url": url, "published_date": published}


def test_quote_and_option_pages_are_dropped():
    # Real 2026-09-24 SPY results: 7 of 8 were listing pages.
    results = [
        _result(
            "SPDR S&P 500 ETF (SPY) Stock Price | Quotes & News",
            url="https://www.moomoo.com/stock/SPY-US?chain_id=x",
        ),
        _result(
            "SPY Sep 2026 736.000 put (SPY260929P00736000) Stock Price, News, Quote & History",
            url="https://ca.finance.yahoo.com/quote/SPY260929P00736000",
        ),
        _result("V Stock Quote Price and Forecast | CNN", url="https://www.cnn.com/markets/stocks/V"),
        _result(
            "SPY is down 0.6% today, on INTC stock price movement",
            url="https://www.quiverquant.com/news/SPY+is+down",
        ),
    ]
    headlines, dropped = _filter_results(results, NOW)
    assert [h["title"] for h in headlines] == ["SPY is down 0.6% today, on INTC stock price movement"]
    assert dropped["listing_page"] == 3


def test_ordinary_price_move_headlines_are_kept():
    results = [
        _result("Visa (V) Stock Moves -2.14%: What You Should Know"),
        _result("Apple stock price jumps after earnings beat"),
    ]
    headlines, _ = _filter_results(results, NOW)
    assert len(headlines) == 2


def test_stale_and_undated_results_are_dropped():
    results = [
        _result("Old news", published="Mon, 14 Sep 2026 12:00:00 GMT"),
        _result("No date", published=None),
        _result("Garbage date", published="not a date"),
        _result("Fresh news"),
    ]
    headlines, dropped = _filter_results(results, NOW)
    assert [h["title"] for h in headlines] == ["Fresh news"]
    assert dropped == {"undated": 2, "stale": 1, "listing_page": 0, "duplicate": 0}


def test_duplicates_are_dropped_ignoring_case_and_punctuation():
    results = [_result("Apple beats estimates!"), _result("apple beats estimates")]
    headlines, dropped = _filter_results(results, NOW)
    assert len(headlines) == 1
    assert dropped["duplicate"] == 1


def test_kept_headlines_are_newest_first_with_age_and_source():
    results = [
        _result(
            "Nintendo Co. Ltd. ADR falls Friday, underperforms market",
            url="https://www.marketwatch.com/data-news/nintendo",
            published="Sat, 19 Sep 2026 13:00:00 GMT",
        ),
        _result("Newer story", published="Thu, 24 Sep 2026 15:00:00 GMT"),
    ]
    headlines, _ = _filter_results(results, NOW)
    assert headlines[0]["title"] == "Newer story"
    assert headlines[1] == {
        "title": "Nintendo Co. Ltd. ADR falls Friday, underperforms market",
        "source": "marketwatch.com",
        "published": "2026-09-19",
        "age_days": 5,
    }


def test_iso_dates_are_accepted():
    headlines, _ = _filter_results([_result("ISO dated", published="2026-09-24T10:00:00")], NOW)
    assert headlines[0]["published"] == "2026-09-24"


def test_kept_headlines_are_capped():
    results = [_result(f"Story {i}") for i in range(MAX_HEADLINES + 5)]
    headlines, _ = _filter_results(results, NOW)
    assert len(headlines) == MAX_HEADLINES


def test_user_message_carries_as_of_date_and_per_headline_dates():
    headlines = [
        {"title": "Nintendo ADR falls Friday", "source": "marketwatch.com",
         "published": "2026-09-19", "age_days": 5},
    ]
    message = _build_user_message("NTDOY", headlines, NOW)
    assert "As of: 2026-09-24" in message
    assert "- [2026-09-19, 5d ago, marketwatch.com] Nintendo ADR falls Friday" in message
    assert "Recent headlines" not in message


def _headline(age_days):
    return {"title": f"story {age_days}", "source": "x.com", "published": "2026-09-24", "age_days": age_days}


def test_older_headlines_count_as_half_evidence():
    assert _evidence_score([_headline(0), _headline(3), _headline(4), _headline(6)]) == 3.0


@pytest.mark.parametrize(
    "score, cap",
    [(0.0, 0.0), (0.5, 0.15), (1.0, 0.3), (1.5, 0.3), (2.0, 0.45), (3.0, 0.6), (4.5, 0.6), (5.0, 1.0)],
)
def test_confidence_cap_table(score, cap):
    assert _confidence_cap(score) == cap


def _fake_llm(monkeypatch, confidence):
    parsed = SentimentResult(
        sentiment="bearish", confidence=confidence, key_headlines=[], reasoning="r"
    )
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=lambda **kwargs: completion))
    )
    monkeypatch.setattr(sentiment_agent, "OpenAI", lambda **kwargs: client)


def test_model_confidence_is_capped_by_evidence_and_raw_is_logged(monkeypatch):
    monkeypatch.setattr(sentiment_agent, "_fetch_headlines", lambda t, now: ([_headline(1)], {}))
    _fake_llm(monkeypatch, 0.56)
    result = get_news_sentiment("SPY")
    assert result["confidence"] == 0.3
    assert result["confidence_raw"] == 0.56
    assert result["evidence_score"] == 1.0
    assert result["confidence_cap"] == 0.3


def test_confidence_under_the_cap_is_untouched(monkeypatch):
    headlines = [_headline(0) for _ in range(5)]
    monkeypatch.setattr(sentiment_agent, "_fetch_headlines", lambda t, now: (headlines, {}))
    _fake_llm(monkeypatch, 0.8)
    result = get_news_sentiment("AAPL")
    assert result["confidence"] == 0.8
    assert result["confidence_raw"] == 0.8


def test_no_headlines_skips_the_llm(monkeypatch):
    monkeypatch.setattr(sentiment_agent, "_fetch_headlines", lambda t, now: ([], {"listing_page": 8}))
    monkeypatch.setattr(sentiment_agent, "OpenAI", lambda **kwargs: pytest.fail("LLM called"))
    result = get_news_sentiment("V")
    assert result["confidence"] == 0.0
    assert result["confidence_cap"] == 0.0
