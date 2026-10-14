"""Check whether the sentiment agent's confidence means anything, against
what each stock actually did next. Answers three questions, in order:

  1. Direction: do bullish calls beat SPY, and bearish calls trail it?
  2. Shape: are calls backed by more evidence more often right? This is
     what CONFIDENCE_CAPS in agents/sentiment_agent.py assumes.
  3. Added value: does sentiment agreeing with the technical signal make
     the technical call more right?

Usage:  uv run python scripts/calibrate_sentiment.py [--out PATH]

Live entries only (backtests stub sentiment), from SINCE onward, when the
cap and evidence_score started being logged. Forward returns are close to
close from the decision day, minus SPY's over the same days, so a rising
market doesn't make every bullish call look right. Prices are fetched
after the fact, so there's no lookahead.

Statistics are grouped by decision date: 23 tickers on one day share one
market move, so a day counts once, not 23 times.

DECISION RULE — fixed before the data was looked at (2026-10-13), so the
cap table isn't tuned to noise. Only the 1-day horizon decides anything:
it is the only one with no overlapping windows (2 months of 5-day windows
is ~11 independent periods), and news effects fade fastest. 5 and 10 days
are reported, not acted on.
  - Under MIN_DATES decision dates in a group: insufficient, change nothing.
  - Edge: directional calls' 1-day signed excess return has a 95% CI above
    zero. If not, lower every cap to NO_EDGE_MAX_CAP.
  - Shape: high-evidence calls (score >= HIGH_EVIDENCE) beat the rest by a
    95% CI above zero. If not, flatten: one cap for every evidence level.
"""

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRADES_PATH = ROOT / "logs" / "trades.jsonl"
SINCE = date(2026, 9, 25)  # cap and evidence_score logged from here on
HORIZONS = (1, 5, 10)
PRIMARY_HORIZON = 1
MIN_DATES = 40
HIGH_EVIDENCE = 5.0  # the top row of CONFIDENCE_CAPS, cap 1.0
NO_EDGE_MAX_CAP = 0.3
EVIDENCE_BINS = ((0.0, 1.0), (1.0, 3.0), (3.0, 5.0), (5.0, math.inf))
RAW_CONFIDENCE_BINS = ((0.0, 0.5), (0.5, 0.7), (0.7, 1.01))
MARKET_TZ = ZoneInfo("America/New_York")
BENCHMARK = "SPY"


def load_rows(path: Path = TRADES_PATH, since: date = SINCE) -> list[dict]:
    """One row per (ticker, decision date), the day's last run if it ran
    more than once. Decision date is the New York date of the run."""
    rows: dict[tuple[str, date], dict] = {}
    for line in path.read_text().splitlines():
        entry = json.loads(line)
        if "type" in entry or (entry.get("run_id") or "").startswith("backtest"):
            continue
        outputs = entry.get("agent_outputs") or {}
        sentiment = outputs.get("sentiment_signal") or {}
        if sentiment.get("confidence_cap") is None:
            continue  # before the cap existed, or sentiment failed
        day = datetime.fromisoformat(entry["timestamp"]).astimezone(MARKET_TZ).date()
        if day < since:
            continue
        ticker = (entry.get("final_decision") or {}).get("ticker")
        rows[(ticker, day)] = {
            "ticker": ticker,
            "date": day,
            "sentiment": sentiment["sentiment"],
            "confidence": sentiment["confidence"],
            "confidence_raw": sentiment.get("confidence_raw"),
            "evidence_score": sentiment.get("evidence_score") or 0.0,
            "technical": (outputs.get("technical_signal") or {}).get("signal"),
        }
    return sorted(rows.values(), key=lambda r: (r["date"], r["ticker"]))


def fetch_closes(tickers: set[str], start: date) -> dict[str, dict[date, float]]:
    from agents.market_data_agent import get_historical_price_data

    end = date.today() + timedelta(days=1)
    closes = {}
    for ticker in sorted(tickers | {BENCHMARK}):
        df = get_historical_price_data(ticker, start, end)
        closes[ticker] = {ts.date(): float(c) for ts, c in df["Close"].items()}
    return closes


def attach_returns(rows: list[dict], closes: dict[str, dict[date, float]]) -> None:
    """Add excess_{h}: the ticker's return minus SPY's from the decision
    day's close to the close h trading days later, or None if those days
    haven't happened yet or a price is missing."""
    calendar = sorted(closes[BENCHMARK])
    index = {d: i for i, d in enumerate(calendar)}
    for row in rows:
        i = index.get(row["date"])
        for h in HORIZONS:
            row[f"excess_{h}"] = None
            if i is None or i + h >= len(calendar):
                continue
            d0, d1 = calendar[i], calendar[i + h]
            stock = closes.get(row["ticker"], {})
            if d0 not in stock or d1 not in stock:
                continue
            ret = stock[d1] / stock[d0] - 1
            spy = closes[BENCHMARK][d1] / closes[BENCHMARK][d0] - 1
            row[f"excess_{h}"] = ret - spy


def signed(row: dict, h: int, direction_key: str = "sentiment") -> float | None:
    """Excess return in the call's favor: positive means the call was right."""
    excess = row.get(f"excess_{h}")
    if excess is None:
        return None
    direction = row[direction_key]
    if direction in ("bullish", "buy"):
        return excess
    if direction in ("bearish", "sell"):
        return -excess
    return None


def summarize(rows: list[dict], h: int, value=signed) -> dict:
    """Mean and 95% CI of `value`, averaged within each decision date first
    so a day counts once; hit rate over rows."""
    by_date = defaultdict(list)
    for row in rows:
        v = value(row, h)
        if v is not None:
            by_date[row["date"]].append(v)
    values = [v for vs in by_date.values() for v in vs]
    daily = [statistics.fmean(vs) for vs in by_date.values()]
    result = {"rows": len(values), "dates": len(daily), "mean": None, "ci": None, "hit_rate": None}
    if not daily:
        return result
    result["mean"] = statistics.fmean(daily)
    result["hit_rate"] = sum(v > 0 for v in values) / len(values)
    if len(daily) > 1:
        half = 1.96 * statistics.stdev(daily) / math.sqrt(len(daily))
        result["ci"] = (result["mean"] - half, result["mean"] + half)
    return result


def difference(a: dict, b: dict) -> dict:
    """a's mean minus b's, with a 95% CI from their standard errors."""
    if a["ci"] is None or b["ci"] is None:
        return {"mean": None, "ci": None}
    se = math.hypot((a["ci"][1] - a["mean"]) / 1.96, (b["ci"][1] - b["mean"]) / 1.96)
    mean = a["mean"] - b["mean"]
    return {"mean": mean, "ci": (mean - 1.96 * se, mean + 1.96 * se)}


def directional(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["sentiment"] in ("bullish", "bearish")]


def in_bin(value: float | None, low: float, high: float) -> bool:
    return value is not None and low <= value < high


def verdict(rows: list[dict]) -> list[str]:
    """Apply the decision rule in the module docstring to the 1-day horizon."""
    h = PRIMARY_HORIZON
    calls = directional(rows)
    edge = summarize(calls, h)
    if edge["dates"] < MIN_DATES:
        return [
            f"INSUFFICIENT DATA: {edge['dates']} decision dates with a {h}-day outcome, "
            f"need {MIN_DATES}. Change nothing."
        ]

    lines = []
    if edge["ci"][0] > 0:
        lines.append(f"EDGE: directional calls beat SPY over {h} day (CI {_ci(edge['ci'])}).")
    else:
        lines.append(
            f"NO EDGE: directional calls don't reliably beat SPY over {h} day "
            f"(CI {_ci(edge['ci'])}). Lower every cap to at most {NO_EDGE_MAX_CAP}."
        )

    high = summarize([r for r in calls if r["evidence_score"] >= HIGH_EVIDENCE], h)
    rest = summarize([r for r in calls if r["evidence_score"] < HIGH_EVIDENCE], h)
    if min(high["dates"], rest["dates"]) < MIN_DATES:
        lines.append(
            f"SHAPE UNTESTED: high-evidence {high['dates']} dates, the rest {rest['dates']}; "
            f"need {MIN_DATES} each. Keep the cap table's shape."
        )
        return lines
    gap = difference(high, rest)
    if gap["ci"][0] > 0:
        lines.append(f"SHAPE SUPPORTED: high-evidence calls beat the rest (CI {_ci(gap['ci'])}).")
    else:
        lines.append(
            f"SHAPE NOT SUPPORTED: more evidence isn't more right (CI {_ci(gap['ci'])}). "
            "Flatten: one cap for every evidence level."
        )
    return lines


def _pct(x: float | None) -> str:
    return "     n/a" if x is None else f"{x * 100:+7.2f}%"


def _ci(ci: tuple[float, float] | None) -> str:
    return "n/a" if ci is None else f"{ci[0] * 100:+.2f}% to {ci[1] * 100:+.2f}%"


def _line(label: str, s: dict) -> str:
    hit = "  n/a" if s["hit_rate"] is None else f"{s['hit_rate']:5.0%}"
    return f"  {label:<28} {s['rows']:>5} {s['dates']:>5}  {_pct(s['mean'])}  hit {hit}  CI {_ci(s['ci'])}"


def report(rows: list[dict]) -> str:
    header = f"  {'group':<28} {'rows':>5} {'dates':>5}  {'mean':>8}"
    first, last = rows[0]["date"], rows[-1]["date"]
    out = [
        f"Sentiment calibration, {date.today()}: {len(rows)} live calls, {first} to {last}",
        "Mean = excess return vs SPY in the call's favor (positive = the call was right),",
        "averaged per decision date. Only the 1-day horizon feeds the verdict.",
        "",
        "VERDICT (decision rule fixed 2026-10-13, see the script's docstring)",
        *[f"  {line}" for line in verdict(rows)],
    ]
    for h in HORIZONS:
        calls = directional(rows)
        out += ["", f"=== {h}-day horizon {'(primary)' if h == PRIMARY_HORIZON else '(secondary, windows overlap)'} ===", header]
        out.append("  1. direction")
        out.append(_line("bullish", summarize([r for r in rows if r["sentiment"] == "bullish"], h)))
        out.append(_line("bearish", summarize([r for r in rows if r["sentiment"] == "bearish"], h)))
        out.append(_line("all directional", summarize(calls, h)))
        neutral = summarize(
            [r for r in rows if r["sentiment"] == "neutral"], h, value=lambda r, h: r.get(f"excess_{h}")
        )
        out.append(_line("neutral (raw excess)", neutral))
        out.append("  2. shape: by evidence score")
        for low, high in EVIDENCE_BINS:
            label = f"evidence {low:g}+" if math.isinf(high) else f"evidence {low:g} to <{high:g}"
            out.append(_line(label, summarize([r for r in calls if in_bin(r["evidence_score"], low, high)], h)))
        out.append("     by the model's own confidence, before the cap")
        for low, high in RAW_CONFIDENCE_BINS:
            label = f"raw {low:.1f} to <{min(high, 1.0):.1f}" if high <= 1 else f"raw {low:.1f}+"
            out.append(_line(label, summarize([r for r in calls if in_bin(r["confidence_raw"], low, high)], h)))
        out.append("  3. added value: technical calls, by whether sentiment agrees")
        tech = [r for r in rows if r["technical"] in ("buy", "sell")]
        agrees = {("buy", "bullish"), ("sell", "bearish")}
        disagrees = {("buy", "bearish"), ("sell", "bullish")}
        tech_signed = lambda r, h: signed(r, h, "technical")
        for label, pairs in (("sentiment agrees", agrees), ("sentiment disagrees", disagrees)):
            group = [r for r in tech if (r["technical"], r["sentiment"]) in pairs]
            out.append(_line(label, summarize(group, h, value=tech_signed)))
        out.append(_line("sentiment neutral", summarize(
            [r for r in tech if r["sentiment"] == "neutral"], h, value=tech_signed
        )))
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="also write the report here")
    args = parser.parse_args()

    rows = load_rows()
    if not rows:
        sys.exit(f"No live sentiment calls since {SINCE} in {TRADES_PATH}")
    attach_returns(rows, fetch_closes({r["ticker"] for r in rows}, rows[0]["date"] - timedelta(days=7)))
    text = report(rows)
    print(text, end="")
    if args.out:
        args.out.write_text(text)


if __name__ == "__main__":
    main()
