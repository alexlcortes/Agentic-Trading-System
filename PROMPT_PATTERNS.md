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

*(More patterns will be added here as later phases — going live — surface new ones worth naming.)*
