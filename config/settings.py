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
    watchlist: list[str] = field(
        default_factory=lambda: ["AAPL", "MSFT", "SPY", "GOOGL", "JPM", "JNJ", "XOM", "AMZN"]
    )


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


auto_execute: bool = _bool_env("AUTO_EXECUTE", False)
kill_switch: bool = _bool_env("KILL_SWITCH", False)
