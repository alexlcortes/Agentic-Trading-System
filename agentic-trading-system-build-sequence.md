# Agentic Stock Trading System — Build Sequence

A from-scratch, multi-agent trading system, broken into prompts you run yourself in VS Code (Copilot Chat or any AI pair-programmer works — the prompts are tool-agnostic). Same practice pattern as your RAG course project: you write the code, the prompt tells the assistant what to build and why, and you review/adjust before moving on.

**Where this is headed:** paper trading first, real capital eventually. Every phase below is ordered so the safety-critical pieces (risk limits, audit logging, a kill switch) exist *before* the system can place a single order — not bolted on afterward. Do not skip Phases 8–10 on the way to live capital.

---

## Architecture at a glance

```
Market Data Agent ─┐
                    ├─→ Technical Agent ─┐
News/Sentiment Agent┘                    ├─→ Risk Manager ─→ Portfolio Manager ─→ [Human Gate] ─→ Execution Agent ─→ Audit Log
                                          │        (hard rules,        (LLM synthesis,        (optional manual
                                          │      not LLM-decided)      structured decision)     approval toggle)
                                          └──────────────────────────────────────────────────────────┘
```

- **Orchestration:** LangGraph — an explicit state graph, not a freeform agent chat. That matters here specifically because you want deterministic control over when execution is allowed to fire, and an easy place to insert a human-approval gate later.
- **Broker:** Alpaca — same API for paper and live trading, so Phase 11 (going live) is a config change, not a rewrite.
- **Market data:** `yfinance` to start (free, no key); note where to swap in Alpaca's or Polygon's market data API later for better reliability.
- **Package management:** `uv`, matching your LinkedIn project setup.
- **LLM:** whatever you have access to — OpenAI or Azure OpenAI (the prompts below are provider-agnostic; plug in your endpoint/keys via `.env` like your other course projects).

Suggested repo name: `agentic-trading-system` (lowercase-hyphenated, matching your convention).

---

## Phase 0 — Scaffolding

**Prompt 1:**
> Set up a new Python project called `agentic-trading-system` using `uv`. Create this folder structure: `agents/`, `orchestration/`, `execution/`, `backtest/`, `logs/`, `config/`, `tests/`. Add a `.env.example` with placeholders for `LLM_API_KEY`, `LLM_ENDPOINT` (optional, for Azure), `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL` (paper: `https://paper-api.alpaca.markets`), and `TAVILY_API_KEY` (or another news API key). Add a `.gitignore` that excludes `.env`, `logs/*.jsonl`, and `__pycache__`. Add dependencies: `langgraph`, `langchain`, `alpaca-py`, `yfinance`, `pandas`, `python-dotenv`.

**Prompt 2:**
> Build `config/settings.py`: load `.env` via `python-dotenv`, and define a `RiskLimits` dataclass with hard-coded defaults: `max_position_pct=0.05` (max 5% of account equity per position), `max_daily_loss_pct=0.02` (halt trading for the day if daily loss exceeds 2%), `max_open_positions=5`, `watchlist=["AAPL","MSFT","SPY"]` (placeholder tickers). Also define `auto_execute=False` and `kill_switch=False` as module-level flags read from environment variables so they can be flipped without a code change.

*Why this matters:* the risk limits and kill switch exist in code before a single agent is written. Everything downstream has to respect this file — it's the thing you'll point to later as your "reasonable basis" for how the system behaves.

---

## Phase 1 — Data agents

**Prompt 3:**
> Build `agents/market_data_agent.py`: a function `get_price_data(ticker: str, lookback_days: int = 60) -> pd.DataFrame` using `yfinance` that returns OHLCV data. Add basic error handling for delisted/invalid tickers and rate limiting (retry with backoff).

**Prompt 4:**
> Build `agents/sentiment_agent.py`: a function `get_news_sentiment(ticker: str) -> dict` that pulls recent headlines for the ticker (Tavily search or a news API of your choice), then calls the LLM with a structured prompt asking it to return JSON: `{"sentiment": "bullish"|"bearish"|"neutral", "confidence": 0-1, "key_headlines": [...], "reasoning": "..."}`. Use function calling / structured output if the LLM provider supports it, not free-text parsing.

---

## Phase 2 — Analysis agents

**Prompt 5:**
> Build `agents/technical_agent.py`: given the OHLCV DataFrame from the market data agent, compute RSI(14), 20/50-day SMA crossover, and MACD. Then call the LLM with these computed indicators (not raw price data) and ask it to return structured JSON: `{"signal": "buy"|"sell"|"hold", "confidence": 0-1, "reasoning": "..."}`. Keep indicator math in plain Python/pandas — only hand the LLM the *interpretation* step.

**Prompt 6 (optional, later):**
> Build `agents/fundamentals_agent.py` using `yfinance`'s `.info` for basic metrics (P/E, sector, market cap) and have the LLM flag anything unusual (e.g., P/E far outside sector norms). Treat this as a minor input, not a primary signal — fundamentals data from free sources is often stale or incomplete.

---

## Phase 3 — Risk manager (deterministic, no LLM)

**Prompt 7:**
> Build `agents/risk_manager.py`: a pure-Python function `check_trade(proposed_trade: dict, portfolio_state: dict, limits: RiskLimits) -> dict` that returns `{"approved": bool, "adjusted_size": float, "reasons": [...]}`. It must reject or resize any trade that would: exceed `max_position_pct` of equity, push open positions past `max_open_positions`, or occur while `kill_switch=True` or the day's realized loss already exceeds `max_daily_loss_pct`. This function must contain **no LLM calls** — it's the one place in the system where the rules are fixed code, not a model's judgment call.

*This is the piece worth taking your time on.* Everything else in the system can be wrong sometimes; this function is what keeps "wrong" from becoming "expensive."

---

## Phase 4 — Portfolio manager (decision synthesis)

**Prompt 8:**
> Build `agents/portfolio_manager.py`: a function that takes the technical agent's signal, sentiment agent's signal, (optionally) fundamentals, and the risk manager's constraints, then calls the LLM to synthesize a final decision as structured JSON: `{"ticker": str, "action": "buy"|"sell"|"hold", "size_pct": float, "confidence": float, "reasoning": str}`. Explicitly tell the LLM in the prompt that `size_pct` must never exceed what the risk manager allows — pass the risk manager's `adjusted_size` as a hard ceiling in the prompt, and re-validate the LLM's output in code afterward (never trust the LLM to have obeyed the constraint on its own).

---

## Phase 5 — Orchestration

**Prompt 9:**
> Build `orchestration/graph.py` using LangGraph: a `StateGraph` with nodes for `market_data`, `sentiment`, `technical`, `risk_manager`, `portfolio_manager`, a `human_gate` node, and `execution`. Data and sentiment run in parallel, both feed into `technical`/synthesis, then `risk_manager`, then `portfolio_manager`. The `human_gate` node should check `settings.auto_execute` — if `False`, print the proposed trade and reasoning, then wait for CLI confirmation (`y`/`n`) before allowing the graph to proceed to `execution`. If the risk manager's decision was `approved: False`, route straight to logging and skip execution entirely.

---

## Phase 6 — Execution (paper trading)

**Prompt 10:**
> Build `execution/alpaca_executor.py` using `alpaca-py`, pointed at the paper trading endpoint from `.env`. Implement `submit_order(ticker, side, qty)` that submits a market order, polls for fill status (handle partial fills and rejections explicitly), and returns a structured result. Never let this module read live-trading credentials — it should only ever see whatever `ALPACA_BASE_URL` is set to in `.env`, so switching paper/live is purely a config change, never a code change.

---

## Phase 7 — Audit logging

**Prompt 11:**
> Build `logs/audit_logger.py`: a function `log_decision(run_id, timestamp, agent_outputs: dict, final_decision: dict, execution_result: dict | None)` that appends one JSON line per run to `logs/trades.jsonl`. Every agent's raw output (technical signal, sentiment, risk manager's reasons, portfolio manager's reasoning) must be captured, not just the final action — this is your record of *why* the system did what it did, for every single run, including runs where it decided to do nothing.

*Why this matters:* regulators expect automated trades to have a "reasonable basis," and practically speaking, you'll want this the first time a trade looks wrong and you need to know whether it was a bad signal, a bug, or a risk-limit edge case.

---

## Phase 8 — Backtesting

**Prompt 12:**
> Build `backtest/runner.py`: replay the full agent pipeline day-by-day over historical data (pick a 6–12 month window), tracking hypothetical portfolio value, and compare against a simple buy-and-hold baseline on the same tickers. Log every simulated decision through the same `audit_logger`. Report total return, max drawdown, win rate, and number of risk-manager rejections.

**Checkpoint:** don't move to Phase 9 until the backtest runs cleanly across at least one bull period and one flat/down period. A strategy that only "works" in one type of market isn't validated yet.

---

## Phase 9 — Extended paper-trading validation

**Prompt 13:**
> Build `run_daily.py`: a script (runnable via cron or a scheduled task) that executes the full graph once per trading day against the paper account, with `auto_execute=True` (paper money only) so you get an unattended track record. Add a simple daily summary notification (email, Slack webhook, or just a log file you check) reporting that day's decision and outcome.

**Checkpoint before you even think about live capital** — run this for a minimum of 4–8 weeks and confirm:
- No unhandled crashes or silent failures across that whole window
- The risk manager has never been bypassed (grep the audit log for any executed trade where `approved: False`)
- The reasoning in the logs actually makes sense when you read it back — if it doesn't, the system isn't ready, regardless of P&L
- Performance has been evaluated across more than one kind of market condition, not just a lucky stretch

---

## Phase 10 — Safeguards checklist (gate before live capital)

Don't treat this as optional polish — this is the difference between "a project" and "a thing that can lose real money unattended." Confirm each of these before touching a live key:

- **Kill switch** you can flip from outside the code (an env var or a file the script checks on every run) that halts execution immediately
- **Daily loss circuit breaker** already enforced in Phase 3, tested by deliberately forcing a losing scenario in backtest
- **Position size caps** as a percentage of account equity, not a fixed dollar amount (so they scale correctly if the account size changes)
- **Notification on every order attempt**, not just failures
- **Manual approval mode** (`auto_execute=False`) as the default, requiring you to explicitly re-enable unattended execution
- **You remain legally responsible for what the bot does** — automation doesn't transfer liability. Keep the audit log; it's your paper trail if a trade is ever questioned.

---

## Phase 11 — Going live (when Phase 9 + 10 are genuinely done)

**Prompt 14:**
> Update `.env` to point at Alpaca's live trading endpoint and live API keys (never commit these). Add a startup check in `run_daily.py` that refuses to run in live mode unless `auto_execute=False` (i.e., live trading always starts in manual-approval mode, no exceptions, even if paper mode was fully automated). Reduce `max_position_pct` to a fraction of what you used in paper testing for the first live weeks, and plan to ramp it back up only as the live track record itself accumulates.

Start with capital you could fully lose without it mattering financially. The paper track record tells you the logic works; it doesn't tell you how you'll react the first time a real order fills badly, and that's worth finding out with small stakes.

---

## Notes

- This intentionally builds from scratch rather than forking `ai-hedge-fund` or `TradingAgents` — the tradeoff we talked through is that a fork gets you running faster, but a system you're eventually trusting with real capital is worth having designed and understood end to end yourself.
- Feel free to reorder Phase 6 (fundamentals) or swap in a different news source, LLM provider, or data feed — the structure (deterministic risk manager, audited decisions, paper-trading gate) is the part that shouldn't move.
