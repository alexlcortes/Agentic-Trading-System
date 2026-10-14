import json
from datetime import date, timedelta

import pytest

from scripts import calibrate_sentiment as cal


def _entry(ticker, ts, sentiment="bullish", evidence=5.0, cap=1.0, run_id="r", technical="buy"):
    return {
        "run_id": run_id,
        "timestamp": ts,
        "agent_outputs": {
            "technical_signal": {"signal": technical},
            "sentiment_signal": {
                "sentiment": sentiment, "confidence": 0.6, "confidence_raw": 0.7,
                "evidence_score": evidence, "confidence_cap": cap,
            },
        },
        "final_decision": {"ticker": ticker},
    }


def _write(tmp_path, entries):
    path = tmp_path / "trades.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def test_load_keeps_live_capped_calls_one_per_ticker_day(tmp_path):
    path = _write(tmp_path, [
        _entry("AAPL", "2026-09-24T20:30:00+00:00"),                      # before SINCE
        _entry("AAPL", "2026-10-01T20:30:00+00:00", sentiment="bearish"),
        _entry("AAPL", "2026-10-01T21:10:00+00:00", sentiment="bullish"),  # rerun wins
        _entry("AAPL", "2026-10-02T20:30:00+00:00", cap=None),            # no cap logged
        _entry("V", "2026-10-02T20:30:00+00:00", run_id="backtest-V"),
        {"type": "rotation", "timestamp": "2026-10-02T20:30:00+00:00"},
    ])
    rows = cal.load_rows(path)
    assert [(r["ticker"], r["date"], r["sentiment"]) for r in rows] == [("AAPL", date(2026, 10, 1), "bullish")]


def test_decision_date_is_new_york_not_utc(tmp_path):
    # 00:30 UTC on Oct 2 is still Oct 1 in New York.
    rows = cal.load_rows(_write(tmp_path, [_entry("AAPL", "2026-10-02T00:30:00+00:00")]))
    assert rows[0]["date"] == date(2026, 10, 1)


def test_excess_return_is_net_of_spy_and_skips_the_future():
    d0, d1 = date(2026, 10, 1), date(2026, 10, 2)
    closes = {"SPY": {d0: 100.0, d1: 101.0}, "AAPL": {d0: 200.0, d1: 206.0}}
    rows = [{"ticker": "AAPL", "date": d0}]
    cal.attach_returns(rows, closes)
    assert rows[0]["excess_1"] == pytest.approx(0.03 - 0.01)
    assert rows[0]["excess_5"] is None


def test_signed_return_favors_the_call():
    assert cal.signed({"sentiment": "bullish", "excess_1": 0.02}, 1) == 0.02
    assert cal.signed({"sentiment": "bearish", "excess_1": 0.02}, 1) == -0.02
    assert cal.signed({"sentiment": "neutral", "excess_1": 0.02}, 1) is None


def test_a_day_counts_once_however_many_tickers_it_has():
    d0, d1 = date(2026, 10, 1), date(2026, 10, 2)
    rows = [{"date": d0, "sentiment": "bullish", "excess_1": 0.01} for _ in range(20)]
    rows.append({"date": d1, "sentiment": "bullish", "excess_1": -0.03})
    s = cal.summarize(rows, 1)
    assert (s["rows"], s["dates"]) == (21, 2)
    assert s["mean"] == pytest.approx(-0.01)  # (0.01 + -0.03) / 2, not pulled toward d0


def _calls(n_dates, excess, evidence=5.0):
    start = date(2026, 1, 1)
    return [
        {"date": start + timedelta(days=i), "sentiment": "bullish", "evidence_score": evidence,
         "excess_1": excess + (0.001 if i % 2 else -0.001)}
        for i in range(n_dates)
    ]


def test_verdict_changes_nothing_below_min_dates():
    assert cal.verdict(_calls(cal.MIN_DATES - 1, 0.01))[0].startswith("INSUFFICIENT DATA")


def test_verdict_no_edge_lowers_caps():
    lines = cal.verdict(_calls(cal.MIN_DATES, 0.0))
    assert lines[0].startswith("NO EDGE")
    assert lines[1].startswith("SHAPE UNTESTED")


def test_verdict_flattens_when_more_evidence_is_not_more_right():
    rows = _calls(cal.MIN_DATES, 0.01) + _calls(cal.MIN_DATES, 0.01, evidence=2.0)
    edge, shape = cal.verdict(rows)
    assert edge.startswith("EDGE")
    assert shape.startswith("SHAPE NOT SUPPORTED")


def test_verdict_keeps_the_shape_when_evidence_pays():
    rows = _calls(cal.MIN_DATES, 0.02) + _calls(cal.MIN_DATES, 0.001, evidence=2.0)
    assert cal.verdict(rows)[1].startswith("SHAPE SUPPORTED")
