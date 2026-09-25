import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Used only by run_daily.py when ALPACA_BASE_URL is NOT paper — deliberately
# separate from RiskLimits.max_position_pct (which stays whatever was
# paper-validated) so going live never silently inherits paper's sizing.
# Default is 1/5 of the paper-tested 0.05; ramp up only as a real live
# track record accumulates, never all at once.
LIVE_MAX_POSITION_PCT = float(os.getenv("LIVE_MAX_POSITION_PCT", "0.01"))

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")


@dataclass
class RiskLimits:
    max_position_pct: float = 0.05
    max_daily_loss_pct: float = 0.02
    max_open_positions: int = 5
    # Largest size_pct a human is ever even asked to approve via the
    # max_position_pct override. Anything above this is treated as a
    # malformed decision (e.g. the LLM writing 5.0 for "5%", i.e. 500%)
    # and auto-declined without sending a notification.
    max_override_size_pct: float = 0.10
    # Exit rules (agents/exit_rules.py). Placeholders, not tuned — validate
    # with backtest/runner.py before trusting them. At max_position_pct=0.05,
    # an 8% stop risks ~0.4% of equity per position.
    stop_loss_pct: float = 0.08
    trailing_stop_pct: float = 0.10
    trailing_stop_activation_pct: float = 0.05  # trail only once up this much from entry
    max_holding_days: int = 30
    watchlist: list[str] = field(
        default_factory=lambda: [
            "AAPL", "MSFT", "SPY", "GOOGL", "JPM", "JNJ", "XOM", "AMZN",
            "PG", "CAT", "NEE", "V", "UNH", "TTWO", "NTDOY",
        ]
    )


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


auto_execute: bool = _bool_env("AUTO_EXECUTE", False)
kill_switch: bool = _bool_env("KILL_SWITCH", False)

# How run_daily.py uses agents/exit_rules.py. "shadow" evaluates every held
# position each run and logs what WOULD have exited (type "exit_check" in
# trades.jsonl) without trading, so the rules can be judged on real data
# before they're trusted with orders. "off" skips them entirely. There is
# deliberately no "live" value yet — acting on exits needs graph routing
# that doesn't exist.
EXIT_REVIEW_MODE = os.getenv("EXIT_REVIEW_MODE", "shadow").strip().lower()

# Human-in-the-loop override for trades the risk manager blocks solely for
# hitting max_position_pct (see agents.human_override). Off by default so a
# missing/unconfigured webhook never changes existing hold-on-reject
# behavior — only enable once the n8n workflow is actually built and reachable.
ENABLE_HUMAN_OVERRIDE = _bool_env("ENABLE_HUMAN_OVERRIDE", False)
N8N_OVERRIDE_WEBHOOK_URL = os.getenv("N8N_OVERRIDE_WEBHOOK_URL")
N8N_OVERRIDE_SECRET = os.getenv("N8N_OVERRIDE_SECRET")
# How long n8n's Wait node is configured to hold the request open for your
# Discord reply. The HTTP client timeout adds a buffer on top of this so it
# never cuts the connection before n8n's own timeout fires.
OVERRIDE_TIMEOUT_SECONDS = int(os.getenv("OVERRIDE_TIMEOUT_SECONDS", "600"))
OVERRIDE_HTTP_TIMEOUT_BUFFER_SECONDS = 30
