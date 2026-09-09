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

*(More patterns will be added here as later phases — the deterministic risk manager, the portfolio manager's re-validation of LLM output, the human-approval gate — surface new ones worth naming.)*
