"""Read-only web dashboard for the copytrader bots.

Reads the state_<name>.json snapshots written each cycle by bot.py and renders a styled
page (KPI cards + open/resolved positions) for every tracked target. Read-only: it never
touches the bots, the Excel files, or any keys.

Run:  python dashboard.py            # http://0.0.0.0:8080
Env:  DASHBOARD_DATA_DIR  where the state_*.json files live (default: this folder)
      DASHBOARD_PORT      port (default 8080)
      DASHBOARD_USER/PASS optional HTTP basic-auth (set both to require login)
"""
from __future__ import annotations

import glob
import json
import os

from flask import Flask, Response, request

DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
USER = os.environ.get("DASHBOARD_USER", "")
PW = os.environ.get("DASHBOARD_PASS", "")

app = Flask(__name__)


def _auth_ok() -> bool:
    if not USER:
        return True
    a = request.authorization
    return bool(a and a.username == USER and a.password == PW)


def _load_states() -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(DATA_DIR, "state_*.json"))):
        try:
            with open(p) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def _money(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def _cls(x) -> str:
    try:
        return "pos" if float(x) > 0 else ("neg" if float(x) < 0 else "")
    except (TypeError, ValueError):
        return ""


CSS = """
* { box-sizing: border-box; }
body { margin:0; background:#0d1117; color:#e6edf3; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
.wrap { max-width:1200px; margin:0 auto; padding:24px; }
h1 { font-size:18px; margin:0 0 2px; }
.sub { color:#8b949e; font-size:12px; margin-bottom:14px; }
.live { color:#f0883e; font-weight:700; }
.cards { display:grid; grid-template-columns:repeat(7,1fr); gap:10px; margin-bottom:14px; }
.daily { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:18px; }
.day { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:4px 9px; text-align:center; }
.day .d { color:#8b949e; font-size:10px; }
.day .v { font-size:12px; font-weight:700; }
.card { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:12px 14px; }
.card .label { color:#8b949e; font-size:10px; letter-spacing:.04em; text-transform:uppercase; }
.card .val { font-size:20px; font-weight:700; margin-top:4px; }
.card.green { background:#0f2a16; border-color:#1f7a33; }
.card.red { background:#2c1416; border-color:#a23b3b; }
table { width:100%; border-collapse:collapse; background:#161b22; border:1px solid #30363d; border-radius:10px; overflow:hidden; }
th,td { padding:7px 10px; text-align:right; border-bottom:1px solid #21262d; white-space:nowrap; }
th { background:#1c2330; color:#8b949e; font-size:11px; text-transform:uppercase; position:sticky; top:0; }
td.l,th.l { text-align:left; }
tr:hover td { background:#1b2230; }
.pos { color:#3fb950; } .neg { color:#f85149; }
.tag { font-size:10px; padding:1px 7px; border-radius:10px; }
.tag.OPEN { background:#10331c; color:#3fb950; } .tag.RESOLVED { background:#2a2f37; color:#8b949e; }
.foot { color:#6e7681; font-size:11px; margin-top:18px; }
.section { margin-top:30px; }
"""


def _render_tracker(s: dict) -> str:
    summ = s.get("summary", {})
    name = s.get("name", "?")
    live = '<span class="live">LIVE</span>' if s.get("live") else "paper (dry-run)"
    def cardcls(x):
        return "green" if _cls(x) == "pos" else ("red" if _cls(x) == "neg" else "")
    tp = summ.get("total_pnl", 0)
    today = summ.get("today_pnl", 0)
    real = summ.get("realized_pnl", 0)
    unreal = summ.get("unrealized_pnl", 0)
    cards = [
        ("Total P&L", _money(tp), cardcls(tp)),
        ("Today's P&L", _money(today), cardcls(today)),
        ("Realized · resolved", _money(real), cardcls(real)),
        ("Unrealized · open", _money(unreal), cardcls(unreal)),
        ("Total Spent", _money(summ.get("total_cost", 0)), ""),
        ("Portfolio Value · open", _money(summ.get("portfolio_value", 0)), ""),
        ("Open / Resolved", f"{summ.get('open_count',0)} / {summ.get('resolved_count',0)}", ""),
    ]
    card_html = "".join(
        f'<div class="card {c}"><div class="label">{l}</div><div class="val">{v}</div></div>'
        for l, v, c in cards)

    daily = summ.get("daily", [])[-14:]
    daily_html = ""
    if daily:
        chips = "".join(
            f'<div class="day"><div class="d">{d[5:]}</div>'
            f'<div class="v {_cls(p)}">{_money(p)}</div></div>' for d, p in daily)
        daily_html = f'<div class="daily">{chips}</div>'

    rows = s.get("positions", [])
    body = []
    for r in rows[:200]:
        st = r.get("status", "")
        body.append(
            f'<tr><td class="l">{r.get("market_title","")}</td>'
            f'<td class="l">{r.get("outcome","")}</td>'
            f'<td><span class="tag {st}">{st}</span></td>'
            f'<td>{r.get("net_paper_size","")}</td>'
            f'<td>{r.get("avg_entry_price","")}</td>'
            f'<td>{r.get("current_price","")}</td>'
            f'<td>{_money(r.get("current_value",0))}</td>'
            f'<td class="{_cls(r.get("pnl",0))}">{_money(r.get("pnl",0))}</td></tr>')
    note = f" (showing first 200 of {len(rows)})" if len(rows) > 200 else ""

    return f"""
    <div class="section">
      <h1>@{name} <span class="sub">· {live}</span></h1>
      <div class="sub">Updated {summ.get('last_updated','')} · Scale {summ.get('scale_pct',1):g}%
        · Return {summ.get('total_pnl_pct',0):.2f}% · {summ.get('priced_count',0)} counted,
        {summ.get('hidden_count',0)} hidden &lt;$1{note}</div>
      <div class="cards">{card_html}</div>
      {daily_html}
      <table>
        <tr><th class="l">Market</th><th class="l">Outcome</th><th>Status</th><th>Size</th>
            <th>Avg Entry</th><th>Cur Price</th><th>Value</th><th>P&L</th></tr>
        {''.join(body)}
      </table>
    </div>"""


@app.route("/")
def index():
    if not _auth_ok():
        return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="dashboard"'})
    states = _load_states()
    if not states:
        inner = "<p>No data yet — waiting for the bot's first cycle.</p>"
    else:
        inner = "".join(_render_tracker(s) for s in states)
    return f"""<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <title>Polymarket Copytrader</title><style>{CSS}</style></head>
    <body><div class="wrap">
      <h1>Polymarket Paper Copytrader</h1>
      <div class="sub">auto-refreshes every 30s · read-only</div>
      {inner}
      <div class="foot">Paper trading unless a tracker shows LIVE. Not financial advice.</div>
    </div></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("DASHBOARD_PORT", "8080")))
