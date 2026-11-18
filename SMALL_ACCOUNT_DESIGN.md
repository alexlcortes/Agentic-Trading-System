# Small-Account System: Design and Test Criteria

*Drafted 2026-11-18. Status: **proposal**. The decisions in section 8 need sign-off before any build starts. No code changes before the Nov 22 golden snapshot is built and reviewed.*

## 1. Why

If this system ever trades real money, it will start as a small account (about $1,000). The current paper test runs a $100K, ≤40%-invested setup that will never go live. At $1,000 it would never trade: `shares_for` rounds down to whole shares, so 1% ($10) or 5% ($50) of equity buys 0 shares of 22 of the 23 watchlist tickers.

So we build the small-account version and **restart the paper test on it**, carrying over everything the first test taught us.

**Lesson #1 from test one:** behavior changed partway through (watchlist, slot count, override flow, rotation), which split the data across code versions. In this test, **everything that affects trading is decided before Day 1 and frozen until the verdict.**

## 2. What stays and what changes

| Stays as is | Changes |
|---|---|
| Technical, sentiment, fundamentals and portfolio-manager agents and their prompts (except the 2d wording fix) | Order sizing: whole shares → **dollar amounts (fractional)** |
| Risk manager is pure Python with no LLM, and the kill switch | Position limits: 8 × 5% → **5 × 15%** (proposed) |
| Two-phase decide/commit graph, ranking, confirmation | A **cash check** in the risk manager |
| Shadow machinery (exit rules, position review, rotation) | Sells that close a position sell the **exact held quantity** |
| `logs/trades.jsonl` (keeps growing; the Dec 15 calibration reads it) | New Alpaca **paper account funded with $1,000** |
| Old $100K paper account (left untouched as a record) | New measurement: equity log, SPY benchmark, API cost, eval report |

## 3. Design

### 3.1 Cutover to a new account
- **Create a new Alpaca paper account with a $1,000 starting balance**, with new API keys in `.env`. Don't reset or liquidate the old account: it's the record of test one.
- **New `TEST_ID` setting** (e.g. `small-1k-v1`), stamped on every `trades.jsonl` row, replay file, equity row and summary block. Analysis filters by it, so test one and test two never mix. The calibration script already filters by date and ignores typed rows, so it's unaffected.
- **Archive the per-account state files** to `logs/archive/paper-100k/`, then start them empty: `positions_meta.json`, `pending_orders.json`, `last_proposals.json`, `last_completed_run.json`. They describe positions in the old account. If they carried over, the new run would believe it holds AAPL, PG and so on.
- `trades.jsonl`, `daily_summary.log` and `run_daily.log` keep appending, not archived.
- **Set `ENABLE_HUMAN_OVERRIDE=false` in `.env`** (D6). It's on today. If the flag is left as is, the ntfy override comes back with the new account.

### 3.2 Fractional orders
Alpaca accepts a **notional** order ("buy $150 of MSFT") or a fractional `qty` for assets marked `fractionable`. Fractional orders must be market or limit orders with `TimeInForce.DAY`.

- **Buys** are submitted by notional dollars: `round(size_pct × equity, 2)`. The order spends exactly that amount, so a gap up at the open can't overspend.
- **Full exits** (risk manager result `sell_capped_to_position`, or a requested size ≥ the position) sell the **exact held qty** taken from Alpaca's position, with no conversion from percent to shares. This removes the leftover-share bug class ([MSFT 11/18, SPY 10/23]).
- **Partial sells** go by notional. If what would remain is under the minimum buy, sell everything instead.
- `shares_for` and `REASON_BELOW_ONE_SHARE` are replaced by a **minimum order notional** (`min_order_dollars`, default $1, Alpaca's floor). The `min_buy_fraction_of_cap` rule stays (1.5% of equity = $15 at 15%), so tiny top-ups are still blocked.
- `pending_orders.json` stores `notional` for buys instead of `requested_qty × reference_price`. `get_portfolio_state` counts pending exposure from it.
- **Universe check:** at startup, drop any watchlist ticker whose Alpaca asset isn't `fractionable` and log it once. NTDOY (an OTC ADR) probably drops.
- *Why notional instead of fractional qty:* one number (dollars) flows from the portfolio manager → risk manager → order with no price conversion in between. Every bug in the rounding family so far came from that conversion.

### 3.3 Position limits
| Limit | Test one | Proposed | At $1,000 |
|---|---|---|---|
| `max_open_positions` | 8 | **5** | — |
| `max_position_pct` | 5% | **15%** | $150 |
| Max invested | 40% | **75%** | $750 |
| Minimum buy (10% of cap) | 0.5% | 1.5% | $15 |
| `max_daily_loss_pct` | 2% | 2% | $20 |
| 8% stop risk per position | 0.4% | 1.2% of equity | $12 |

The live override `LIVE_MAX_POSITION_PCT` (1%) was built to shrink a large account's sizing when going live. At $1K it would make the system inert again. **For this design, live uses the same limits as the test.** The small stake itself is the safety margin. `run_daily.py`'s override changes accordingly.

### 3.4 Cash check
New risk-manager rule (pure Python, like the rest): a buy is approved only if
`notional ≤ cash − pending_buy_notional − cash_reserve`, with `cash_reserve` = 1% of equity.
- `portfolio_state` gains `cash` and `pending_buy_notional`. This changes the replay fixture format, so `FIXTURE_VERSION` gets bumped. Test two gets its own golden recording anyway (3.6).
- The check uses **`cash`, never `buying_power`**. Paper accounts are margin accounts, and buying power can be 2× cash. This system never borrows.
- New reason code `insufficient_cash`. It isn't override-eligible.
- Trimming rule: if the cash available is less than the request but above the minimum buy, resize to the cash available (like `position_resized_to_limit`). Otherwise reject.

### 3.5 Feature modes: fixed on Day 1, held for the whole test
**Rule: a feature is live on Day 1 only if it passes a backtest judged by the eval report (2e) before Day 1. Otherwise it runs in shadow for the whole test. Nothing switches modes partway through.**

| Feature | Day-1 mode (proposed) | Condition |
|---|---|---|
| Technical sell (current only live exit) | live | — |
| Fixed stop-loss 8% | **live if the backtest passes**, else shadow | Backtest 2022–23 using intraday lows (plan 2) |
| Trailing stop 10% / activation 5% | shadow | Too few firings so far (3 in ~250 checks) |
| Sell confirmation (2 runs in a row) | **live if the backtest passes**, else off | Plan 1 |
| Broker-side GTC stops | off | Needs the 5 paper checks plus the live stop-loss; a go-live item instead (section 7) |
| Volatility-scaled cap + SPY regime flag | shadow | Retune `vol_target` for a 15% cap; plan 2c |
| Position review | shadow | — |
| Rotation | shadow | 5 slots fill faster, so more data |
| Human override (ntfy) | **off** | The test measures the agents, not the agents plus your judgment |

### 3.6 Measurement (all built before Day 1)
- **Equity log** (plan 2j), plus a **`net_deposits` column**. Deposits are expected if this goes live and you add money over time. Returns are computed time-weighted, so a deposit doesn't look like a gain.
- **SPY benchmark:** each run also logs SPY's close. The report compares against "$1,000 in SPY on Day 1" and against an **exposure-matched** SPY (SPY scaled to the system's actual % invested each day), which separates stock-picking from simply holding cash.
- **API cost per run:** record `response.usage` tokens for every OpenAI call and count Tavily searches. Price constants live in `settings` (you fill them from your billing page). A `COST` line goes in the daily summary. Returns are reported **after** costs.
- **Eval report** (plan 2e) with `--live --test-id`: equity curve, drawdown, the Sharpe CI, and the "inconclusive" verdict rules.
- **Trade journal** (plan 2h). With money at stake, a crash must never double a buy.
- **Replay recording** from Day 1 under the new fixture version. Golden snapshot after 20 recorded days, so any bug fix made during the test is provably limited to its target.

### 3.7 Prompt fix (plan 2d)
`REVIEW_SYSTEM_PROMPT` and `ROTATION_SYSTEM_PROMPT` say stops are "handled separately by fixed rules". Reword them to match the Day-1 modes in 3.5 exactly. This happens before Day 1, because prompt changes alter model answers.

## 4. Build order

Each step ends with the full test suite passing.

0. **Nov 22:** golden snapshot builds. Review the report, commit `tests/golden/`.
1. **Eval report + backtest plumbing** (2e). Stop backtests writing to `trades.jsonl` first.
2. **Choose and freeze the ETF list** (D2). Write the selection rules down first, then apply them:
   - No leveraged or inverse ETFs. They're built for one-day holds and lose value over multi-week holds.
   - Check overlap and correlation with the 23 stocks, the same check as the 9/29 watchlist expansion. A sector ETF like XLK holds AAPL and MSFT.
   - Count the cost: each ticker adds about 3–4 model calls per run (gate P3).
   - Don't pick by recent performance (see section 9).
   The final list goes into `RiskLimits.watchlist` and doesn't change during the test.
3. **Paper checks against the new $1K account**, read-only or with tiny orders:
   - (a) Is a notional DAY market order submitted after the close accepted and queued for the open?
   - (b) Can a fractional qty sell close a whole position?
   - (c) Which Day-1 watchlist tickers, stocks and ETFs, are `fractionable`?
   - (d) What are `cash` vs `buying_power` on a $1K paper account?
   **If (a) fails**, the order flow has to change (e.g. a second job at the open that submits queued decisions). That's a bigger design change, so stop and re-plan.
4. **Fractional orders + full-position sells** (3.2), with `build_golden.py --update`.
5. **Small-account limits + cash check** (3.3, 3.4).
6. **Measurement:** equity log, benchmark, cost logging, `TEST_ID` (3.6).
7. **Trade journal** (2h).
8. **Backtests** for the stop-loss and sell confirmation, at the **new** limits on the **Day-1 watchlist** (the 23 stocks plus the frozen ETF list), judged by the eval report. Their results set the modes in 3.5.
9. **Prompt fix** (3.7).
10. **Host:** make sure the Mac can't sleep through 4:30 PM (`sudo pmset -c sleep 0`, or move to the Pi).
11. **Dry run:** one full run against the $1K account with `AUTO_EXECUTE=false` and no orders. Review the summary together.
12. **Cutover** (3.1) → **Day 1**.

Estimate: about 2–3 weeks of build. Day 1 lands in early-to-mid December. The Dec 15 calibration still runs on schedule, since nothing here changes the sentiment agent.

## 5. Test protocol
- **Length:** 13 weeks from Day 1 (≈63 trading days). Extensions follow section 6, in 4-week blocks, to at most 26 weeks.
- **Change policy during the test:**
  - **Allowed:** fixes for bugs that stop the system from doing what this document says (crashes, failed orders, wrong logging). Each one is logged in `logs/test_changes.md` with date, reason and whether it changed any trade, committed with `build_golden.py --update`.
  - **Not allowed:** tuning limits, thresholds, prompts, the watchlist or feature modes. An idea for any of these goes into a list for the next test.
  - **Allowed at any time:** pausing (kill switch). Paused days count as missed days.
- **Weekly review:** the eval report plus a skim of the summary log. Reviews are for catching bugs, not for tuning.
- **Manual orders:** none. If something looks stuck, it counts as a bug (operational gate O3).

## 6. Pre-committed criteria and verdict

*Like the sentiment calibration rule, these are fixed before any results exist. Don't loosen them after seeing the numbers.*

**A word on what 13 weeks can show.** It can show the machinery works and that the system isn't clearly harmful. It **can't** prove the agents have an edge: the Sharpe CI over ~60 days is roughly ±4. So the bar is "ready, and not losing to the index by much", not "proven to beat it".

### Operational gates (all must pass)
| # | Gate | Threshold |
|---|---|---|
| O1 | Runs completed | ≥ 95% of trading days |
| O2 | Every Alpaca fill traced to a `trades.jsonl` row or a reconciliation | 100% |
| O3 | Leftover or stuck positions; manual orders; hand repairs to state files | 0 |
| O4 | Risk-limit breaches at order time (slots, cap, cash) | 0 |
| O5 | Orders rejected by Alpaca for insufficient funds | 0 |
| O6 | Double-submitted orders | 0 |

### Performance gates
| # | Gate | Threshold |
|---|---|---|
| P1 | Max drawdown (peak-to-low equity) | ≤ 10% |
| P2 | Return **after API costs** vs $1,000 in SPY over the same dates | ≥ SPY − 3 percentage points |
| P3 | Projected annual API cost | ≤ 5% of account value ($50/yr at $1K) |
| P4 | Closed round trips (buy then full sell) | ≥ 30 for the eval report to give a verdict |

The exposure-matched comparison is reported but not a gate.

### Verdict
- **GO**: O1–O6 and P1–P3 pass, and P4 is met (or 26 weeks are reached). This makes live with $1K *eligible*, once section 7 is done. Eligible, not automatic: you still decide.
- **EXTEND**: O1–O6 pass and P1 passes, but P4 isn't met yet. Add 4 weeks with no changes, up to 26 weeks.
- **NO-GO, operational**: any O gate fails. Fix it, log it, and **restart the clock**. The data so far is kept and reported, but doesn't count toward the verdict.
- **NO-GO, performance**: P1 or P2 fails at the end. No live trading. Review what to change, which becomes test three.
- **Cost only**: P3 fails but everything else passes. Cut costs first (fewer tickers, cheaper model for some agents, less frequent runs), then run a short check that behavior didn't change. Going live is blocked until it passes.
- **Hard stop at any time:** drawdown > 15% → pause with the kill switch and review.

## 7. Before any real money (outside the test)
- **Phone approvals** (2i). Live mode forces manual approval, and `input()` can't run under launchd.
- **Broker-side stops** (2b), if the stop-loss went live. A laptop that's off shouldn't mean no protection.
- **Always-on host** (Pi or equivalent).
- **Account type:** decide cash vs margin. Under $2K Alpaca doesn't allow margin anyway. A cash account has T+1 settlement and good-faith-violation rules; a margin account under $25K has the pattern-day-trader rule (broker stops could create same-day round trips). Verify against Alpaca's current rules before opening.
- **Taxes:** every sale is a taxable event, and short-term gains are taxed as ordinary income. Alpaca issues a 1099. Keep the trade journal.
- **Real-money criteria**, written before the switch: e.g. a live drawdown that triggers a pause, and when to add money.

## 8. Decisions needed before the build

| # | Decision | Recommended | Your call |
|---|---|---|---|
| D1 | Slots × cap | 5 × 15% (75% max invested). Alternative: 4 × 20% |  5 × 15% |
| D2 | Watchlist | Keep the current 23, minus non-fractionable tickers. ETFs are an idea for test three |  Add ETFs to the test from Day 1 (which ETFs: to be chosen and frozen before Day 1) |
| D3 | Test length | 13 weeks, extendable to 26 under section 6 |  13 weeks is fine |
| D4 | Gate thresholds | As in section 6 (DD 10%, SPY − 3 pts, cost 5%/yr, 30 round trips) |  Whatever you suggest (so: as recommended) |
| D5 | Run frequency | Daily (the confirmation and rotation rules assume it); revisit only if P3 fails |  Keep it daily |
| D6 | Human override during the test | Off |  Keep it off (the ntfy push notification is not used during the test) |
| D7 | Starting balance | $1,000 |  $1,000 for now |
| D8 | Old paper account | Leave as is (positions stay, daily job switches to the new account) |  Leave as is |

*Consequence of D2:* the watchlist is no longer just the current 23. The ETF list is chosen and frozen in build step 2, and it flows into the universe check (3.2), the backtests in build step 8, and the 3.5 volatility cap.

*How the agents handle ETFs, checked 2026-11-18 against SPY's last 40 live runs (SPY is already an ETF on the watchlist):*
- **Fundamentals:** no change needed. Every run said "Insufficient fundamentals data (3/9 metrics populated) — skipping analysis rather than guessing", and that will apply to ETFs generally.
- **Sentiment:** SPY was always neutral at low confidence (0.10–0.34), because news about an index fund is general market commentary. Expect ETF decisions to rest mostly on the technical agent.

## 9. Risks and unknowns
- **Paper check 3(a):** if Alpaca won't queue a notional order after the close, the order flow has to change.
- **Paper fills are idealized:** no slippage, instant fills at the open. With liquid large caps and $150 orders, real slippage should be negligible, but it's unverified.
- **Hindsight in the watchlist:** the 23 tickers were picked in Sep 2026, so the 2022–23 backtests flatter them. The forward paper test doesn't have this problem, which is one reason it matters more than the backtests. The same goes for the ETFs: they must be chosen by rules written down in advance (build step 2), not by recent performance.
- **Concentration:** 5 positions at 15% means one bad name costs ~3× what it did in test one. The stop-loss decision (3.5) matters more here.
- **Host downtime** counts against O1. Missed days in test one came from the Mac sleeping.
