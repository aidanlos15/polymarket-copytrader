"""Web dashboard for the copytrader bots.

Reads the state_<name>.json snapshots written each cycle by bot.py and renders one
tracker at a time (switchable via a toggle at the top). Lets you set the copy SCALE %
(1-100) per tracker — written to scale_<name>.txt, which the bot reads and uses to
re-scale ALL trades every cycle (only counting those worth >= $1 at that scale). Also a
manual Refresh button. Otherwise read-only — it never touches the bots' Excel or keys.

Env:  DASHBOARD_DATA_DIR  where the state_*.json + scale_*.txt files live
      DASHBOARD_PORT      port (default 8080)
      DASHBOARD_USER/PASS optional HTTP basic-auth (set both to require login)
"""
from __future__ import annotations

import glob
import json
import os

from flask import Flask, Response, redirect, request

DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
USER = os.environ.get("DASHBOARD_USER", "")
PW = os.environ.get("DASHBOARD_PASS", "")

app = Flask(__name__)


def _auth_ok() -> bool:
    if not USER:
        return True
    a = request.authorization
    return bool(a and a.username == USER and a.password == PW)


def _load_states() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted(glob.glob(os.path.join(DATA_DIR, "state_*.json"))):
        try:
            with open(p) as fh:
                s = json.load(fh)
            out[s.get("name", os.path.basename(p))] = s
        except (OSError, ValueError):
            continue
    return out


def _scale_path(name: str) -> str:
    return os.path.join(DATA_DIR, f"scale_{name}.txt")


def _read_scale(name: str) -> int | None:
    try:
        v = int(float(open(_scale_path(name)).read().strip()))
        return max(1, min(100, v))
    except (OSError, ValueError):
        return None


def _write_scale(name: str, v: int) -> None:
    with open(_scale_path(name), "w") as fh:
        fh.write(str(v))


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
body { margin:0; background:#ffffff; color:#1f2328; font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; }
.wrap { max-width:1200px; margin:0 auto; padding:24px; }
h1 { font-size:18px; margin:0 0 2px; }
.sub { color:#656d76; font-size:12px; margin-bottom:14px; }
.live { color:#bc4c00; font-weight:700; }
.topbar { display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin-bottom:6px; }
.toggle { display:inline-flex; border:1px solid #d0d7de; border-radius:9px; overflow:hidden; }
.toggle a { padding:7px 18px; text-decoration:none; color:#1f2328; background:#fff; font-weight:600; font-size:13px; }
.toggle a.active { background:#0969da; color:#fff; }
.controls { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:12px 0; }
.btn { padding:6px 12px; border:1px solid #d0d7de; border-radius:8px; background:#f6f8fa; cursor:pointer; font:inherit; font-size:13px; color:#1f2328; text-decoration:none; }
.btn:hover { background:#eaeef2; }
form.scale { display:inline-flex; gap:6px; align-items:center; margin:0; }
input[type=number] { width:64px; padding:5px 6px; border:1px solid #d0d7de; border-radius:6px; font:inherit; }
.hint { color:#8c959f; font-size:11px; }
.cards { display:grid; grid-template-columns:repeat(7,1fr); gap:10px; margin-bottom:14px; }
.daily { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:18px; }
.day { background:#f6f8fa; border:1px solid #d0d7de; border-radius:6px; padding:4px 9px; text-align:center; }
.day .d { color:#656d76; font-size:10px; }
.day .v { font-size:12px; font-weight:700; }
.card { background:#f6f8fa; border:1px solid #d0d7de; border-radius:10px; padding:12px 14px; }
.card .label { color:#656d76; font-size:10px; letter-spacing:.04em; text-transform:uppercase; }
.card .val { font-size:20px; font-weight:700; margin-top:4px; }
.card.green { background:#dafbe1; border-color:#2da44e; }
.card.red { background:#ffebe9; border-color:#cf222e; }
table { width:100%; border-collapse:collapse; background:#ffffff; border:1px solid #d0d7de; border-radius:10px; overflow:hidden; }
th,td { padding:7px 10px; text-align:right; border-bottom:1px solid #d8dee4; white-space:nowrap; }
th { background:#f6f8fa; color:#656d76; font-size:11px; text-transform:uppercase; position:sticky; top:0; }
td.l,th.l { text-align:left; }
tr:hover td { background:#f6f8fa; }
.pos { color:#1a7f37; } .neg { color:#cf222e; }
.tag { font-size:10px; padding:1px 7px; border-radius:10px; }
.tag.OPEN { background:#dafbe1; color:#1a7f37; } .tag.RESOLVED { background:#eaeef2; color:#656d76; }
.foot { color:#8c959f; font-size:11px; margin-top:18px; }
"""


def _render_tracker(s: dict, scale_val: int) -> str:
    summ = s.get("summary", {})
    name = s.get("name", "?")
    live = '<span class="live">LIVE</span>' if s.get("live") else "paper (dry-run)"

    def cardcls(x):
        return "green" if _cls(x) == "pos" else ("red" if _cls(x) == "neg" else "")
    tp = summ.get("total_pnl", 0)
    cards = [
        ("Total P&L", _money(tp), cardcls(tp)),
        ("Today's P&L", _money(summ.get("today_pnl", 0)), cardcls(summ.get("today_pnl", 0))),
        ("Realized · resolved", _money(summ.get("realized_pnl", 0)), cardcls(summ.get("realized_pnl", 0))),
        ("Unrealized · open", _money(summ.get("unrealized_pnl", 0)), cardcls(summ.get("unrealized_pnl", 0))),
        ("Total Spent", _money(summ.get("total_cost", 0)), ""),
        ("Portfolio Value · open", _money(summ.get("portfolio_value", 0)), ""),
        ("Open / Resolved", f"{summ.get('open_count',0)} / {summ.get('resolved_count',0)}", ""),
    ]
    card_html = "".join(
        f'<div class="card {c}"><div class="label">{l}</div><div class="val">{v}</div></div>'
        for l, v, c in cards)

    daily = summ.get("daily", [])[:14]
    daily_html = ""
    if daily:
        chips = "".join(
            f'<div class="day"><div class="d">{d[5:]}</div>'
            f'<div class="v {_cls(p)}">{_money(p)}</div></div>' for d, p in daily)
        daily_html = f'<div class="daily"><span class="sub">Daily realized:</span> {chips}</div>'

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

    controls = f"""
    <div class="controls">
      <form class="scale" method="post" action="/scale">
        <input type="hidden" name="tracker" value="{name}">
        <label>Scale %: <input id="scaleinput" type="number" name="scale" min="1" max="100" value="{scale_val}"></label>
        <button class="btn">Apply</button>
      </form>
      <button class="btn" onclick="location.reload()">&#8635; Refresh</button>
      <span class="hint">Scale recomputes ALL trades (only those &ge; $1 at that scale); applies within ~1 min.</span>
    </div>"""

    return f"""
      <h1>@{name} <span class="sub">· {live}</span></h1>
      <div class="sub">Updated {summ.get('last_updated','')} · Scale {summ.get('scale_pct',100):g}%
        · Return {summ.get('total_pnl_pct',0):.2f}% · {summ.get('priced_count',0)} counted,
        {summ.get('hidden_count',0)} hidden &lt;$1{note}</div>
      {controls}
      <div class="cards">{card_html}</div>
      {daily_html}
      <table>
        <tr><th class="l">Market</th><th class="l">Outcome</th><th>Status</th><th>Size</th>
            <th>Avg Entry</th><th>Cur Price</th><th>Value</th><th>P&L</th></tr>
        {''.join(body)}
      </table>"""


@app.route("/scale", methods=["POST"])
def set_scale():
    if not _auth_ok():
        return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="dashboard"'})
    name = request.form.get("tracker", "").strip()
    try:
        v = max(1, min(100, int(float(request.form.get("scale", "100")))))
    except ValueError:
        v = 100
    if name:
        _write_scale(name, v)
    return redirect(f"/?t={name}")


@app.route("/")
def index():
    if not _auth_ok():
        return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="dashboard"'})
    states = _load_states()
    if not states:
        inner, toggle = "<p>No data yet — waiting for the bot's first cycle.</p>", ""
    else:
        names = list(states)
        active = request.args.get("t") or names[0]
        if active not in states:
            active = names[0]
        toggle = '<div class="toggle">' + "".join(
            f'<a href="/?t={n}" class="{"active" if n == active else ""}">@{n}</a>' for n in names
        ) + "</div>"
        s = states[active]
        scale_val = _read_scale(active)
        if scale_val is None:
            scale_val = int(s.get("summary", {}).get("scale_pct", 100) or 100)
        inner = _render_tracker(s, scale_val)

    return f"""<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Polymarket Copytrader</title><style>{CSS}</style>
    <script>
      // Auto-refresh every 30s, but never while you're typing in the scale box.
      setInterval(function() {{
        var el = document.getElementById('scaleinput');
        if (!el || document.activeElement !== el) location.reload();
      }}, 30000);
    </script></head>
    <body><div class="wrap">
      <div class="topbar">
        <h1 style="margin:0">Polymarket Paper Copytrader</h1>
        {toggle}
      </div>
      <div class="sub">switch trader above · auto-refreshes every 30s</div>
      {inner}
      <div class="foot">Paper trading unless a tracker shows LIVE. Not financial advice.</div>
    </div></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("DASHBOARD_PORT", "8080")))
