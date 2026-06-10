"""Configuration loader for the Polymarket paper-trade copytrading bot.

Reads settings from a local .env file (see .env.example). Nothing here is secret
except the path to the Google service-account key; the actual trade data lives in
the Google Sheet, not on disk.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise SystemExit(
            f"Missing required config '{name}'. Copy .env.example to .env and fill it in."
        )
    return val if val is not None else ""


# --- Polymarket target ------------------------------------------------------
# Defaults to @RN1; override in .env to copy a different wallet.
TARGET_ADDRESS: str = _get(
    "TARGET_ADDRESS", "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
).lower()
# Display name for this target (used in the dashboard title and logs).
TARGET_NAME: str = _get("TARGET_NAME", "RN1")
SCALE_RATIO: float = float(_get("SCALE_RATIO", "0.01"))
# Polymarket's order minimum is $1 — ignore any trade whose USD value is below this.
MIN_TRADE_USD: float = float(_get("MIN_TRADE_USD", "1.0"))
POLL_INTERVAL_SECONDS: int = int(_get("POLL_INTERVAL_SECONDS", "30"))  # legacy / fallback
# Decoupled cadences: detect new trades often (cheap), re-price positions less often
# (expensive — one CLOB call per market). Detection drives entry timing.
DETECT_INTERVAL_SECONDS: int = int(_get("DETECT_INTERVAL_SECONDS", "7"))
REPRICE_INTERVAL_SECONDS: int = int(_get("REPRICE_INTERVAL_SECONDS", "45"))
# Hard wall-clock cap on the per-cycle price refresh. With a big portfolio, fetching every
# market's price (one CLOB call each) can take minutes and freeze the dashboard, which only
# updates when a reprice finishes. We refresh stalest-first within this budget and reuse the
# last-known price for the rest, so every cycle finishes and the dashboard always updates.
REPRICE_BUDGET_SECONDS: float = float(_get("REPRICE_BUDGET_SECONDS", "20"))
# How often to push per-trade marks onto the (potentially huge) Excel Trades sheet. This
# rewrites every historical row, so it's done sparingly — the live dashboard doesn't use it.
TRADE_MARKS_INTERVAL_SECONDS: int = int(_get("TRADE_MARKS_INTERVAL_SECONDS", "300"))
# How often the data-API backup source reconciles (on-chain is primary, every poll).
DATAAPI_INTERVAL_SECONDS: int = int(_get("DATAAPI_INTERVAL_SECONDS", "60"))
TRADES_PER_POLL: int = int(_get("TRADES_PER_POLL", "200"))

# --- Local Excel datastore --------------------------------------------------
EXCEL_PATH: str = _get(
    "EXCEL_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "polymarket_paper_trades.xlsx"),
)

# --- API hosts (public, no auth) -------------------------------------------
DATA_API: str = "https://data-api.polymarket.com"
CLOB_API: str = "https://clob.polymarket.com"

# --- Polygon RPC (for the on-chain real-time detector) ----------------------
# A free Alchemy/Infura key is recommended for reliable eth_subscribe support.
POLYGON_HTTP: str = _get("POLYGON_HTTP", "https://polygon-bor-rpc.publicnode.com")
# Detector poll cadence. eth_getLogs only fires when a new block appears (~2s on Polygon),
# so polling faster than that just adds cheap eth_blockNumber checks but notices new blocks
# sooner — directly lowering copy lag. 0.75s keeps most copies under ~1s; can go to 0.5.
ONCHAIN_POLL_SECONDS: float = float(_get("ONCHAIN_POLL_SECONDS", "0.75"))
# On startup, re-scan this many recent blocks (~2.1s each on Polygon, so 100 ≈ 3.5 min) so a
# restart/downtime never drops trades within the copy window — any still fresh get copied,
# already-logged ones de-dupe. (Trades older than MAX_COPY_LAG are stale and ignored anyway.)
ONCHAIN_STARTUP_LOOKBACK_BLOCKS: int = int(_get("ONCHAIN_STARTUP_LOOKBACK_BLOCKS", "100"))
# Max blocks per eth_getLogs scan. Alchemy's FREE tier caps this at 10; a PAID (Pay As You Go)
# plan allows large ranges, so 1000 lets even a long backlog catch up in one or two queries.
# (If you ever revert to free, set ONCHAIN_MAX_SCAN_BLOCKS=10 in .env.)
ONCHAIN_MAX_SCAN_BLOCKS: int = int(_get("ONCHAIN_MAX_SCAN_BLOCKS", "1000"))

# --- Live trading (REAL MONEY) ----------------------------------------------
# OFF by default: the bot records what it WOULD trade ("DRY_RUN") without sending.
# Set ENABLE_LIVE_TRADING=true ONLY after you understand the risks.
def _bool(name: str, default: str = "false") -> bool:
    return _get(name, default).strip().lower() in ("1", "true", "yes", "on")

ENABLE_LIVE_TRADING: bool = _bool("ENABLE_LIVE_TRADING", "false")

# The signing key is read from one of these — NEVER from the spreadsheet:
#   1. POLYMARKET_PK environment variable, or
#   2. PRIVATE_KEY_FILE: path to a file containing only the key (must be chmod 600).
PRIVATE_KEY_FILE: str = _get("PRIVATE_KEY_FILE", "")

# Polymarket account type: 0 = EOA (you hold the key & funds), 1 = email/magic proxy,
# 2 = browser-wallet proxy. For 1/2, FUNDER_ADDRESS must be the proxy wallet holding USDC.
SIGNATURE_TYPE: int = int(_get("SIGNATURE_TYPE", "0"))
FUNDER_ADDRESS: str = _get("FUNDER_ADDRESS", "")

# Hard safety rails.
LIVE_MAX_ORDER_USD: float = float(_get("LIVE_MAX_ORDER_USD", "50"))     # per-order cap (USD)
LIVE_DAILY_MAX_USD: float = float(_get("LIVE_DAILY_MAX_USD", "500"))    # per-run spend cap (USD)
# Only live-copy FRESH trades: never place a real order for a trade older than this
# (protects against backfills and stale/resolved markets). Seconds.
MAX_COPY_LAG_SECONDS: int = int(_get("MAX_COPY_LAG_SECONDS", "120"))
# Whale-match filter: skip a copy if OUR fill (book VWAP for our size) would land more than
# this % away from the whale's own fill price — so every trade we DO copy is near the whale's
# entry. 0 = off (copy everything). e.g. 1.0 only copies trades we can get within 1% of.
MAX_ENTRY_DELTA_PCT: float = float(_get("MAX_ENTRY_DELTA_PCT", "0"))
