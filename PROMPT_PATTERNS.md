# Prompt & Agentic-Design Patterns

Running notes on the recurring design patterns used in this project, captured as they come up during the build. This is a learning artifact, not a spec — see `agentic-trading-system-build-sequence.md` for the actual build plan.

---

## 1. Structured output over free-text parsing

**Where:** `agents/sentiment_agent.py` (`SentimentResult`)

Instead of asking the LLM for prose and regex-scraping the response, define a schema (a Pydantic `BaseModel`) and pass it via `response_format=` (OpenAI) so the API guarantees the shape of what comes back.

**Why it matters:** free-text parsing is where agentic systems silently break in production — a model rephrases "bullish" as "positive" or "leaning up" one day, and downstream code that was doing string matching just breaks or, worse, silently misreads it. A typed schema turns that failure mode into a hard error at the API boundary instead of a subtle bug three agents downstream.

---

## 2. LLM does interpretation, not retrieval or arithmetic

**Where:** `agents/sentiment_agent.py` (Tavily fetches headlines, not the LLM), and will recur in `agents/technical_agent.py` (RSI/MACD computed in pandas, LLM only interprets the numbers)

Anything checkable or computable by deterministic code stays in code. The LLM is only invoked for the step that's genuinely a judgment call — given these facts, what's the read?

**Why it matters:** LLMs are unreliable at exact arithmetic and can hallucinate data if asked to "find" or "recall" facts instead of being handed them. Keeping retrieval/computation in code means the *only* thing that can be wrong is the interpretation — which is also the only thing you actually want the LLM's judgment on.

---

## 3. Refuse to let the LLM guess on missing data

**Where:** `agents/fundamentals_agent.py` (`MIN_POPULATED_METRICS` gate)

Before calling the LLM, check whether the underlying data is actually good enough to reason about. If fewer than 4 of 9 fundamentals fields came back populated, skip the LLM call entirely and return a plain "insufficient data" result — don't send a mostly-empty payload and hope the model says "I don't know" instead of confabulating a plausible-sounding but made-up answer.

**Why it matters:** LLMs are cooperative by default — handed a sparse or partial prompt, they'll often produce a confident-sounding answer anyway rather than refusing. The fix isn't a better prompt ("only answer if you're sure"); it's a code-level gate that never gives the model the chance to fill a gap with a guess. This is the same principle as pattern #2 taken one step further: not just "the LLM doesn't fetch or compute," but "the LLM doesn't get called at all when there's nothing real to interpret."

---

## 4. Take the highest-stakes decision away from the model entirely

**Where:** `agents/risk_manager.py` (`check_trade`)

Every other agent in this system has an LLM somewhere in it. This one doesn't, on purpose — position sizing, the daily-loss halt, and the kill switch are all fixed Python `if` statements over numbers, with zero model calls anywhere in the file.

**Why it matters:** patterns #1–3 are all about making an LLM's judgment *safer to rely on* — better schemas, narrower scope, refusing to guess. This pattern is different: for the one decision where being wrong is genuinely expensive (how much capital is at risk, whether trading halts), don't rely on judgment at all, model or human-written-prompt or otherwise. An LLM can be prompt-injected via a poisoned headline, have an off day, or just be statistically wrong sometimes — a risk limit that lives in a system prompt can't guarantee it holds. A `RiskLimits` dataclass and a pure function can. The rule of thumb: as a decision's cost-of-being-wrong rises, push it further away from the model and closer to fixed code — this is the far end of that spectrum.

A design tradeoff worth naming from this file specifically: the kill switch and daily-loss halt block *all* actions, including sells — even though you could reasonably argue a sell should stay allowed during a halt since it reduces risk rather than adding it. That's a real decision with an alternative, not an obvious "correct" answer, which is exactly the kind of judgment call that's worth making consciously rather than letting a prompt make it implicitly.

---

## 5. Re-check the decision that actually happened, not the one you expected

**Where:** `orchestration/graph.py` (`risk_precheck` vs. `risk_final_check`)

Phase 4's portfolio manager is handed a risk ceiling computed *before* it decides anything — the risk manager doesn't yet know what the LLM will pick, so it checks a "maximal candidate" trade (e.g. "what if this goes to a full-size buy?") just to give the LLM an informed number to stay under. But `check_trade`'s math for a buy and a sell are completely different (position-limit room vs. capped-at-what-you-hold), so if the LLM ends up proposing a different action than what was pre-checked, that ceiling means nothing for the actual decision.

The fix is to run the deterministic check twice: once *before* the LLM call (to inform its prompt), and once *after* (to gate execution) — against whatever the LLM actually decided, not the placeholder used to build its prompt. The second check is the only one that's authoritative; if it disapproves, the decision is overridden to `hold` in code, and if it merely resizes, the decision's `size_pct` is clamped to match.

**Why it matters:** this generalizes pattern #4 (keep the highest-stakes decision out of the model's hands) to a subtler failure mode — it's not enough to have a deterministic gate *somewhere* in the pipeline; the gate has to run against the decision that will actually be executed, not a stand-in for it. A safety check that validates the wrong object gives you the appearance of a guarantee without the substance of one.

---

## 6. One setting, not two that can drift apart

**Where:** `execution/alpaca_executor.py` (`_get_client`)

Alpaca's SDK wants a `paper: bool` flag and, separately, a base URL. It would be easy to add a `PAPER_TRADING=true` variable to `.env` alongside `ALPACA_BASE_URL` and pass both through independently — but then there are two settings that both have to agree, and nothing stops someone from setting `ALPACA_BASE_URL` to the live endpoint while `PAPER_TRADING` still says `true`. Instead, `paper` is *derived* from `ALPACA_BASE_URL` itself (`"paper" in ALPACA_BASE_URL.lower()`) — there's only one fact in `.env` to get right, and everything else follows from it.

**Why it matters:** this is the same root idea as the risk manager's kill switch being read live rather than cached — a safety-relevant setting should have exactly one source of truth. Two settings that are supposed to always agree will eventually disagree, usually at the worst possible time (here: routing a live order through code someone believed was still paper-only).

---

## 7. Log the reasoning for every run, not just the trades

**Where:** `logs/audit_logger.py` (`log_decision`), called from every path through `orchestration/graph.py`'s `log_and_end_node`

It would be tempting to only log when a trade actually executes — that's the "interesting" outcome, after all. Instead, every single graph run appends an entry: a hold because technicals were mixed, a rejection because the kill switch was on, a human declining at the gate. Same schema every time — full agent reasoning, not just the final action.

**Why it matters:** the question you'll actually ask, staring at a trade that looks wrong six months from now, is usually "why *didn't* it also do X" as often as "why did it do Y" — and that question is unanswerable if the do-nothing runs were never recorded. This is also a debugging tool, not just a compliance artifact: if the technical agent's signal quietly degrades over time (a data source going stale, a model update subtly changing its calibration), the only way you'd ever notice is by being able to read back *every* run's reasoning, including the boring ones.

---

## 8. Some agents can't be backtested honestly — say so, don't fake it

**Where:** `backtest/runner.py` (`NEUTRAL_SENTIMENT_STUB`)

`agents/sentiment_agent.py` and `agents/fundamentals_agent.py` both depend on "what's true right now" data sources — Tavily news search, yfinance's `.info` — with no way to ask either for a historical point-in-time answer. Calling them for a simulated day in March 2025 would actually fetch September 2026's headlines, silently leaking future information into a "past" decision (lookahead bias). The backtest replaces sentiment with an explicit stub carrying `confidence: 0.0` (not a fabricated `0.5` "neutral" reading) and reasoning that states plainly why it's stubbed, and omits fundamentals entirely.

**Why it matters:** the tempting failure mode here isn't a crash, it's a backtest that runs cleanly and reports a plausible-looking number — total return, a win rate, a drawdown — while silently having validated something other than what you think it validated. A stubbed input that's clearly labeled as stubbed keeps the backtest's scope honest: this backtest tells you whether the technical-signal + risk-manager side of the strategy works, and explicitly does not tell you whether sentiment adds value. Pretending otherwise would be worse than not backtesting at all, because a wrong number with false confidence is more dangerous than an acknowledged gap.

---

## 9. A safeguards checklist has to prove each item fires, not just that the code exists

**Where:** Phase 10's audit of `agents/risk_manager.py` and `backtest/runner.py`

Going through the brief's pre-live-capital checklist item by item surfaced two real, latent gaps that had survived every prior phase's testing:

1. The kill switch (`settings.kill_switch`) was read once from an env var at process start. It genuinely does halt trading — but only starting with the *next* process launch, not inside a process already mid-run. Nothing prior had ever tested "flip it while something is running," because nothing had a reason to — Phase 3's tests all set the flag *before* calling `check_trade`, never *during*.
2. `backtest/runner.py` had hardcoded `daily_realized_pnl: 0.0` since Phase 8, meaning the daily-loss circuit breaker had never once fired through an actual backtest run, despite every prior report claiming the backtest "ran cleanly." It ran cleanly because nothing ever gave it a reason not to — a different thing entirely from having been tested.

**Why it matters:** both gaps are invisible from reading the code in isolation — `check_trade`'s kill-switch check looks correct, and it is correct, for the case anyone had actually exercised. A checklist that just asks "does this exist in the code" would have checked both boxes. The version that catches real gaps asks "show me it actually firing, right now, through the real path" — which is exactly why this phase added a live file-based kill switch (checked fresh every call, not cached, so it works mid-run) and made the backtest track real day-over-day P&L instead of a hardcoded zero, then proved the halt fires by deliberately forcing it with an artificially tight threshold against real data.

---

## 10. Giving the model more context can add bias, so say what the context is for

**Where:** `agents/portfolio_manager.py` (`review_position`, `REVIEW_SYSTEM_PROMPT`), wired in `orchestration/graph.py` (`_position_review`)

A held position whose technical signal was "hold" used to end in a forced hold with no model call at all. No agent saw the position — entry price, P&L, days held — so "hold" meant "the chart says do nothing today," identical to not owning it, and nothing ever asked whether the position still deserved its place. The review gives the portfolio manager that position data and a hold-or-sell choice.

The same gap showed up in a second case: a held position whose technical signal is "buy" but which is already at `max_position_pct`. The portfolio manager is only asked whether to add past the cap (the human-override path), never whether to keep what it holds, so the review runs there too. The first version of the system prompt said "the technical signal says hold" — true when it was the only trigger, and false the moment a second one existed, with the model looking at `signal: buy` a few lines below. A system prompt states what is fixed about the task; what varies per call belongs in the user message. So the system prompt now says only "adding is not an option," and a "Why this review:" line in the user message names the trigger. For an at-cap buy, it spells out that the buy signal is not a case for adding, since a bullish signal next to "should you keep this?" reads naturally as "yes, and buy more."

The obvious prompt ("here is the position, down 4%, should we sell?") invites the two biases people show with exactly this data: the *disposition effect* (holding a loser "until it comes back," selling a winner early "to lock it in") and *anchoring* on the entry price as if it were a meaningful level rather than a historical accident. The model learned from text written by people with those biases, so the prompt names both bad justifications as invalid on their own.

The first version went further and gave the model the logged reasoning from the run that bought the position, with two rules: "decide as if you were choosing whether to buy it today," and "if the original reason no longer holds, sell; if it still holds, hold." That traded one anchor for another. The second rule was the concrete one, so the model followed it, and in the first live run every review defended the entry: "nothing clearly invalidates the buy case," "weaker but not broken." Rerunning that day's five reviews three times each, 15 of 15 answers rested on the original thesis, and all 15 held. The first rule was no escape either. Every reviewed position has a technical *hold*, and the system never buys on a hold, so taken literally "would you buy it today?" means selling everything. The model had good reason to fall back on the thesis.

The fix was cumulative, and each step was measured on the same inputs:

1. **One rule instead of two:** do today's signals lean toward owning the stock or away from it? A genuinely balanced read goes to hold, and the prompt says why: exiting and re-entering costs something. Thesis language mostly disappeared, but every answer still held, now on "not clearly adverse enough to exit." The burden of proof still sat on selling, and a bearish moving-average crossover kept getting labelled "balanced."
2. **Argue both sides before deciding.** The schema now makes the model write `case_for_owning` and `case_against_owning` *before* `action`. Fields are generated in order, so the case against has been written out in full before the decision. The prompt adds that "neither side needs to be decisive" and that "a mix of weak points on both sides is not automatically equal." This is where the action finally responded to the evidence.
3. **Drop the entry reasoning.** With it included, the review sometimes argued an "intact longer-term setup" and held. Without it, answers rested on specific indicators. These entries were technically driven, so the "original thesis" was mostly a stale copy of today's technical signal anyway.

The result is not "sell more." Four of the five positions still held every time. The difference is XOM, a real borderline case (price below its 20-day SMA with a bearish MACD, inside a still-bullish 20/50-day structure). Over 10 samples the review split it about evenly between sell and hold, where before it held 3 of 3 by defending the entry.

Four structural choices back the prompt up rather than trusting it:

- **Both cases come before the decision** (`PositionReview` field order), and both are logged on the decision, so each review shows what it weighed.
- **The schema can only say hold or sell** (`PositionReview`). A review can exit a position against a "hold" signal but can never *add* against one — risk reduction is allowed to override the primary signal, risk addition isn't.
- **The model makes the call; code sets the size.** Exits are all-or-nothing, sized to the whole position by the risk manager, so there's no `size_pct` for the model to get wrong (pattern #2).
- **Price-based exits stay out of its hands.** The prompt tells it stop-losses are handled by fixed rules (`agents/exit_rules.py`), so it judges today's evidence and nothing else (pattern #4).

**Why it matters:** "give the model more context" is usually good advice, but context isn't neutral — P&L data carries a well-documented pull toward bad decisions, and a model will follow that pull unless the prompt says what the data is *for*. And like the exit rules, the review ships in shadow mode (`POSITION_REVIEW_MODE=shadow`) so the reframed prompt can be checked against real positions — does it actually avoid "waiting for breakeven"? — before its answer ever places an order. It did not avoid it at first: the fix for the disposition effect brought in an anchor of its own, and only rerunning real inputs showed that. Keyword counts ("thesis," "intact") were a useful first pass, but reading the answers was what told a thesis defense apart from "the 50-day trend is intact," which is evidence. The borderline split can't be prompted away — on a genuinely close call, a well-calibrated model *should* be unsure — so it's handled in code instead: a review sell only counts once the previous daily run proposed it too (`orchestration/confirmation.py`). A 50/50 answer repeats on the next day's data about a quarter of the time by chance; a position whose evidence has really turned repeats far more often. Shadow logs record each sell as a first day or a repeat, so that difference can be checked rather than assumed.

---

## 11. When the inputs were selected, tell the model how

**Where:** `agents/portfolio_manager.py` (`propose_rotation`, `ROTATION_SYSTEM_PROMPT`, `_rotation_schema`), wired in `orchestration/rotation.py`

Once the portfolio holds `max_open_positions`, a buy on any other ticker is rejected before the model sees it, so nothing ever asks whether the new idea beats something already held. Rotation asks that question after the watchlist loop, when every blocked candidate and every holding has today's signals: swap one holding for one candidate, or make no swap.

The comparison is lopsided before the model reads a word. Every candidate is on the list *because* it has a buy signal; holdings appear with whatever signal they have today, usually "hold." Laid side by side, "buy" next to "hold" reads as "the candidate is better," but the labels only restate how the list was built. That's selection bias, and the model can't see it unless told. So the prompt says it outright — "every candidate has a buy signal because that is how candidates are chosen … the label alone is not evidence" — and points the model at what *is* comparable: the confidence and reasoning behind each signal.

The same asymmetry argues for a default. A swap is two trades made on one day's signal, while "no swap" costs nothing today, so the prompt makes no-swap the default and asks for a candidate that is "clearly stronger, not merely comparable." Holdings get the same "would you buy it today?" framing and the same P&L warning as the position review (pattern #10). They are shown without the reasoning from the day they were bought, which anchored the position review on the original thesis (pattern #10).

As in the review, structure backs the prompt up:

- **The schema only accepts real tickers.** It is built per call, with the candidates as the only buy options and the holdings as the only sell options, so a swap naming a stock that isn't in play can't be returned. It uses enums, not `Literal`s: a single-ticker `Literal` becomes a JSON-schema `const`, while an enum stays an `enum` for any count.
- **The model picks the pair; code does the rest.** The sell is the whole position, and both legs go through `check_trade`, the buy against the portfolio as it would be after the sell. A swap the risk manager would block is logged as such.

**Why it matters:** a prompt can be neutral in every word and still stack the deck, because the *data* arrives pre-sorted. Whenever code filters what the model sees — only the blocked buys, only the flagged trades, only the top results — the filter is part of the evidence, and the model reads it as a signal unless told what it is. A first check against real holdings showed the risk: on a borderline input (a 0.78 buy candidate against a 0.83 hold), five identical calls split 2 swaps to 3 no-swaps. That's why it ships as shadow only (`ROTATION_MODE=shadow`, with no live setting at all), and why a swap only counts once the previous run proposed the same pair (the same two-in-a-row rule as the review, pattern #10), until the logs show how often it proposes swaps and whether the ones it repeated would have helped.

---

*(More patterns will be added here as later phases — going live — surface new ones worth naming.)*
