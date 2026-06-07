"""Live order execution via the Polymarket CLOB — DRY-RUN by default.

SAFETY MODEL
------------
- Live trading is OFF unless config.ENABLE_LIVE_TRADING is true. When off, execute()
  returns a "DRY_RUN" record describing the order it *would* have placed, and sends
  nothing. This lets you run the full pipeline against real signals with zero risk.
- The signing key is loaded from POLYMARKET_PK (env) or a chmod-600 file — NEVER from
  the spreadsheet, and it is never logged or written anywhere.
- Two hard caps: LIVE_MAX_ORDER_USD per order and LIVE_DAILY_MAX_USD per process run.
- Every failure is caught and returned as a record; a bad order never crashes the loop.

NOTE: before any live order works you must (one time) grant USDC + CTF allowances to the
exchange contract (via the Polymarket UI or a web3 approval). Orders are rejected until then.
"""
from __future__ import annotations

import os

import config

# Columns this module contributes to each Trades row.
EXEC_FIELDS = ("live_status", "live_order_id", "live_filled", "live_avg_price")

_DRY = {"live_status": "DRY_RUN", "live_order_id": "", "live_filled": "", "live_avg_price": ""}


def _blank(status: str) -> dict:
    return {"live_status": status, "live_order_id": "", "live_filled": "", "live_avg_price": ""}


class Executor:
    def __init__(self) -> None:
        self.live = config.ENABLE_LIVE_TRADING
        self.client = None
        self._spent = 0.0  # USD spent on BUYs this run (for the daily cap)
        if self.live:
            self._connect()
            print("  [trader] LIVE TRADING ENABLED — real orders will be placed.")
        else:
            print("  [trader] dry-run mode — no real orders will be placed.")

    # --- setup --------------------------------------------------------------

    def _load_key(self) -> str:
        pk = os.environ.get("POLYMARKET_PK", "").strip()
        if pk:
            return pk
        path = config.PRIVATE_KEY_FILE
        if not path or not os.path.exists(path):
            raise RuntimeError(
                "Live trading on but no key found: set POLYMARKET_PK or PRIVATE_KEY_FILE.")
        # Refuse to use a key file that others can read.
        if os.stat(path).st_mode & 0o077:
            raise RuntimeError(f"Key file {path} is group/world-readable. Run: chmod 600 {path}")
        with open(path) as fh:
            return fh.read().strip()

    def _connect(self) -> None:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            config.CLOB_API,
            chain_id=137,  # Polygon
            key=self._load_key(),
            signature_type=config.SIGNATURE_TYPE,
            funder=config.FUNDER_ADDRESS or None,
        )
        # L2 API credentials are derived by signing with the key (one network call).
        client.set_api_creds(client.create_or_derive_api_creds())
        self.client = client

    # --- order execution ----------------------------------------------------

    def execute(self, token_id: str, side: str, usd_amount: float, shares: float,
                fresh: bool = True) -> dict:
        """Place (or simulate) a market order mirroring a copied trade.

        BUY amount is in USD (capped); SELL amount is in shares. `fresh` must be True for
        a real order — stale/backfilled trades are never live-copied. Returns a dict with
        the EXEC_FIELDS describing the outcome. Never raises.
        """
        side = side.upper()

        # Per-order cap (USD): scale a too-large BUY down to the cap.
        amount_usd = min(usd_amount, config.LIVE_MAX_ORDER_USD) if side == "BUY" else 0.0

        if not self.live or self.client is None:
            rec = dict(_DRY)
            if side == "BUY" and usd_amount > config.LIVE_MAX_ORDER_USD:
                rec["live_status"] = f"DRY_RUN(capped ${config.LIVE_MAX_ORDER_USD:g})"
            return rec

        if not fresh:  # backfill / stale trade — never live-copy
            return _blank("SKIPPED_STALE")

        if side == "BUY" and self._spent + amount_usd > config.LIVE_DAILY_MAX_USD:
            return _blank("SKIPPED_DAILY_CAP")

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            args = MarketOrderArgs(
                token_id=str(token_id),
                amount=float(amount_usd if side == "BUY" else shares),
                side=BUY if side == "BUY" else SELL,
            )
            signed = self.client.create_market_order(args)
            resp = self.client.post_order(signed, OrderType.FAK)  # fill-and-kill = market take

            if not isinstance(resp, dict):
                return _blank("ERROR_BADRESP")
            ok = resp.get("success", False)
            status = resp.get("status") or ("OK" if ok else "REJECTED")
            if side == "BUY" and ok:
                self._spent += amount_usd
            return {
                "live_status": f"LIVE:{status}"[:28],
                "live_order_id": resp.get("orderID") or resp.get("orderId") or "",
                "live_filled": resp.get("takingAmount", ""),
                "live_avg_price": resp.get("price", ""),
            }
        except Exception as exc:  # never let an order error kill the loop
            return {"live_status": f"ERROR:{type(exc).__name__}", "live_order_id": "",
                    "live_filled": "", "live_avg_price": str(exc)[:40]}
