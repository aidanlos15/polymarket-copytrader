# Polymarket Paper-Trade Copytrading Bot

Mirrors the on-chain trading of a Polymarket user — by default **@RN1**
(`0x2005d16a84ceefa912d4e380cd32e7ff827875ea`) — as **paper trades** (simulated, no
real money, no real orders ever placed) and records everything to a **local Excel
file** on your computer.

The bot:
1. Polls the target's recent trades from the public Polymarket Data API.
2. For each new trade, "buys/sells" at the **current market price** (CLOB best
   bid/ask) at a **scaled size** (`their_size × SCALE_RATIO`, default 1%).
3. Logs every paper trade to the **Trades** sheet of the workbook.
4. Continuously re-prices open positions via the CLOB midpoint and writes live
   value + unrealized P&L to the **Positions** sheet.

The Excel file is the only datastore — the bot keeps no other state, so it's
restart-safe (already-logged trades are de-duplicated by reading the file).

> ⚠️ **Paper trading only.** RN1's data is public on-chain activity. Simulated fills
> differ from real execution (slippage, liquidity, and the lag between when RN1
> trades and when the bot observes it). Not financial advice.

---

## 1. Install

```bash
cd /Users/aidanosullivan/Desktop
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure (optional)

Defaults work out of the box — the workbook is created at
`./polymarket_paper_trades.xlsx`. To change anything:

```bash
cp .env.example .env
```

| key | meaning | default |
|-----|---------|---------|
| `TARGET_ADDRESS` | wallet to copy | RN1 |
| `TARGET_NAME` | display name (dashboard title / logs) | `RN1` |
| `SCALE_RATIO` | fraction of their size to copy (`0.01` = 1%) | `0.01` |
| `DETECT_INTERVAL_SECONDS` | how often to poll `/trades` for new copies (cheap) | `7` |
| `REPRICE_INTERVAL_SECONDS` | how often to re-price all positions (expensive) | `45` |
| `EXCEL_PATH` | path to the .xlsx file | `./polymarket_paper_trades.xlsx` |

## 3. Run

```bash
python bot.py                # live loop
python bot.py --backfill 50  # seed last 50 trades, then loop
python bot.py --once         # single cycle (good for a first test)
```

The `.xlsx` file is created automatically on first run. Open it in Excel/Numbers any
time to view results. The workbook opens on the **Positions** tab, which has a
**summary dashboard pinned at the top** (Total P&L, Return %, Portfolio Value, Cost
Basis, Open Positions) — frozen so it and the table header stay visible as you scroll.
The P&L card turns green/red with profit/loss, and P&L cells are color-coded throughout.
The **Trades** tab logs every individual trade (long ID/hash columns are hidden for
readability — unhide them via the column headers if you need them).

> **Tip:** if you have the file open in Excel while the bot runs, a save may be
> blocked — the bot prints a notice and retries on the next cycle. Close the file (or
> just glance at it between cycles) to avoid this.

---

## Running 24/7 (macOS launchd)

The bot is installed as a **LaunchAgent** that starts at login, keeps running, and
auto-restarts if it crashes:

- Service file: `~/Library/LaunchAgents/com.user.polymarket-copytrader.plist`
- Live logs: `copytrader.log` (output) and `copytrader.err` (errors) on the Desktop.

| Action | Command |
|--------|---------|
| Check it's running | `launchctl list \| grep polymarket` (a numeric PID = alive) |
| Watch the log live | `tail -f ~/Desktop/copytrader.log` |
| Stop it | `launchctl unload ~/Library/LaunchAgents/com.user.polymarket-copytrader.plist` |
| Start it | `launchctl load ~/Library/LaunchAgents/com.user.polymarket-copytrader.plist` |
| Restart (after editing code/.env) | run the two commands above in sequence |

Notes:
- It runs whenever you're **logged in** to your Mac (sleep pauses it; it resumes on
  wake). For a true always-on server, run it on a machine that stays awake, or convert
  the agent to a `LaunchDaemon`.
- After changing `bot.py` or `.env`, unload + load to pick up the changes.

---

## Run it online 24/7 (cloud VM + web dashboard)

To keep it running whether your computer is on or off, host it on a cheap always-on
Linux VM (~$5/mo). See **[deploy/DEPLOY.md](deploy/DEPLOY.md)** — create a VM, copy the
folder up, run `bash deploy/install.sh`, and it starts both trackers + a web dashboard as
auto-restarting `systemd` services.

**Web dashboard** (`dashboard.py`): a read-only page showing both trackers' KPIs and
positions, refreshing every 30s — viewable from any device at `http://VM_IP:8080`. It
reads `state_<name>.json` snapshots the bot writes each cycle (it never touches the bots
or any keys). Optional HTTP basic-auth via `DASHBOARD_USER` / `DASHBOARD_PASS`. You can
run it locally too: `python dashboard.py` → http://localhost:8080.

---

## Live trading (REAL MONEY) — off by default

> ⚠️ **This places real orders with real funds and is irreversible. Read all of this.**
> By default the bot is in **dry-run**: it records the order it *would* place
> (`live_status = DRY_RUN` on the Trades tab) and sends nothing. Leave it there until
> your friction-adjusted paper P&L is convincingly positive.

**The key is NEVER stored in the spreadsheet.** It is read from one of:
- `POLYMARKET_PK` environment variable, or
- `PRIVATE_KEY_FILE`: a file containing only the key, which **must** be `chmod 600`
  (the bot refuses to start if it's group/world-readable). Keep it out of any
  cloud-synced folder and never commit it.

**One-time on-chain setup (required, or every order is rejected):**
1. Fund the account with **USDC on Polygon**.
2. Grant **USDC + CTF allowances** to the Polymarket exchange contract (do this once via
   the Polymarket UI, or a web3 approval script).
3. Set the account type:
   - `SIGNATURE_TYPE=0` — you hold the key and the funds (EOA).
   - `SIGNATURE_TYPE=1` (email/magic) or `2` (browser wallet) — set `FUNDER_ADDRESS` to
     the **proxy wallet** that holds your USDC.

**Safety rails (env vars):**
| key | meaning | default |
|-----|---------|---------|
| `ENABLE_LIVE_TRADING` | master switch; `false` = dry-run | `false` |
| `LIVE_MAX_ORDER_USD` | per-order cap (a larger copy is scaled down to this) | `50` |
| `LIVE_DAILY_MAX_USD` | total spend cap per process run | `500` |
| `MAX_COPY_LAG_SECONDS` | never live-copy a trade older than this (blocks backfills/stale) | `120` |

**Going live (only after dry-run looks good):** start tiny — set a small `SCALE_RATIO`
and a low `LIVE_MAX_ORDER_USD`, put `ENABLE_LIVE_TRADING=true` + the key env vars in the
plist's `EnvironmentVariables`, reload the service, and **watch the first orders closely**.
Each trade row records `live_status`, `live_order_id`, `live_filled`, `live_avg_price`.

It still copies only fresh trades (≤ `MAX_COPY_LAG_SECONDS`), so a `--backfill` never
fires live orders.

---

## Tracking multiple users at once

The same code runs as one process per target — each just needs its own
`TARGET_ADDRESS`, `TARGET_NAME`, and `EXCEL_PATH`. Two trackers are installed:

| Target | Excel file | LaunchAgent | Logs |
|--------|-----------|-------------|------|
| **@RN1** | `polymarket_paper_trades.xlsx` | `com.user.polymarket-copytrader` | `copytrader.log/.err` |
| **@swisstony** | `swisstony_paper_trades.xlsx` | `com.user.polymarket-copytrader-swisstony` | `copytrader-swisstony.log/.err` |

Each LaunchAgent sets its target via `EnvironmentVariables` in the plist, so the
instances are fully isolated (separate files, logs, and processes) while sharing one
codebase — edit `bot.py` once and both benefit.

**To add another target:** copy a plist in `~/Library/LaunchAgents/`, change the
`Label`, the three `EnvironmentVariables`, and the two log paths, then
`launchctl load` it. (Optionally seed history first with a manual run, e.g.
`TARGET_ADDRESS=0x... TARGET_NAME=foo EXCEL_PATH=~/Desktop/foo.xlsx python bot.py --once --backfill 1000`.)

---

## How it works

| File | Role |
|------|------|
| `config.py` | loads `.env` |
| `polymarket_client.py` | Data API (`/trades`, `/positions`) + CLOB (`/price`, `/midpoint`) with retry/backoff |
| `excel_client.py` | openpyxl wrapper; creates sheets/headers, dedupe, snapshot writes |
| `bot.py` | decoupled loop: fast detect/copy every `DETECT_INTERVAL_SECONDS`, heavy re-price every `REPRICE_INTERVAL_SECONDS` |

**Decoupled cadences:** detecting new trades is one cheap API call, so it runs every
~7s for tight entry timing; re-pricing every position is ~one call per market, so it
runs every ~45s. This keeps copies fast without rate-limiting the price feed.

**Detection lag:** each trade row records `detect_lag_s` = log time − the trade's
on-chain timestamp (Polymarket indexer lag + our poll phase). Watch this column to see
how stale the feed actually is — it sets the floor on how fast copying can ever be, and
no faster polling can beat it.

**Sizing:** scaled/proportional — `paper_size = rn1_size × scale`. SELLs reduce held size
(clamped, never short) and realize P&L; entries use a weighted average.

**Editable SCALE % (in the Excel).** The Positions tab has a yellow input cell (**B5**,
labelled `SCALE % →`) where you type a number **1–100**. The bot reads it every cycle and
**re-sizes every trade — current and historical** — to that percentage. Type a value,
**save the file** (and ideally close it so the bot can write back), and it applies on the
next refresh (~within a cycle).

Because the $1 minimum applies to *your scaled order* (Polymarket rejects sub-$1 orders),
trades whose scaled value is below $1 are **hidden** (rows collapsed, excluded from P&L)
and **reappear when you raise the scale**. At 1% only the whale's ≥$100 trades clear $1; at
10% their ≥$10 trades do; etc. The dashboard header shows `Scale X% · … · N hidden <$1`.

**Minimum trade filter:** trades whose value (`size × price`) is below `MIN_TRADE_USD`
(default **$1**, Polymarket's order minimum) are skipped at ingestion — never logged,
so they never affect positions or P&L. Configurable via `.env`.

**P&L = live mark-to-market, per trade.** Every cycle the bot fetches the *current*
price of each outcome and computes P&L for **every individual trade**:
- BUY: `pnl = paper_size × (current_price − entry_price)`
- SELL: the mirror, `paper_size × (entry_price − current_price)`

These per-trade figures are written back onto the **Trades** sheet (`current_price`,
`current_value`, `pnl`, `pnl_updated`) and aggregated per token on the **Positions**
sheet (`pnl`, `pnl_pct`). The SUMMARY row totals everything — the two sheets always agree.

**Daily realized P&L + Today's P&L.** The dashboard has a **TODAY'S P&L** card (next to
TOTAL P&L) and a **DAILY REALIZED** strip, **newest day first** (e.g. 08, 07, 06…). These
count **realized gains only** — i.e. the P&L of *resolved* positions — attributed to the
day each position was opened. Because it's computed from the full trade history every
cycle, **all past days show immediately** and the per-day values **sum to the REALIZED
total**. (Open positions' mark-to-market is excluded here; it's shown separately in the
UNREALIZED card.)

**Price source — works for every market, even closed ones.** Prices come from
`GET /markets/<conditionId>`, which returns each outcome token's current price for
*active* markets (live) **and** *closed/resolved* markets (→ $1 won / $0 lost) — unlike
`/price` and `/midpoint`, which 404 once the order book is removed. A trade only shows
blank P&L (counted as `unpriced`) if no price could be fetched at all.

**Open vs Resolved.** The same `/markets` response carries a `closed` flag, so each
position is tagged **OPEN** (still trading) or **RESOLVED** (market settled). The split
drives the dashboard:
- **Portfolio Value** = value of **OPEN positions only** — resolved markets are settled
  and no longer "in the portfolio".
- **Realized P&L** = locked-in P&L from RESOLVED markets.
- **Unrealized P&L** = P&L of still-open positions.
- **Total P&L** = realized + unrealized.
- **Total Spent** = the total amount deployed across **all** buys (open + resolved) — your
  cumulative capital outlay / volume bought.

Once a market is closed its price is fixed, so its final prices are **cached and never
re-fetched** — resolution detection is free, and re-pricing gets cheaper as markets
settle (only open markets are queried each cycle after the first).

**Dedupe key:** `transactionHash:asset:outcomeIndex:timestamp` (handles multi-fill txs).

**Resolved/illiquid markets:** if the CLOB book is unavailable, position value falls
back to cost basis and the row is marked `RESOLVED`.

## Troubleshooting

- **Save blocked / `PermissionError`** → the `.xlsx` is open in Excel; close it.
- **Empty Trades sheet** → the target may have no recent trades; try `--backfill 50`.
- **Want it running 24/7** → ask for a `launchd` plist to run it as a background service.
