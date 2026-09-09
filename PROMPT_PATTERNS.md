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

*(More patterns will be added here as later phases — the portfolio manager's re-validation of LLM output, the human-approval gate — surface new ones worth naming.)*
