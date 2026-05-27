"""All env loading and named constants in one place. Every magic number lives here."""
from __future__ import annotations

import os
from datetime import time

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Required env var {name} is not set")
    return val or ""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- Credentials (loaded lazily so tests can run without them) ---
ALPACA_API_KEY = _env("ALPACA_API_KEY")
ALPACA_SECRET_KEY = _env("ALPACA_SECRET_KEY")
ALPACA_PAPER = _env_bool("ALPACA_PAPER", True)
SUPABASE_URL = _env("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = _env("SUPABASE_SERVICE_ROLE_KEY")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")

# --- Operational toggles ---
DRY_RUN = _env_bool("DRY_RUN", True)
DAILY_TRADE_CAP = _env_int("DAILY_TRADE_CAP", 10)   # sanity throttle; account is above PDT threshold

# --- Universe ---
TICKERS = ("SPY", "QQQ")

# --- Hours (ET) ---
ENTRY_WINDOW_START = time(10, 0)
ENTRY_WINDOW_END = time(15, 0)
EOD_FLATTEN_AFTER = time(15, 45)

# --- Account / risk gates (sized for ~$50K paper account) ---
MIN_EQUITY = 20000.0
MIN_BUYING_POWER_FOR_ENTRY = 2000.0
MAX_CONCURRENT_POSITIONS = 10
DAILY_LOSS_HALT = -2000.0
MAX_PCT_OF_EQUITY_PER_POSITION = 0.10

# --- Spread construction ---
DTE_MIN = 7
DTE_MAX = 14
LONG_DELTA_MIN = 0.40
LONG_DELTA_MAX = 0.55
SHORT_STRIKES_AWAY_MIN = 2
SHORT_STRIKES_AWAY_MAX = 3
DEBIT_MIN = 300.0
DEBIT_MAX = 2000.0
MIN_OPEN_INTEREST = 500
MAX_BID_ASK_PCT_OF_MID = 0.08

# --- Position management ---
PROFIT_TARGET_PCT = 0.30      # close at +30% of debit
STOP_LOSS_PCT = -0.50         # close at -50% of debit
TIME_STOP_DTE = 1             # force-close at 1 DTE

# --- Claude decision step ---
CLAUDE_MODEL = "claude-opus-4-7"
CLAUDE_MAX_TOKENS = 512
# Note: temperature is deprecated on opus-4.7; not sent. Model is deterministic by default.
