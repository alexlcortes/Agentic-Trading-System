from datetime import datetime, timezone

from agents.sentiment_agent import MAX_HEADLINES, _build_user_message, _filter_results

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
