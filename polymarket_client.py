"""Thin HTTP client for the public Polymarket APIs.

All endpoints used here are public and require no authentication:

  - Data API   https://data-api.polymarket.com
        GET /trades?user=<addr>      -> a user's individual fills (most recent first)
        GET /positions?user=<addr>   -> a user's current holdings (used as a fallback)

  - CLOB API   https://clob.polymarket.com
        GET /price?token_id=<id>&side=buy|sell  -> best ask / best bid for a token
        GET /midpoint?token_id=<id>             -> midpoint price (re-pricing positions)

Endpoint shapes verified against https://docs.polymarket.com (api-reference/core/*).
"""
from __future__ import annotations

import time
from typing import Any

import requests

import config

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "paper-copytrader/1.0"})

_MAX_RETRIES = 4
_BACKOFF_BASE = 1.5  # seconds; exponential


def _get(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET with retry/backoff. Returns parsed JSON, or None on persistent failure.

    A 404 is treated as a soft "no data" (common for resolved markets) and returns
    None immediately rather than retrying.
    """
    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _SESSION.get(url, params=params, timeout=15)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                # Rate limited — back off harder and retry.
                time.sleep(_BACKOFF_BASE * (2 ** attempt) * 2)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            time.sleep(_BACKOFF_BASE * (2 ** attempt))
    print(f"[polymarket] GET failed after {_MAX_RETRIES} tries: {url} ({last_err})")
    return None


# --- Data API ---------------------------------------------------------------

def get_trades(user: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """Return a user's recent taker fills, most recent first.

    Each item includes: side, asset (ERC-1155 token id), conditionId, size, price,
    timestamp, title, slug, outcome, outcomeIndex, transactionHash.
    """
    data = _get(
        f"{config.DATA_API}/trades",
        {"user": user, "limit": limit, "offset": offset, "takerOnly": "true"},
    )
    return data if isinstance(data, list) else []


def get_positions(user: str) -> list[dict]:
    """Return a user's current positions (used only as a price/metadata fallback)."""
    data = _get(
        f"{config.DATA_API}/positions",
        {"user": user, "sizeThreshold": 0, "limit": 500},
    )
    return data if isinstance(data, list) else []


# --- CLOB API ---------------------------------------------------------------

def get_price(token_id: str, side: str) -> float | None:
    """Best market price for a token. side='BUY' -> best ask; side='SELL' -> best bid.

    This is the price our paper order would realistically fill at.
    """
    clob_side = "buy" if side.upper() == "BUY" else "sell"
    data = _get(f"{config.CLOB_API}/price", {"token_id": token_id, "side": clob_side})
    if isinstance(data, dict) and data.get("price") not in (None, ""):
        try:
            return float(data["price"])
        except (TypeError, ValueError):
            return None
    return None


def get_fill_price(token_id: str, side: str, shares: float) -> tuple[float | None, bool]:
    """Realistic fill price for taking `shares` from the book — i.e. what a real market order
    of OUR size would actually pay, slippage included, WITHOUT placing it.

    Walks the book from the best price outward (lowest asks for a BUY, highest bids for a
    SELL), filling `shares`, and returns (vwap, fully_filled). vwap is the volume-weighted
    average price over the filled portion; fully_filled is False if the book didn't have
    enough depth for our whole size. Returns (None, False) if the book is unavailable.
    """
    if shares is None or shares <= 0:
        return None, False
    data = _get(f"{config.CLOB_API}/book", {"token_id": token_id})
    if not isinstance(data, dict):
        return None, False
    raw = data.get("asks") if side.upper() == "BUY" else data.get("bids")
    levels = []
    for lv in raw or []:
        try:
            levels.append((float(lv["price"]), float(lv["size"])))
        except (TypeError, ValueError, KeyError):
            continue
    if not levels:
        return None, False
    # Best price first: a buyer takes the lowest asks; a seller hits the highest bids.
    levels.sort(key=lambda x: x[0], reverse=(side.upper() == "SELL"))
    remaining, cost, filled = shares, 0.0, 0.0
    for price, size in levels:
        take = min(size, remaining)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled <= 0:
        return None, False
    return cost / filled, remaining <= 1e-9


def get_midpoint(token_id: str) -> float | None:
    """Midpoint (avg of best bid/ask) for a token. Returns None if the book is gone."""
    data = _get(f"{config.CLOB_API}/midpoint", {"token_id": token_id})
    if isinstance(data, dict) and data.get("mid") not in (None, ""):
        try:
            return float(data["mid"])
        except (TypeError, ValueError):
            return None
    return None


def get_market(condition_id: str) -> dict | None:
    """Return {'prices': {token_id: current_price}, 'closed': bool} for a market.

    Uses GET /markets/<conditionId>, which returns each token's current price and a
    `closed` flag. Works for BOTH active markets (live price, closed=false) and
    resolved ones (price 1.0 / 0.0, closed=true) — unlike /price and /midpoint, which
    404 once the order book is removed. Returns None if the market can't be fetched.
    """
    data = _get(f"{config.CLOB_API}/markets/{condition_id}")
    if not isinstance(data, dict):
        return None
    prices: dict[str, float] = {}
    for tok in data.get("tokens", []) or []:
        tid = str(tok.get("token_id", ""))
        try:
            prices[tid] = float(tok.get("price"))
        except (TypeError, ValueError):
            continue
    return {"prices": prices, "closed": bool(data.get("closed"))}
