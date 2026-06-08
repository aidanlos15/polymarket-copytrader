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


def _live_path(name: str) -> str:
    return os.path.join(DATA_DIR, f"live_{name}.txt")


def _read_live(name: str) -> bool:
    try:
        return open(_live_path(name)).read().strip().lower() == "on"
    except OSError:
        return False


def _write_live(name: str, on: bool) -> None:
    with open(_live_path(name), "w") as fh:
        fh.write("on" if on else "off")


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
.cards { display:grid; grid-template-columns:repeat(auto-fit, minmax(135px, 1fr)); gap:10px; margin-bottom:14px; }
.card.peak { background:#fff8c5; border-color:#d4a72c; }
.daily { display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-bottom:14px; }
.day { display:inline-block; background:#f6f8fa; border:1px solid #d0d7de; border-radius:6px; padding:4px 9px; text-align:center; text-decoration:none; color:inherit; cursor:pointer; }
.day:hover { border-color:#0969da; }
.day.active { border-color:#0969da; background:#ddf4ff; }
.day .d { color:#656d76; font-size:10px; }
.day .v { font-size:12px; font-weight:700; }
.banner { background:#ddf4ff; border:1px solid #0969da; border-radius:8px; padding:8px 12px; margin-bottom:12px; font-size:13px; }
.liveswitch { padding:7px 16px; border-radius:9px; border:1px solid #d0d7de; font:inherit; font-weight:700; font-size:13px; cursor:pointer; }
.liveswitch.off { background:#f6f8fa; color:#656d76; }
.liveswitch.on { background:#cf222e; color:#fff; border-color:#cf222e; }
.livewarn { background:#cf222e; color:#fff; padding:9px 13px; border-radius:8px; margin-bottom:12px; font-weight:700; }
.card { background:#f6f8fa; border:1px solid #d0d7de; border-radius:10px; padding:12px 14px; }
.card .label { color:#656d76; font-size:10px; letter-spacing:.04em; text-transform:uppercase; }
.card .val { font-size:20px; font-weight:700; margin-top:4px; }
.card.green { background:#dafbe1; border-color:#2da44e; }
.card.red { background:#ffebe9; border-color:#cf222e; }
.tablewrap { overflow-x:auto; -webkit-overflow-scrolling:touch; border-radius:10px; }
table { width:100%; border-collapse:collapse; background:#ffffff; border:1px solid #d0d7de; border-radius:10px; overflow:hidden; }
th,td { padding:7px 10px; text-align:right; border-bottom:1px solid #d8dee4; white-space:nowrap; }
th { background:#f6f8fa; color:#656d76; font-size:11px; text-transform:uppercase; position:sticky; top:0; }
td.l,th.l { text-align:left; }
tr:hover td { background:#f6f8fa; }
.pos { color:#1a7f37; } .neg { color:#cf222e; }
.tag { font-size:10px; padding:1px 7px; border-radius:10px; }
.tag.OPEN { background:#dafbe1; color:#1a7f37; } .tag.RESOLVED { background:#eaeef2; color:#656d76; }
.foot { color:#8c959f; font-size:11px; margin-top:18px; }

/* --- Mobile (iPhone-width and below): reflow so it fits neatly --- */
@media (max-width: 480px) {
  .wrap { padding:12px; }
  .topbar { flex-direction:column; align-items:flex-start; gap:8px; }
  .topbar h1 { font-size:16px; }
  .toggle a { padding:8px 22px; }              /* big tap targets */
  .cards { grid-template-columns:repeat(2,1fr); gap:8px; }
  .card { padding:10px 11px; }
  .card .val { font-size:16px; }
  .controls { gap:8px; }
  .hint { display:none; }                       /* hide long hint to save space */
  .daily { gap:5px; }
  table { font-size:12px; }
  th, td { padding:6px 8px; }
  .banner { font-size:12px; }
}
"""


def _render_tracker(s: dict, scale_val: int, date_filter: str = "") -> str:
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
        ("Peak Open · cash to fund", _money(summ.get("peak_open_value", 0)), "peak"),
        ("Open / Resolved", f"{summ.get('open_count',0)} / {summ.get('resolved_count',0)}", ""),
    ]
    card_html = "".join(
        f'<div class="card {c}"><div class="label">{l}</div><div class="val">{v}</div></div>'
        for l, v, c in cards)

    daily = summ.get("daily", [])[:14]
    daily_html = ""
    if daily:
        allcls = "" if date_filter else " active"
        chips = f'<a class="day{allcls}" href="/?t={name}"><div class="d">ALL</div><div class="v">&mdash;</div></a>'
        chips += "".join(
            f'<a class="day{" active" if d == date_filter else ""}" href="/?t={name}&date={d}">'
            f'<div class="d">{d[5:]}</div><div class="v {_cls(p)}">{_money(p)}</div></a>'
            for d, p in daily)
        daily_html = f'<div class="daily"><span class="sub">Daily realized:</span> {chips}</div>'

    rows = s.get("positions", [])
    if date_filter:
        rows = [r for r in rows if r.get("trade_date") == date_filter]
    banner = ""
    if date_filter:
        banner = (f'<div class="banner">Showing positions opened on <b>{date_filter}</b> '
                  f'({len(rows)}) · <a href="/?t={name}">show all</a></div>')
    body = []
    for r in rows[:200]:
        st = r.get("status", "")
        body.append(
            f'<tr><td class="l">{r.get("market_title","")}</td>'
            f'<td class="l">{r.get("outcome","")}</td>'
            f'<td><span class="tag {st}">{st}</span></td>'
            f'<td class="l">{r.get("trade_time","")}</td>'
            f'<td>{r.get("net_paper_size","")}</td>'
            f'<td>{r.get("avg_entry_price","")}</td>'
            f'<td>{r.get("whale_entry","")}</td>'
            f'<td>{r.get("current_price","")}</td>'
            f'<td>{_money(r.get("current_value",0))}</td>'
            f'<td class="{_cls(r.get("pnl",0))}">{_money(r.get("pnl",0))}</td>'
            f'<td>{r.get("avg_lag_s","")}</td></tr>')
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
      {banner}
      <div class="tablewrap"><table>
        <tr><th class="l">Market</th><th class="l">Outcome</th><th>Status</th><th class="l">Opened</th><th>Size</th>
            <th>Our Entry</th><th>Whale Entry</th><th>Cur Price</th><th>Value</th><th>P&L</th>
            <th>Lag s</th></tr>
        {''.join(body)}
      </table></div>"""


@app.route("/live", methods=["POST"])
def set_live():
    if not _auth_ok():
        return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="dashboard"'})
    name = request.form.get("tracker", "").strip()
    if name:
        _write_live(name, request.form.get("on", "0") == "1")
    return redirect(f"/?t={name}")


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
        inner, toggle, livebtn, livewarn = "<p>No data yet — waiting for the bot's first cycle.</p>", "", "", ""
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
        date_filter = request.args.get("date", "")
        inner = _render_tracker(s, scale_val, date_filter)
        # Live/paper toggle (top-right). Turning it ON asks for confirmation.
        live = bool(s.get("live"))
        livebtn = (f'<form method="post" action="/live" onsubmit="return confirmLive(this)" '
                   f'style="margin-left:auto">'
                   f'<input type="hidden" name="tracker" value="{active}">'
                   f'<input type="hidden" name="on" value="{0 if live else 1}">'
                   f'<button class="liveswitch {"on" if live else "off"}">'
                   f'{"● LIVE" if live else "○ PAPER"}</button></form>')
        livewarn = (f'<div class="livewarn">⚠️ LIVE TRADING ACTIVE for @{active} — real '
                    f'orders are being placed for new copied trades.</div>') if live else ""

    return f"""<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Polymarket Copytrader</title><style>{CSS}</style>
    <script>
      // Auto-refresh every 30s, but never while you're typing in the scale box.
      setInterval(function() {{
        var el = document.getElementById('scaleinput');
        if (!el || document.activeElement !== el) location.reload();
      }}, 30000);
      function confirmLive(f) {{
        if (f.on.value === '1') return confirm(
          '⚠️ Switch to LIVE TRADING?\\n\\nReal orders with REAL MONEY will be placed for new copied trades. Only do this if you have funded the account and configured a key.');
        return true;
      }}
    </script></head>
    <body><div class="wrap">
      <div class="topbar">
        <h1 style="margin:0">Polymarket Copytrader</h1>
        {toggle}
        {livebtn}
      </div>
      <div class="sub">switch trader above · toggle live/paper top-right · auto-refreshes every 30s</div>
      {livewarn}
      {inner}
      <div class="foot">Paper trading unless a tracker shows LIVE. Not financial advice.</div>
    </div></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("DASHBOARD_PORT", "8080")))
