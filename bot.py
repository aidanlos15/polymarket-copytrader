"""Polymarket copytrading paper-trade bot.

Continuously mirrors a target Polymarket user's trades as *paper* trades (no real
orders are ever placed), logging everything to a local Excel file and re-pricing
positions live.

Two decoupled cadences run in one loop:
  - DETECT every few seconds: cheap /trades poll to copy new trades fast (entry timing).
  - REPRICE less often: the expensive per-market mark-to-market of all positions.

Usage:
    python bot.py                # start the live loop
    python bot.py --backfill 50  # seed from the target's last 50 trades, then loop
    python bot.py --once         # run a single cycle and exit (handy for testing)

The Excel file is the only datastore — restart-safe via trade-id dedupe.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

import config
import polymarket_client as pm
from excel_client import ExcelClient
from onchain_detector import OnchainDetector
from trader import Executor


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def trade_id(t: dict) -> str:
    """Source-independent dedupe key. Keyed on transaction hash + outcome token so the
    SAME trade detected on-chain and later echoed by the data API maps to one key
    (their timestamps differ, so timestamp must NOT be part of the key)."""
    return f"{t.get('transactionHash', '')}:{t.get('asset', '')}"


def process_new_trades(sheets: ExcelClient, trades: list[dict], processed: set[str],
                       executor: Executor, scale_frac: float,
                       source: str = "onchain") -> tuple[int, list[int]]:
    """Append any not-yet-seen trades as paper trades (and place live orders if enabled).

    `trades` must be oldest-first. `processed` is the set of dedupe keys (mutated as we
    log). `source` ("onchain"/"dataapi") is recorded per trade. Returns (count_added,
    detect_lags) where detect_lag_s = our log time minus the trade's on-chain timestamp.
    """
    added = 0
    skipped = 0
    lags: list[int] = []
    for t in trades:
        tid = trade_id(t)
        if tid in processed:
            continue

        asset = str(t.get("asset", ""))
        side = str(t.get("side", "BUY")).upper()
        rn1_size = float(t.get("size", 0) or 0)
        rn1_price = float(t.get("price", 0) or 0)

        # Skip sub-$1 trades: Polymarket's order minimum is $1, so anything below that
        # is dust (resolution/merge remnants). Not logged -> never counted in P&L.
        if rn1_size * rn1_price < config.MIN_TRADE_USD:
            skipped += 1
            continue

        # Price our paper fill at the *current* market price for that side.
        entry = pm.get_price(asset, side)
        if entry is None:
            # Fall back to the price RN1 actually traded at if the book is unavailable.
            entry = float(t.get("price", 0) or 0)

        paper_size = rn1_size * scale_frac
        paper_cost = paper_size * entry

        try:
            lag = int(time.time()) - int(t.get("timestamp", 0))
        except (TypeError, ValueError):
            lag = ""
        if isinstance(lag, int) and lag >= 0:
            lags.append(lag)

        # Place (or simulate, in dry-run) the live market order mirroring this trade.
        # Only live-copy FRESH trades whose scaled order clears Polymarket's $1 minimum.
        fresh = isinstance(lag, int) and 0 <= lag <= config.MAX_COPY_LAG_SECONDS
        placeable = fresh and paper_cost >= config.MIN_TRADE_USD
        exec_result = executor.execute(
            token_id=asset, side=side, usd_amount=paper_cost, shares=paper_size, fresh=placeable)

        sheets.append_trade({
            "trade_id": tid,
            "detected_at": _now_iso(),
            "trade_ts": t.get("timestamp", ""),
            "market_title": t.get("title", ""),
            "slug": t.get("slug", ""),
            "outcome": t.get("outcome", ""),
            "side": side,
            "token_id": asset,
            "condition_id": t.get("conditionId", ""),
            "rn1_size": round(rn1_size, 4),
            "rn1_price": t.get("price", ""),
            "scale_ratio": round(scale_frac, 4),
            "paper_size": round(paper_size, 6),
            "paper_entry_price": round(entry, 6),
            "paper_cost": round(paper_cost, 6),
            "tx_hash": t.get("transactionHash", ""),
            "detect_lag_s": lag,
            "source": source,
            **exec_result,
        })
        processed.add(tid)
        added += 1
        lag_txt = f"{lag}s" if isinstance(lag, int) else "?"
        print(f"  + [{source} {lag_txt}] {side} {paper_size:.4f} @ {entry:.4f} "
              f"[{exec_result['live_status']}]  {t.get('title','')[:42]}")
    # Only mention skipped dust when we also logged something, to avoid spamming the
    # log on every routine detect pass (the latest-N window always has a few sub-$1s).
    if skipped and added:
        print(f"  (also skipped {skipped} sub-${config.MIN_TRADE_USD:g} dust trade(s))")
    return added, lags


# Final prices of markets already known to be resolved/closed. Once a market closes its
# price is fixed (1.0/0.0), so we cache it and stop calling the API for it — resolution
# detection is effectively free and re-pricing gets cheaper as more markets settle.
_RESOLVED_CACHE: dict[str, dict[str, float]] = {}


def fetch_current_prices(trade_rows: list[dict]) -> tuple[dict[str, float], dict[str, bool]]:
    """Fetch the current price for every token we hold, one call per market.

    Returns (price_by_token, resolved_by_condition). Markets already in the resolved
    cache are served from it with no API call. Many trades share a market, so we query
    GET /markets/<conditionId> once per unique conditionId.
    """
    price_by_token: dict[str, float] = {}
    resolved_by_condition: dict[str, bool] = {}
    conditions = {str(r.get("condition_id", "")) for r in trade_rows if r.get("condition_id")}
    for cid in conditions:
        if cid in _RESOLVED_CACHE:                       # settled — no API call needed
            price_by_token.update(_RESOLVED_CACHE[cid])
            resolved_by_condition[cid] = True
            continue
        m = pm.get_market(cid)
        if m and m["prices"]:
            price_by_token.update(m["prices"])
            resolved_by_condition[cid] = m["closed"]
            if m["closed"]:
                _RESOLVED_CACHE[cid] = m["prices"]       # cache final prices forever
        else:
            resolved_by_condition[cid] = False
    return price_by_token, resolved_by_condition


def mark_to_market(sheets: ExcelClient, scale_pct: float) -> None:
    """Re-size every trade to the current SCALE %, mark to market, and recompute P&L.

    paper_size = rn1_size * (scale_pct/100); P&L = paper_size * (current - entry) for a
    BUY (mirror for SELL). Trades whose scaled order is below Polymarket's $1 minimum are
    HIDDEN and excluded from P&L/positions — raising the scale brings them back.
    Positions split into OPEN (counts toward portfolio value / unrealized) and RESOLVED
    (settled → realized P&L, excluded from portfolio value).
    """
    now = _now_iso()
    scale_frac = scale_pct / 100.0
    trades = sheets.read_all_trades()
    price_by_token, resolved_by_condition = fetch_current_prices(trades)

    marks: dict[str, dict] = {}                 # trade_id -> per-cycle columns
    agg: dict[str, dict] = defaultdict(lambda: {
        "net_paper_size": 0.0, "total_bought": 0.0, "cost_basis": 0.0, "pnl": 0.0,
        "market_title": "", "outcome": "", "condition_id": "", "cur_price": None,
        "whale_cost": 0.0, "lag_sum": 0.0, "lag_n": 0, "first_ts": 0,
    })
    priced = unpriced = hidden = 0
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_realized: dict[str, float] = defaultdict(float)  # entry-date -> realized P&L

    for t in trades:
        tid = str(t.get("trade_id", ""))
        token = str(t.get("token_id", ""))
        cid = str(t.get("condition_id", ""))
        side = str(t.get("side", "BUY")).upper()
        entry = float(t.get("paper_entry_price") or 0)
        rn1_size = float(t.get("rn1_size") or 0)
        size = rn1_size * scale_frac            # re-sized to the current scale
        cost = size * entry
        cur = price_by_token.get(token)

        # Below the $1 minimum at this scale -> hide the row, exclude from P&L.
        below_min = cost < config.MIN_TRADE_USD
        if below_min:
            hidden += 1

        if cur is None:
            marks[tid] = {"paper_size": round(size, 6), "paper_cost": round(cost, 6),
                          "current_price": "", "current_value": "", "pnl": "",
                          "pnl_updated": now, "_hidden": below_min}
            unpriced += 1
            continue

        if side == "BUY":
            pnl = size * (cur - entry)
        else:  # SELL — profit if the price fell after we sold
            pnl = size * (entry - cur)
        marks[tid] = {
            "paper_size": round(size, 6),
            "paper_cost": round(cost, 6),
            "current_price": round(cur, 6),
            "current_value": round(size * cur if side == "BUY" else 0.0, 4),
            "pnl": round(pnl, 4),
            "pnl_updated": now,
            "_hidden": below_min,
        }
        if below_min:
            continue                            # excluded from positions / P&L
        priced += 1

        # Daily P&L = REALIZED gains only (resolved trades), attributed to the day the
        # position was opened. Computable for all history, so past days show immediately.
        if resolved_by_condition.get(cid, False):
            try:
                d = datetime.fromtimestamp(
                    int(float(t.get("trade_ts") or 0)), tz=timezone.utc).strftime("%Y-%m-%d")
                daily_realized[d] += pnl
            except (ValueError, OSError, OverflowError):
                pass

        a = agg[token]
        a["market_title"] = t.get("market_title", a["market_title"])
        a["outcome"] = t.get("outcome", a["outcome"])
        a["condition_id"] = cid
        a["cur_price"] = cur
        a["pnl"] += pnl
        # Detection lag — ONLY for live on-chain copies (the real copy latency). Trades
        # imported via the data-API backfill have a misleading "age at import" lag, so
        # they're excluded; a position with no live-copied trades shows a blank lag.
        if str(t.get("source", "")).lower() == "onchain":
            try:
                a["lag_sum"] += float(t.get("detect_lag_s"))
                a["lag_n"] += 1
            except (TypeError, ValueError):
                pass
        # Entry timestamp = earliest trade on this token.
        try:
            ts = int(float(t.get("trade_ts") or 0))
            if ts and (a["first_ts"] == 0 or ts < a["first_ts"]):
                a["first_ts"] = ts
        except (TypeError, ValueError):
            pass
        if side == "BUY":
            a["net_paper_size"] += size
            a["total_bought"] += size
            a["cost_basis"] += size * entry
            a["whale_cost"] += size * float(t.get("rn1_price") or 0)  # whale's own fill price
        else:
            a["net_paper_size"] -= size

    # Write per-trade marks back to the Trades sheet.
    sheets.update_trade_marks(marks)

    # Build the per-token Positions view and split Open vs Resolved.
    rows: list[dict] = []
    total_pnl = total_cost = 0.0
    realized_pnl = unrealized_pnl = portfolio_value = 0.0
    open_count = resolved_count = 0
    for token, a in agg.items():
        cur = a["cur_price"]
        net = a["net_paper_size"]
        cost = a["cost_basis"]
        cur_value = net * cur if cur is not None else 0.0
        avg_entry = (cost / a["total_bought"]) if a["total_bought"] > 1e-9 else 0.0
        whale_entry = (a["whale_cost"] / a["total_bought"]) if a["total_bought"] > 1e-9 else 0.0
        avg_lag = round(a["lag_sum"] / a["lag_n"]) if a["lag_n"] else ""
        if a["first_ts"]:
            _dt = datetime.fromtimestamp(a["first_ts"], tz=timezone.utc)
            trade_time, trade_date = _dt.strftime("%Y-%m-%d %H:%M"), _dt.strftime("%Y-%m-%d")
        else:
            trade_time = trade_date = ""
        pnl_pct = (a["pnl"] / cost * 100) if cost > 1e-9 else 0.0
        resolved = resolved_by_condition.get(a["condition_id"], False)
        status = "RESOLVED" if resolved else "OPEN"

        total_pnl += a["pnl"]
        total_cost += cost
        if resolved:
            realized_pnl += a["pnl"]            # settled — locked in
            resolved_count += 1
        else:
            unrealized_pnl += a["pnl"]          # still at risk
            portfolio_value += cur_value        # only OPEN positions count here
            open_count += 1

        rows.append({
            "market_title": a["market_title"],
            "outcome": a["outcome"],
            "status": status,
            "trade_time": trade_time,
            "trade_date": trade_date,
            "first_ts": a["first_ts"],
            "net_paper_size": round(net, 6),
            "total_bought": round(a["total_bought"], 6),
            "avg_entry_price": round(avg_entry, 6),
            "whale_entry": round(whale_entry, 6),
            "avg_lag_s": avg_lag,
            "cost_basis": round(cost, 4),
            "current_price": round(cur, 6) if cur is not None else "",
            "current_value": round(cur_value, 4),
            "pnl": round(a["pnl"], 4),
            "pnl_pct": round(pnl_pct, 2),
            "price_source": "RESOLVED" if resolved else "OPEN",
            "last_updated": now,
            "token_id": token,
            "condition_id": a["condition_id"],
        })
    # Most recently opened first (by entry timestamp).
    rows.sort(key=lambda r: r.get("first_ts", 0), reverse=True)
    # Daily realized P&L, most-recent-day first (8th, 7th, 6th, ...). Sums to total realized.
    daily_series = sorted(daily_realized.items(), key=lambda kv: kv[0], reverse=True)[:30]
    today_pnl = daily_realized.get(today_str, 0.0)
    summary = {
        "last_updated": now,
        "total_cost": round(total_cost, 4),
        "total_pnl": round(total_pnl, 4),
        "today_pnl": round(today_pnl, 4),
        "daily": [[d, round(p, 4)] for d, p in daily_series],
        "total_pnl_pct": round((total_pnl / total_cost * 100) if total_cost > 1e-9 else 0.0, 2),
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "portfolio_value": round(portfolio_value, 4),
        "open_count": open_count,
        "resolved_count": resolved_count,
        "priced_count": priced,
        "unpriced_count": unpriced,
        "hidden_count": hidden,
        "scale_pct": round(scale_pct),
    }
    sheets.write_positions(rows, summary, scale_pct=scale_pct)
    write_state_json(summary, rows)
    print(f"  scale {scale_pct:g}% | counted {priced} (hidden <$1: {hidden}, unpriced {unpriced}) "
          f"| open {open_count} (value ${portfolio_value:,.2f}) | "
          f"resolved {resolved_count} (real ${realized_pnl:,.2f}) | total P&L ${total_pnl:,.2f}")


def write_state_json(summary: dict, rows: list[dict]) -> None:
    """Write a small JSON snapshot (summary + positions) next to the Excel file, for the
    web dashboard to read. Atomic via temp-file rename so the dashboard never sees a
    half-written file."""
    state = {
        "name": config.TARGET_NAME,
        "address": config.TARGET_ADDRESS,
        "live": config.ENABLE_LIVE_TRADING,
        "summary": summary,
        "positions": rows,
    }
    base = os.path.dirname(os.path.abspath(config.EXCEL_PATH))
    path = os.path.join(base, f"state_{config.TARGET_NAME}.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError as exc:
        print(f"  [state] could not write {path}: {exc}")


def dataapi_pass(sheets: ExcelClient, limit: int, executor: Executor, scale_frac: float,
                processed: set[str]) -> int:
    """Backup source: poll the data API and log any trades on-chain missed. Same dedupe
    set, so trades already caught on-chain are not re-logged."""
    trades = list(reversed(pm.get_trades(config.TARGET_ADDRESS, limit=limit)))  # oldest-first
    added, _ = process_new_trades(sheets, trades, processed, executor, scale_frac, source="dataapi")
    return added


def main() -> None:
    ap = argparse.ArgumentParser(description="Polymarket paper-trade copytrading bot")
    ap.add_argument("--backfill", type=int, default=0,
                    help="seed from the target's last N trades on first detect pass")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = ap.parse_args()

    print(f"Tracking @{config.TARGET_NAME} ({config.TARGET_ADDRESS}) "
          f"| on-chain poll {config.ONCHAIN_POLL_SECONDS}s | reprice {config.REPRICE_INTERVAL_SECONDS}s")
    sheets = ExcelClient()
    print(f"Writing to {config.EXCEL_PATH}")
    executor = Executor()
    detector = OnchainDetector()
    print(f"On-chain detection via {config.POLYGON_HTTP.split('/v2/')[0]}/v2/***")

    # Scale (1-100%) is set from the web dashboard, stored in scale_<NAME>.txt next to the
    # Excel file. Default 100% (full size); trades are recorded at the target's full size
    # (rn1_size) and re-scaled to this % every cycle, so changing it recomputes all history.
    scale_pct = 100.0
    scale_file = os.path.join(os.path.dirname(os.path.abspath(config.EXCEL_PATH)),
                              f"scale_{config.TARGET_NAME}.txt")

    def current_scale() -> float:
        nonlocal scale_pct
        try:
            v = float(open(scale_file).read().strip())
            if 1.0 <= v <= 100.0:
                scale_pct = v
        except (OSError, ValueError):
            pass
        return scale_pct

    # First detect pass fetches `--backfill N` trades (seed history); after that, the
    # normal per-poll window.
    first_limit = args.backfill if args.backfill else config.TRADES_PER_POLL

    if args.once:
        sp = current_scale()
        processed = sheets.read_processed_keys()
        dataapi_pass(sheets, first_limit, executor, sp / 100.0, processed)
        mark_to_market(sheets, sp)
        return

    first = True
    last_reprice = last_dataapi = 0.0
    while True:
        try:
            sp = current_scale()
            processed = sheets.read_processed_keys()

            # PRIMARY: on-chain, every loop (~2s) — copies fire within ~1-2s of the whale.
            onchain = detector.poll_once()
            if onchain:
                process_new_trades(sheets, onchain, processed, executor, sp / 100.0,
                                   source="onchain")

            # BACKUP/SEED: the data API. Seeds history on the first loop, then reconciles
            # occasionally (catches anything on-chain missed); same dedupe -> no doubles.
            if first or (time.monotonic() - last_dataapi) >= config.DATAAPI_INTERVAL_SECONDS:
                dataapi_pass(sheets, first_limit if first else config.TRADES_PER_POLL,
                             executor, sp / 100.0, processed)
                last_dataapi = time.monotonic()

            if first or (time.monotonic() - last_reprice) >= config.REPRICE_INTERVAL_SECONDS:
                mark_to_market(sheets, sp)
                last_reprice = time.monotonic()
            first = False
        except Exception as exc:  # keep the loop alive across transient failures
            print(f"  cycle error: {exc!r}")
        time.sleep(config.ONCHAIN_POLL_SECONDS)


if __name__ == "__main__":
    main()
