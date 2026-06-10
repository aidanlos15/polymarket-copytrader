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
import threading
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


def _f(x) -> float:
    """Best-effort float; blanks/garbage -> 0.0 (Excel cells are often '')."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _book_for_storage(levels: list, full_size: float) -> str:
    """Trim the captured order book to just enough levels to fill the whale's FULL size — so
    ANY scale up to 100% can be re-priced (slippage and all) later — and JSON-encode it
    compactly. Capped at 50 levels to bound storage."""
    out, cum = [], 0.0
    for price, size in levels or []:
        out.append([round(price, 6), round(size, 4)])
        cum += size
        if cum >= full_size or len(out) >= 50:
            break
    return json.dumps(out)


def trade_id(t: dict) -> str:
    """Source-independent dedupe key. Keyed on transaction hash + outcome token so the
    SAME trade detected on-chain and later echoed by the data API maps to one key
    (their timestamps differ, so timestamp must NOT be part of the key)."""
    return f"{t.get('transactionHash', '')}:{t.get('asset', '')}"


# Guards the shared `processed` dedupe set: the detector thread and the data-API backup
# (main thread) both log trades, so check-and-reserve of a trade-id must be atomic or the
# same trade could be logged twice.
_PROC_LOCK = threading.Lock()


def process_new_trades(sheets: ExcelClient, trades: list[dict], processed: set[str],
                       executor: Executor, scale_frac: float,
                       source: str = "onchain", live: bool = False) -> tuple[int, list[int]]:
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
        # Atomically claim this trade-id so the other thread can't also process it.
        with _PROC_LOCK:
            if tid in processed:
                continue
            processed.add(tid)

        asset = str(t.get("asset", ""))
        side = str(t.get("side", "BUY")).upper()
        rn1_size = float(t.get("size", 0) or 0)
        rn1_price = float(t.get("price", 0) or 0)

        # Skip sub-$1 trades: Polymarket's order minimum is $1, so anything below that
        # is dust (resolution/merge remnants). Not logged -> never counted in P&L.
        if rn1_size * rn1_price < config.MIN_TRADE_USD:
            skipped += 1
            continue

        # Detection lag = when we RECEIVED the on-chain logs (recv_ts, stamped in the
        # detector) minus the block time. Computed up front, BEFORE our own get_price/
        # order/Excel work, so it measures pure detection latency, not our processing.
        ref = t.get("recv_ts")
        try:
            ref = float(ref) if ref else time.time()
            lag = max(0, int(round(ref - float(t.get("timestamp", 0)))))
        except (TypeError, ValueError):
            lag = ""
        if isinstance(lag, int):
            lags.append(lag)

        # ONLY copy trades we caught LIVE, within the copy window. A trade we missed in real
        # time — surfaced only later by the stale data-API backfill — is ignored entirely:
        # never logged, never ordered. You can't actually copy a trade after the fact, and
        # pricing it at a later moment is exactly what produced the bogus figures.
        fresh = isinstance(lag, int) and 0 <= lag <= config.MAX_COPY_LAG_SECONDS
        if not fresh:
            continue

        # Our realistic fill: walk the order book for OUR scaled size (real price incl.
        # slippage). We also STORE the book so changing the scale later re-prices the fill
        # for the new size. Fall back to top-of-book, then the whale's fill, on a glitch.
        paper_size = rn1_size * scale_frac
        levels = pm.get_book_levels(asset, side)
        entry, _filled = pm.vwap_from_levels(levels, paper_size)
        if entry is None or entry <= 0:
            entry = pm.get_price(asset, side)
        if entry is None or entry <= 0:
            entry = rn1_price
        paper_cost = paper_size * entry
        book_json = _book_for_storage(levels, rn1_size)

        # Live-copy when the scaled order clears Polymarket's $1 minimum (it's already fresh).
        placeable = paper_cost >= config.MIN_TRADE_USD
        exec_result = executor.execute(
            token_id=asset, side=side, usd_amount=paper_cost, shares=paper_size,
            fresh=placeable, live=live)

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
            "book_levels": book_json,
            **exec_result,
        })
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
# Last-known prices for OPEN markets (token -> price), so a market we don't refresh this
# cycle still has a (slightly stale) price. And when each condition was last refreshed, so
# we can round-robin stalest-first across cycles.
_OPEN_PRICE_CACHE: dict[str, dict[str, float]] = {}     # conditionId -> {token: price}
_LAST_PRICED_AT: dict[str, float] = {}                  # conditionId -> time.monotonic()
_last_trade_marks_at = 0.0                              # last heavy Excel Trades re-mark


def fetch_current_prices(trade_rows: list[dict]) -> tuple[dict[str, float], dict[str, bool]]:
    """Current price per held token — BUDGETED so a big portfolio can't freeze the loop.

    Resolved markets are served from cache for free. For open markets we refresh
    stalest-first (one CLOB call each) only until config.REPRICE_BUDGET_SECONDS is spent;
    any not refreshed this cycle keep their last-known price. So every cycle finishes
    promptly (and the dashboard updates), while all markets get refreshed over a few cycles.
    """
    price_by_token: dict[str, float] = {}
    resolved_by_condition: dict[str, bool] = {}
    conditions = {str(r.get("condition_id", "")) for r in trade_rows if r.get("condition_id")}

    open_conditions: list[str] = []
    for cid in conditions:
        if cid in _RESOLVED_CACHE:                       # settled — no API call needed
            price_by_token.update(_RESOLVED_CACHE[cid])
            resolved_by_condition[cid] = True
            continue
        resolved_by_condition[cid] = False
        if cid in _OPEN_PRICE_CACHE:                     # seed with last-known price
            price_by_token.update(_OPEN_PRICE_CACHE[cid])
        open_conditions.append(cid)

    # Refresh stalest-first, but stop once the time budget is spent.
    open_conditions.sort(key=lambda c: _LAST_PRICED_AT.get(c, 0.0))
    start = time.monotonic()
    refreshed = 0
    for cid in open_conditions:
        if refreshed and (time.monotonic() - start) >= config.REPRICE_BUDGET_SECONDS:
            break
        m = pm.get_market(cid)
        _LAST_PRICED_AT[cid] = time.monotonic()
        refreshed += 1
        if m and m["prices"]:
            price_by_token.update(m["prices"])
            resolved_by_condition[cid] = m["closed"]
            if m["closed"]:
                _RESOLVED_CACHE[cid] = m["prices"]       # cache final prices forever
                _OPEN_PRICE_CACHE.pop(cid, None)
            else:
                _OPEN_PRICE_CACHE[cid] = m["prices"]
        # On a failed fetch we keep the seeded last-known price (already in price_by_token).

    stale = len(open_conditions) - refreshed
    if stale > 0:
        print(f"  [reprice] refreshed {refreshed}/{len(open_conditions)} open markets this "
              f"cycle (budget {config.REPRICE_BUDGET_SECONDS:g}s); {stale} used last-known price")
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
        # ONLY count trades we caught LIVE, i.e. within the copy window (detect_lag small) —
        # regardless of whether on-chain or the data-API backup caught it, since either way
        # it's within the window we'd have actually copied/ordered. Trades we missed (only
        # surfaced by the stale backfill, lag huge) are ignored — we never really copied them,
        # so they don't belong in the portfolio. Also drops the existing backfilled junk.
        try:
            dlag = float(t.get("detect_lag_s"))
        except (TypeError, ValueError):
            dlag = 1e9
        if dlag > config.MAX_COPY_LAG_SECONDS:
            continue

        side = str(t.get("side", "BUY")).upper()
        rn1_size = float(t.get("rn1_size") or 0)
        size = rn1_size * scale_frac            # re-sized to the current scale
        wp = float(t.get("rn1_price") or 0)
        # Re-price OUR fill at the CURRENT scale by re-walking the order book we captured at
        # detection — so changing the scale correctly recomputes the slippage for the new
        # (larger/smaller) size. Falls back to the recorded entry, then the whale's fill.
        entry = None
        _bl = t.get("book_levels")
        if _bl:
            try:
                _levels = json.loads(_bl)
            except (TypeError, ValueError):
                _levels = None
            if _levels:
                entry, _ = pm.vwap_from_levels(_levels, size)
        if entry is None or entry <= 0:
            entry = float(t.get("paper_entry_price") or 0)
        if (entry is None or entry <= 0 or entry > 1.0) and wp > 0:
            entry = wp
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

    # (The per-trade marks are pushed to the Excel Trades sheet later, gated — rewriting the
    # full history is expensive and must never block the dashboard. See end of function.)

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
            "delta": round(abs(avg_entry - whale_entry), 6),  # |our entry - whale's|
            "delta_pct": round(abs(avg_entry - whale_entry) / whale_entry * 100, 2) if whale_entry > 1e-9 else 0.0,
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
    # DOLLAR-weighted average absolute delta = avg |our entry - whale entry|, weighting each
    # position by the $ we put into it (cost basis) so bigger-dollar trades count more.
    _tot_cost = sum(r.get("cost_basis", 0) for r in rows)
    avg_delta = (sum(r.get("cost_basis", 0) * r.get("delta", 0) for r in rows) / _tot_cost
                 if _tot_cost > 1e-9 else 0.0)
    summary = {
        "last_updated": now,
        "avg_delta": round(avg_delta, 6),
        "total_cost": round(total_cost, 4),
        "total_pnl": round(total_pnl, 4),
        "today_pnl": round(today_pnl, 4),
        "daily": [[d, round(p, 4)] for d, p in daily_series],
        "total_pnl_pct": round((total_pnl / total_cost * 100) if total_cost > 1e-9 else 0.0, 2),
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "portfolio_value": round(portfolio_value, 4),
        "peak_open_value": round(update_peak_open(portfolio_value, scale_pct), 4),
        "open_count": open_count,
        "resolved_count": resolved_count,
        "priced_count": priced,
        "unpriced_count": unpriced,
        "hidden_count": hidden,
        "scale_pct": round(scale_pct, 3),
    }
    # Live-only view (real placed orders), computed from the same trades + prices.
    live_rows, live_summary = build_live_view(trades, price_by_token, resolved_by_condition)

    # DASHBOARD FIRST: write the small JSON the web dashboard reads BEFORE any expensive
    # Excel work, so the dashboard refreshes every cycle no matter how big the history is.
    write_state_json(summary, rows, live_summary, live_rows)

    # Excel view (secondary). The Positions sheet is small (rebuilt each cycle); the heavy
    # full-history Trades re-mark runs only every TRADE_MARKS_INTERVAL_SECONDS.
    global _last_trade_marks_at
    sheets.write_positions(rows, summary, scale_pct=scale_pct)
    if (time.monotonic() - _last_trade_marks_at) >= config.TRADE_MARKS_INTERVAL_SECONDS:
        sheets.update_trade_marks(marks)
        _last_trade_marks_at = time.monotonic()
    sheets.flush()   # one disk write per cycle (coalesces this cycle's appends + writes)

    print(f"  scale {scale_pct:g}% | counted {priced} (hidden <$1: {hidden}, unpriced {unpriced}) "
          f"| open {open_count} (value ${portfolio_value:,.2f}) | "
          f"resolved {resolved_count} (real ${realized_pnl:,.2f}) | total P&L ${total_pnl:,.2f}")


def update_peak_open(open_value: float, scale_pct: float) -> float:
    """High-water mark of the OPEN-positions value — the most capital ever tied up in
    open positions at once, i.e. the cash you need available to fund the strategy.
    Persisted in peak_<NAME>.json; resets if the scale changes (a different funding size)."""
    base = os.path.dirname(os.path.abspath(config.EXCEL_PATH))
    path = os.path.join(base, f"peak_{config.TARGET_NAME}.json")
    peak = open_value
    try:
        d = json.load(open(path))
        if round(float(d.get("scale", -1)), 3) == round(scale_pct, 3):
            peak = max(float(d.get("peak", 0)), open_value)
    except (OSError, ValueError, TypeError):
        pass
    try:
        with open(path + ".tmp", "w") as fh:
            json.dump({"scale": round(scale_pct, 3), "peak": peak}, fh)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return peak


def _is_live_placed(t: dict) -> bool:
    """True if this trade resulted in a REAL order being placed on the exchange (not a
    dry-run/skip/error). The executor stamps a successful order with live_status='LIVE:...'
    and a live_order_id; dry-runs are 'DRY_RUN' and failures have no order id."""
    status = str(t.get("live_status", "") or "")
    oid = str(t.get("live_order_id", "") or "")
    return status.startswith("LIVE") and bool(oid.strip())


def update_peak_live(open_value: float) -> float:
    """High-water mark of the LIVE open-position value. Unlike the paper peak this never
    resets on scale changes (live orders are fixed at execution, scale is irrelevant)."""
    base = os.path.dirname(os.path.abspath(config.EXCEL_PATH))
    path = os.path.join(base, f"peak_live_{config.TARGET_NAME}.json")
    peak = open_value
    try:
        d = json.load(open(path))
        peak = max(float(d.get("peak", 0)), open_value)
    except (OSError, ValueError, TypeError):
        pass
    try:
        with open(path + ".tmp", "w") as fh:
            json.dump({"peak": peak}, fh)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return peak


def build_live_view(trades: list[dict], price_by_token: dict[str, float],
                    resolved_by_condition: dict[str, bool]) -> tuple[list[dict], dict]:
    """Build the LIVE-only positions view: aggregate ONLY trades that actually placed a
    real order, sized by the real fill (live_filled / live_avg_price) where available,
    else by the order we sent. Independent of the paper view and the scale slider — live
    orders are real and fixed, so they're shown exactly as executed. Returns (rows, summary)
    in the same shape the dashboard uses for the paper view, so rendering is identical."""
    now = _now_iso()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    agg: dict[str, dict] = defaultdict(lambda: {
        "net_paper_size": 0.0, "total_bought": 0.0, "cost_basis": 0.0, "pnl": 0.0,
        "market_title": "", "outcome": "", "condition_id": "", "cur_price": None,
        "whale_cost": 0.0, "lag_sum": 0.0, "lag_n": 0, "first_ts": 0,
    })
    daily_realized: dict[str, float] = defaultdict(float)
    placed = 0

    for t in trades:
        if not _is_live_placed(t):
            continue
        token = str(t.get("token_id", ""))
        cid = str(t.get("condition_id", ""))
        side = str(t.get("side", "BUY")).upper()
        cur = price_by_token.get(token)
        if cur is None:                      # market price unavailable this cycle — skip
            continue

        # Real fill first; fall back to the order we sent (capped at the per-order max).
        entry = _f(t.get("live_avg_price")) or _f(t.get("paper_entry_price"))
        filled = _f(t.get("live_filled"))
        intended_shares = _f(t.get("rn1_size")) * _f(t.get("scale_ratio"))
        if side == "BUY":
            if filled > 0:
                size = filled
            else:
                usd = min(intended_shares * _f(t.get("paper_entry_price")),
                          config.LIVE_MAX_ORDER_USD)
                size = (usd / entry) if entry > 1e-9 else 0.0
        else:
            size = filled if filled > 0 else intended_shares
        if size <= 0 or entry <= 0:
            continue
        placed += 1

        pnl = size * (cur - entry) if side == "BUY" else size * (entry - cur)
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
        if str(t.get("source", "")).lower() == "onchain":
            try:
                a["lag_sum"] += float(t.get("detect_lag_s"))
                a["lag_n"] += 1
            except (TypeError, ValueError):
                pass
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
            a["whale_cost"] += size * _f(t.get("rn1_price"))
        else:
            a["net_paper_size"] -= size

    rows: list[dict] = []
    total_pnl = total_cost = realized_pnl = unrealized_pnl = portfolio_value = 0.0
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
            realized_pnl += a["pnl"]
            resolved_count += 1
        else:
            unrealized_pnl += a["pnl"]
            portfolio_value += cur_value
            open_count += 1

        rows.append({
            "market_title": a["market_title"], "outcome": a["outcome"], "status": status,
            "trade_time": trade_time, "trade_date": trade_date, "first_ts": a["first_ts"],
            "net_paper_size": round(net, 6), "total_bought": round(a["total_bought"], 6),
            "avg_entry_price": round(avg_entry, 6), "whale_entry": round(whale_entry, 6),
            "delta": round(abs(avg_entry - whale_entry), 6),
            "delta_pct": round(abs(avg_entry - whale_entry) / whale_entry * 100, 2) if whale_entry > 1e-9 else 0.0,
            "avg_lag_s": avg_lag, "cost_basis": round(cost, 4),
            "current_price": round(cur, 6) if cur is not None else "",
            "current_value": round(cur_value, 4), "pnl": round(a["pnl"], 4),
            "pnl_pct": round(pnl_pct, 2), "price_source": "RESOLVED" if resolved else "OPEN",
            "last_updated": now, "token_id": token, "condition_id": a["condition_id"],
        })
    rows.sort(key=lambda r: r.get("first_ts", 0), reverse=True)
    daily_series = sorted(daily_realized.items(), key=lambda kv: kv[0], reverse=True)[:30]
    # Dollar-weighted average absolute delta (weight each position by $ cost basis).
    _tot_cost = sum(r.get("cost_basis", 0) for r in rows)
    avg_delta = (sum(r.get("cost_basis", 0) * r.get("delta", 0) for r in rows) / _tot_cost
                 if _tot_cost > 1e-9 else 0.0)
    summary = {
        "last_updated": now,
        "avg_delta": round(avg_delta, 6),
        "total_cost": round(total_cost, 4),
        "total_pnl": round(total_pnl, 4),
        "today_pnl": round(daily_realized.get(today_str, 0.0), 4),
        "daily": [[d, round(p, 4)] for d, p in daily_series],
        "total_pnl_pct": round((total_pnl / total_cost * 100) if total_cost > 1e-9 else 0.0, 2),
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "portfolio_value": round(portfolio_value, 4),
        "peak_open_value": round(update_peak_live(portfolio_value), 4),
        "open_count": open_count,
        "resolved_count": resolved_count,
        "priced_count": placed,
        "unpriced_count": 0,
        "hidden_count": 0,
        "scale_pct": "",          # scale is a paper-only what-if; N/A for live
        "order_count": placed,
    }
    return rows, summary


def read_live() -> bool:
    """Runtime live-trading flag for this tracker, set by the dashboard toggle and stored
    in live_<NAME>.txt. Defaults to config.ENABLE_LIVE_TRADING (off) if unset."""
    base = os.path.dirname(os.path.abspath(config.EXCEL_PATH))
    path = os.path.join(base, f"live_{config.TARGET_NAME}.txt")
    try:
        return open(path).read().strip().lower() == "on"
    except OSError:
        return bool(config.ENABLE_LIVE_TRADING)


_scale_cache = 100.0


def read_scale() -> float:
    """Current copy SCALE % (0.01-100, decimals allowed), set by the dashboard in
    scale_<NAME>.txt. Read by BOTH the detector and pricing threads each cycle. Keeps the
    last good value if the file is briefly unreadable, so scale never spikes to 100%."""
    global _scale_cache
    base = os.path.dirname(os.path.abspath(config.EXCEL_PATH))
    path = os.path.join(base, f"scale_{config.TARGET_NAME}.txt")
    try:
        v = float(open(path).read().strip())
        if 0.01 <= v <= 100.0:
            _scale_cache = v
    except (OSError, ValueError):
        pass
    return _scale_cache


def write_state_json(summary: dict, rows: list[dict],
                     live_summary: dict | None = None,
                     live_rows: list[dict] | None = None) -> None:
    """Write a small JSON snapshot next to the Excel file, for the web dashboard to read.
    Carries BOTH the paper view (summary/positions) and the live view (live_summary/
    live_positions) every cycle, so the dashboard can switch instantly and paper data is
    never lost when live mode is on. Atomic via temp-file rename."""
    state = {
        "name": config.TARGET_NAME,
        "address": config.TARGET_ADDRESS,
        "live": read_live(),
        "excel_file": os.path.basename(os.path.abspath(config.EXCEL_PATH)),
        "summary": summary,
        "positions": rows,
        "live_summary": live_summary or {},
        "live_positions": live_rows or [],
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
                processed: set[str], live: bool = False) -> int:
    """Backup source: poll the data API and log any trades on-chain missed. Same dedupe
    set, so trades already caught on-chain are not re-logged."""
    trades = list(reversed(pm.get_trades(config.TARGET_ADDRESS, limit=limit)))  # oldest-first
    added, _ = process_new_trades(sheets, trades, processed, executor, scale_frac,
                                  source="dataapi", live=live)
    return added


def detection_loop(sheets: ExcelClient, detector: OnchainDetector, executor: Executor,
                   processed: set[str], stop: threading.Event) -> None:
    """DEDICATED detector thread — the ONLY thing in the hot path of a copy.

    Each tick: poll the chain, and for any new whale trade place the live order (if live)
    and log it. It shares nothing slow with the main thread: re-pricing every market and
    writing the Excel/dashboard files happen elsewhere, so a copy is never stalled behind
    them. This is what keeps real copy latency at the on-chain floor (~1-3s)."""
    while not stop.is_set():
        try:
            live_on = read_live()
            scale_frac = read_scale() / 100.0
            onchain = detector.poll_once()
            if onchain:
                process_new_trades(sheets, onchain, processed, executor, scale_frac,
                                   source="onchain", live=live_on)
        except Exception as exc:  # never let the detector thread die
            print(f"  [detect] error: {exc!r}")
        stop.wait(config.ONCHAIN_POLL_SECONDS)


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

    # Shared dedupe set, seeded once from the file. Both threads check-and-reserve under
    # _PROC_LOCK, so it stays consistent without re-reading the whole sheet every loop.
    processed = sheets.read_processed_keys()

    # First data-API pass fetches `--backfill N` trades (seed history); then the normal window.
    first_limit = args.backfill if args.backfill else config.TRADES_PER_POLL

    if args.once:
        sp = read_scale()
        dataapi_pass(sheets, first_limit, executor, sp / 100.0, processed)
        mark_to_market(sheets, sp)
        return

    # Defer the first heavy full-history Trades re-mark by a full interval, so the opening
    # cycles are fast and the dashboard goes live immediately (the re-mark is cosmetic Excel).
    global _last_trade_marks_at
    _last_trade_marks_at = time.monotonic()

    # Detection runs on its own thread (fast, never blocked). Pricing + the data-API
    # backup + Excel/dashboard writes run here on the main thread on their own cadences.
    stop = threading.Event()
    det = threading.Thread(target=detection_loop, name="detector",
                           args=(sheets, detector, executor, processed, stop), daemon=True)
    det.start()

    first = True
    last_reprice = last_dataapi = 0.0
    try:
        while True:
            try:
                sp = read_scale()

                # REPRICE FIRST: refresh the dashboard from the existing sheet right away
                # (so it goes live fast on startup) before the slower data-API catch-up.
                if first or (time.monotonic() - last_reprice) >= config.REPRICE_INTERVAL_SECONDS:
                    mark_to_market(sheets, sp)
                    last_reprice = time.monotonic()

                # BACKUP/SEED: the data API. Seeds history on the first pass, then reconciles
                # occasionally (catches anything on-chain missed); same dedupe -> no doubles.
                # Backup trades are stale (lag > MAX_COPY_LAG) so they never place live orders.
                if first or (time.monotonic() - last_dataapi) >= config.DATAAPI_INTERVAL_SECONDS:
                    dataapi_pass(sheets, first_limit if first else config.TRADES_PER_POLL,
                                 executor, sp / 100.0, processed, live=read_live())
                    last_dataapi = time.monotonic()
                first = False
            except Exception as exc:  # keep the loop alive across transient failures
                print(f"  cycle error: {exc!r}")
            time.sleep(1.0)  # light tick; reprice/data-API gated by their own intervals
    finally:
        stop.set()


if __name__ == "__main__":
    main()
