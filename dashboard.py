"""
Build a standalone dashboard you can open in any browser.

Writes dashboard.html next to this file: one self-contained page with no
internet dependency, showing where the two averages are, every crossover in
the last year, your Alpaca balance and position, recent orders, and the last
few scheduled runs.

    python dashboard.py           # build it
    python dashboard.py --open    # build it and open it in your browser

It reads. It never places an order.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import webbrowser
from datetime import datetime

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

import bot
import config

OUTPUT = bot.BASE_DIR / "dashboard.html"
CHART_DAYS = 260          # about a year of trading days
LOG_RUNS = 8              # how many recent runs to list
TASK_NAME = "Tradebot MA Crossover"

# Chart geometry, in SVG user units.
W, H = 960, 380
ML, MR, MT, MB = 64, 76, 20, 34


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------

def gather_account() -> dict:
    """Read the account, position, orders and clock. Never raises."""
    out: dict = {"ok": False, "error": None}
    try:
        client = bot.connect()
        account = client.get_account()
        out.update(
            ok=True,
            number=account.account_number,
            paper=config.PAPER_TRADING,
            cash=float(account.cash),
            value=float(account.portfolio_value),
            buying_power=float(account.buying_power),
        )

        try:
            p = client.get_open_position(config.SYMBOL)
            out["position"] = {
                "qty": float(p.qty),
                "avg": float(p.avg_entry_price),
                "value": float(p.market_value),
                "pl": float(p.unrealized_pl),
            }
        except APIError:
            out["position"] = None

        orders = client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=10)
        )
        out["orders"] = [
            {
                "submitted": o.submitted_at.strftime("%Y-%m-%d %H:%M"),
                "symbol": o.symbol,
                "side": o.side.value.upper(),
                "qty": float(o.qty or 0),
                "status": o.status.value,
                "filled": float(o.filled_avg_price) if o.filled_avg_price else None,
            }
            for o in orders
        ]

        clock = client.get_clock()
        out["market_open"] = clock.is_open
        out["next_open"] = clock.next_open.strftime("%a %d %b, %H:%M %Z")
    except SystemExit as exc:
        out["error"] = str(exc)
    except Exception as exc:  # the dashboard is still worth showing
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def gather_runs() -> list[dict]:
    """Pull the last few run blocks out of the log."""
    if not bot.LOG_FILE.exists():
        return []

    text = bot.LOG_FILE.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"^=====\s(.+?)\s=====$", text, flags=re.MULTILINE)
    runs = []
    # parts is [preamble, stamp, body, stamp, body, ...]
    for stamp, body in zip(parts[1::2], parts[2::2]):
        signal = "unknown"
        for line in body.splitlines():
            if line.startswith("Signal"):
                signal = line.split(None, 1)[1].strip()
                break
        outcome = [ln.strip() for ln in body.splitlines() if ln.strip()]
        runs.append({
            "when": stamp,
            "signal": signal,
            "outcome": outcome[-1] if outcome else "",
        })
    return runs[-LOG_RUNS:][::-1]


def gather_schedule() -> str | None:
    """Ask Windows when the task next runs. Best effort."""
    try:
        raw = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        nxt = re.search(r"Next Run Time:\s*(.+)", raw)
        state = re.search(r"Scheduled Task State:\s*(.+)", raw)
        if not nxt:
            return None
        label = nxt.group(1).strip()
        if state and state.group(1).strip().lower() != "enabled":
            return f"{label} (task is disabled)"
        return label
    except Exception:
        return None


def find_crossovers(data) -> list[dict]:
    """Every crossover inside the charted window."""
    usable = data.dropna(subset=["sma_short", "sma_long"])
    gap = usable["sma_short"] - usable["sma_long"]
    found = []
    for i in range(1, len(gap)):
        before, after = gap.iloc[i - 1], gap.iloc[i]
        if before <= 0 < after:
            side = bot.BUY
        elif before >= 0 > after:
            side = bot.SELL
        else:
            continue
        stamp = usable.index[i]
        found.append({
            "date": stamp.date().isoformat(),
            "side": side,
            "index": data.index.get_loc(stamp),
            "close": float(usable["Close"].iloc[i]),
        })
    return found


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def build_chart(data, crossovers) -> tuple[str, list[dict]]:
    """Return the SVG body and the per-point series the tooltip reads."""
    closes = data["Close"].tolist()
    short = data["sma_short"].tolist()
    long_ = data["sma_long"].tolist()
    n = len(closes)

    values = [v for v in closes + short + long_ if v == v]  # drop NaN
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.06 or 1.0
    lo, hi = lo - pad, hi + pad

    def x(i: int) -> float:
        return ML + i * (W - ML - MR) / max(n - 1, 1)

    def y(v: float) -> float:
        return MT + (hi - v) * (H - MT - MB) / (hi - lo)

    def path(series) -> str:
        pts = [f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series) if v == v]
        return " ".join(pts)

    # Recessive horizontal grid with price labels.
    grid = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        gy = y(v)
        grid.append(
            f'<line class="grid" x1="{ML}" y1="{gy:.1f}" x2="{W - MR}" y2="{gy:.1f}"/>'
            f'<text class="tick" x="{ML - 10}" y="{gy + 4:.1f}" '
            f'text-anchor="end">${v:,.0f}</text>'
        )

    # Date ticks, five across the bottom.
    for k in range(5):
        i = round(k * (n - 1) / 4)
        label = data.index[i].strftime("%b %Y")
        grid.append(
            f'<text class="tick" x="{x(i):.1f}" y="{H - 10}" '
            f'text-anchor="middle">{label}</text>'
        )

    # Crossover markers: a ring in the surface colour keeps them legible
    # where they sit on top of the lines.
    marks = []
    for c in crossovers:
        cx, cy = x(c["index"]), y(c["close"])
        cls = "buy" if c["side"] == bot.BUY else "sell"
        marks.append(
            f'<circle class="mark-ring" cx="{cx:.1f}" cy="{cy:.1f}" r="6.5"/>'
            f'<circle class="mark {cls}" cx="{cx:.1f}" cy="{cy:.1f}" r="4.5">'
            f'<title>{c["side"]} signal on {c["date"]} at ${c["close"]:,.2f}</title>'
            f"</circle>"
        )

    # Direct labels at the right end, so the lines are named on the plot.
    labels = []
    for series, cls, name in (
        (short, "s-short", f"{config.SHORT_WINDOW}-day"),
        (long_, "s-long", f"{config.LONG_WINDOW}-day"),
    ):
        last = next((v for v in reversed(series) if v == v), None)
        if last is not None:
            labels.append(
                f'<text class="direct {cls}" x="{W - MR + 8}" '
                f'y="{y(last) + 4:.1f}">{name}</text>'
            )

    svg = f"""
    <svg viewBox="0 0 {W} {H}" class="chart"
         role="img" aria-label="{config.SYMBOL} close with the
         {config.SHORT_WINDOW} and {config.LONG_WINDOW} day moving averages">
      {''.join(grid)}
      <polyline class="line s-close" points="{path(closes)}"/>
      <polyline class="line s-long"  points="{path(long_)}"/>
      <polyline class="line s-short" points="{path(short)}"/>
      {''.join(marks)}
      {''.join(labels)}
      <line id="crosshair" x1="0" y1="{MT}" x2="0" y2="{H - MB}"/>
      <circle id="dot-close" r="4"/>
      <circle id="dot-short" r="4"/>
      <circle id="dot-long"  r="4"/>
      <rect id="hit" x="{ML}" y="{MT}" width="{W - ML - MR}"
            height="{H - MT - MB}" fill="transparent"/>
    </svg>
    """

    points = [
        {
            "d": data.index[i].strftime("%d %b %Y"),
            "x": round(x(i), 1),
            "c": round(closes[i], 2),
            "cy": round(y(closes[i]), 1),
            "s": None if short[i] != short[i] else round(short[i], 2),
            "sy": None if short[i] != short[i] else round(y(short[i]), 1),
            "l": None if long_[i] != long_[i] else round(long_[i], 2),
            "ly": None if long_[i] != long_[i] else round(y(long_[i]), 1),
        }
        for i in range(n)
    ]
    return svg, points


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def money(v: float | None) -> str:
    return "-" if v is None else f"${v:,.2f}"


def tile(label: str, value: str, note: str = "", tone: str = "") -> str:
    note_html = f'<div class="tile-note">{note}</div>' if note else ""
    return (
        f'<div class="tile {tone}"><div class="tile-label">{label}</div>'
        f'<div class="tile-value">{value}</div>{note_html}</div>'
    )


def render(data, signal, detail, crossovers, account, runs, next_run) -> str:
    svg, points = build_chart(data, crossovers)

    # Headline tiles.
    if signal == bot.BUY:
        sig_value, sig_tone = "BUY", "good"
        sig_note = f"{config.SHORT_WINDOW}-day crossed above the {config.LONG_WINDOW}-day"
    elif signal == bot.SELL:
        sig_value, sig_tone = "SELL", "warn"
        sig_note = f"{config.SHORT_WINDOW}-day crossed below the {config.LONG_WINDOW}-day"
    else:
        sig_value, sig_tone = "No signal", ""
        side = "above" if detail["current_gap"] > 0 else "below"
        sig_note = f"fast line sitting {side} the slow one, no crossing"

    tiles = [tile("Signal", sig_value, sig_note, sig_tone)]
    if account["ok"]:
        pos = account["position"]
        if pos:
            pl = pos["pl"]
            tiles.append(tile(
                f"{config.SYMBOL} position", f"{pos['qty']:g} share(s)",
                f"avg {money(pos['avg'])} &middot; {money(pl)} unrealised",
                "good" if pl >= 0 else "warn",
            ))
        else:
            tiles.append(tile(f"{config.SYMBOL} position", "None", "flat, all cash"))
        tiles.append(tile("Account value", money(account["value"]),
                          f"{money(account['cash'])} cash"))
        tiles.append(tile("Buying power", money(account["buying_power"]),
                          "cash account, no margin"))
    else:
        tiles.append(tile("Account", "Unavailable", "could not reach Alpaca", "warn"))

    # Orders.
    if account["ok"] and account["orders"]:
        rows = "".join(
            f"<tr><td>{o['submitted']}</td><td>{o['symbol']}</td>"
            f"<td class=\"{'good' if o['side'] == 'BUY' else 'warn'}\">{o['side']}</td>"
            f"<td class=\"num\">{o['qty']:g}</td><td>{o['status']}</td>"
            f"<td class=\"num\">{money(o['filled'])}</td></tr>"
            for o in account["orders"]
        )
        orders_html = (
            "<table><thead><tr><th>Submitted</th><th>Symbol</th><th>Side</th>"
            "<th class='num'>Qty</th><th>Status</th><th class='num'>Fill</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        orders_html = '<p class="empty">No orders yet. The bot has not traded.</p>'

    # Crossover history.
    if crossovers:
        rows = "".join(
            f"<tr><td>{c['date']}</td>"
            f"<td class=\"{'good' if c['side'] == bot.BUY else 'warn'}\">{c['side']}</td>"
            f"<td class=\"num\">{money(c['close'])}</td></tr>"
            for c in reversed(crossovers)
        )
        cross_html = ("<table><thead><tr><th>Date</th><th>Signal</th>"
                      f"<th class='num'>Close</th></tr></thead><tbody>{rows}</tbody></table>")
    else:
        cross_html = '<p class="empty">No crossovers in this window.</p>'

    # Recent runs.
    if runs:
        rows = "".join(
            f"<tr><td>{html.escape(r['when'])}</td>"
            f"<td>{html.escape(r['signal'])}</td>"
            f"<td>{html.escape(r['outcome'])}</td></tr>"
            for r in runs
        )
        runs_html = ("<table><thead><tr><th>Run</th><th>Signal</th><th>Outcome</th>"
                     f"</tr></thead><tbody>{rows}</tbody></table>")
    else:
        runs_html = '<p class="empty">No runs logged yet.</p>'

    # The table view the accessibility pass requires.
    recent = "".join(
        f"<tr><td>{p['d']}</td><td class='num'>{p['c']:,.2f}</td>"
        f"<td class='num'>{'-' if p['s'] is None else format(p['s'], ',.2f')}</td>"
        f"<td class='num'>{'-' if p['l'] is None else format(p['l'], ',.2f')}</td></tr>"
        for p in points[-30:][::-1]
    )

    market = ""
    if account["ok"]:
        market = ("Market open" if account["market_open"]
                  else f"Market closed &middot; next open {account['next_open']}")

    banner = ""
    if not account["ok"]:
        banner = (f'<div class="banner">Could not reach Alpaca, so the balance and '
                  f'orders are missing. The chart and signal below are still live.'
                  f'<br><span class="mono">{html.escape(account["error"] or "")}</span></div>')

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{config.SYMBOL} crossover bot</title>
<style>
  :root {{
    color-scheme: light;
    --surface-0:#f4f4f1; --surface-1:#fcfcfb; --border:#e2e1dc;
    --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#78776f;
    --series-close:#b9b8b1; --series-short:#2a78d6; --series-long:#eb6834;
    --good:#1baf7a; --warn:#e34948;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --surface-0:#121211; --surface-1:#1a1a19; --border:#33322f;
      --text-primary:#f0efec; --text-secondary:#c3c2b7; --text-muted:#8e8d84;
      --series-close:#55544e; --series-short:#3987e5; --series-long:#d95926;
      --good:#199e70; --warn:#e66767;
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface-0:#121211; --surface-1:#1a1a19; --border:#33322f;
    --text-primary:#f0efec; --text-secondary:#c3c2b7; --text-muted:#8e8d84;
    --series-close:#55544e; --series-short:#3987e5; --series-long:#d95926;
    --good:#199e70; --warn:#e66767;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; padding:32px 16px 64px; background:var(--surface-0);
    color:var(--text-primary);
    font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  header {{ display:flex; flex-wrap:wrap; gap:12px; align-items:baseline;
            justify-content:space-between; margin-bottom:4px; }}
  h1 {{ font-size:22px; margin:0; letter-spacing:-0.01em; }}
  h2 {{ font-size:15px; margin:34px 0 10px; letter-spacing:-0.01em; }}
  .sub {{ color:var(--text-secondary); font-size:13px; }}
  .mono {{ font-family:ui-monospace,"Cascadia Mono",Consolas,monospace; font-size:12px; }}
  button {{ font:inherit; font-size:13px; padding:5px 11px; cursor:pointer;
            color:var(--text-secondary); background:var(--surface-1);
            border:1px solid var(--border); border-radius:7px; }}
  .banner {{ margin:18px 0; padding:12px 14px; border-radius:9px;
             border:1px solid var(--border); background:var(--surface-1);
             color:var(--text-secondary); font-size:13px; }}
  .tiles {{ display:grid; gap:12px; margin-top:20px;
            grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }}
  .tile {{ background:var(--surface-1); border:1px solid var(--border);
           border-radius:11px; padding:14px 16px; }}
  .tile-label {{ font-size:12px; color:var(--text-muted);
                 text-transform:uppercase; letter-spacing:0.06em; }}
  .tile-value {{ font-size:25px; margin-top:5px; letter-spacing:-0.02em; }}
  .tile-note {{ font-size:12px; color:var(--text-secondary); margin-top:3px; }}
  .tile.good .tile-value {{ color:var(--good); }}
  .tile.warn .tile-value {{ color:var(--warn); }}
  .card {{ background:var(--surface-1); border:1px solid var(--border);
           border-radius:11px; padding:14px 16px; }}
  .chart-wrap {{ position:relative; }}
  /* Scale uniformly. Stretching the viewBox to a fixed height turns the
     signal markers into ellipses and squashes every label. */
  .chart {{ width:100%; height:auto; display:block; overflow:visible; }}
  .grid {{ stroke:var(--border); stroke-width:1; }}
  .tick {{ fill:var(--text-muted); font-size:11px; }}
  .line {{ fill:none; stroke-width:2; stroke-linejoin:round; stroke-linecap:round;
           vector-effect:non-scaling-stroke; }}
  .s-close {{ stroke:var(--series-close); }}
  .s-short {{ stroke:var(--series-short); }}
  .s-long  {{ stroke:var(--series-long); }}
  text.direct {{ font-size:11.5px; font-weight:600; }}
  text.direct.s-short {{ fill:var(--series-short); }}
  text.direct.s-long  {{ fill:var(--series-long); }}
  .mark-ring {{ fill:var(--surface-1); }}
  .mark.buy  {{ fill:var(--good); }}
  .mark.sell {{ fill:var(--warn); }}
  #crosshair {{ stroke:var(--text-muted); stroke-width:1;
                stroke-dasharray:3 3; opacity:0; }}
  #dot-close,#dot-short,#dot-long {{ opacity:0; stroke:var(--surface-1);
                                     stroke-width:2; }}
  #dot-close {{ fill:var(--series-close); }}
  #dot-short {{ fill:var(--series-short); }}
  #dot-long  {{ fill:var(--series-long); }}
  #tip {{ position:absolute; pointer-events:none; opacity:0; z-index:5;
          background:var(--surface-1); border:1px solid var(--border);
          border-radius:9px; padding:9px 11px; font-size:12.5px;
          min-width:160px; box-shadow:0 6px 20px rgba(0,0,0,.14); }}
  #tip .d {{ color:var(--text-muted); margin-bottom:5px; }}
  #tip .r {{ display:flex; align-items:center; gap:7px;
             justify-content:space-between; }}
  #tip .n {{ display:flex; align-items:center; gap:7px;
             color:var(--text-secondary); }}
  .sw {{ width:9px; height:9px; border-radius:2px; flex:none; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:16px; margin-top:10px;
             font-size:12.5px; color:var(--text-secondary); }}
  .legend span {{ display:flex; align-items:center; gap:7px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  th {{ text-align:left; font-weight:600; font-size:12px; color:var(--text-muted);
        text-transform:uppercase; letter-spacing:0.05em;
        padding:0 10px 7px 0; border-bottom:1px solid var(--border); }}
  td {{ padding:7px 10px 7px 0; border-bottom:1px solid var(--border);
        color:var(--text-secondary); }}
  td.good {{ color:var(--good); }}
  td.warn {{ color:var(--warn); }}
  .num, th.num {{ text-align:right; padding-right:0;
                  font-variant-numeric:tabular-nums; }}
  .empty {{ color:var(--text-muted); font-size:13.5px; margin:0; }}
  details {{ margin-top:12px; }}
  summary {{ cursor:pointer; color:var(--text-secondary); font-size:13px; }}
  footer {{ margin-top:34px; color:var(--text-muted); font-size:12px; }}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>{config.SYMBOL} &middot; {config.SHORT_WINDOW}/{config.LONG_WINDOW} moving average crossover</h1>
      <div class="sub">
        Built {datetime.now(bot.MARKET_TZ):%d %b %Y, %H:%M %Z}
        {' &middot; ' + market if market else ''}
        {' &middot; next run ' + html.escape(next_run) if next_run else ''}
      </div>
    </div>
    <button id="theme">Dark / light</button>
  </header>

  {banner}

  <div class="tiles">{''.join(tiles)}</div>

  <h2>Price and the two averages &middot; last {len(points)} trading days</h2>
  <div class="card chart-wrap">
    {svg}
    <div id="tip"></div>
    <div class="legend">
      <span><i class="sw" style="background:var(--series-close)"></i>Close</span>
      <span><i class="sw" style="background:var(--series-short)"></i>{config.SHORT_WINDOW}-day average</span>
      <span><i class="sw" style="background:var(--series-long)"></i>{config.LONG_WINDOW}-day average</span>
      <span><i class="sw" style="background:var(--good);border-radius:50%"></i>Buy signal</span>
      <span><i class="sw" style="background:var(--warn);border-radius:50%"></i>Sell signal</span>
    </div>
    <details>
      <summary>Show the last 30 days as a table</summary>
      <table style="margin-top:10px">
        <thead><tr><th>Date</th><th class="num">Close</th>
        <th class="num">{config.SHORT_WINDOW}-day</th>
        <th class="num">{config.LONG_WINDOW}-day</th></tr></thead>
        <tbody>{recent}</tbody>
      </table>
    </details>
  </div>

  <h2>Crossovers in this window</h2>
  <div class="card">{cross_html}</div>

  <h2>Orders on the account</h2>
  <div class="card">{orders_html}</div>

  <h2>Recent scheduled runs</h2>
  <div class="card">{runs_html}</div>

  <footer>
    Rebuilt every weekday morning by the scheduled task, and whenever you run
    <span class="mono">python dashboard.py</span>. This page only reports;
    trades are placed by <span class="mono">bot.py</span>.
  </footer>
</div>

<script>
const PTS = {json.dumps(points)};
const SHORT = {json.dumps(f"{config.SHORT_WINDOW}-day")};
const LONG  = {json.dumps(f"{config.LONG_WINDOW}-day")};

const svg = document.querySelector('.chart');
const hit = document.getElementById('hit');
const tip = document.getElementById('tip');
const hair = document.getElementById('crosshair');
const dots = {{
  c: document.getElementById('dot-close'),
  s: document.getElementById('dot-short'),
  l: document.getElementById('dot-long'),
}};
const row = (name, colour, value) =>
  value === null || value === undefined ? '' :
  `<div class="r"><span class="n"><i class="sw" style="background:${{colour}}"></i>${{name}}</span>` +
  `<strong>$${{value.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})}}</strong></div>`;

function toUser(evt) {{
  // Map the pointer into the SVG's own coordinate space, whatever the
  // rendered width happens to be.
  const box = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  return (evt.clientX - box.left) / box.width * vb.width;
}}

function show(evt) {{
  const ux = toUser(evt);
  let best = 0, dist = Infinity;
  for (let i = 0; i < PTS.length; i++) {{
    const d = Math.abs(PTS[i].x - ux);
    if (d < dist) {{ dist = d; best = i; }}
  }}
  const p = PTS[best];

  hair.setAttribute('x1', p.x); hair.setAttribute('x2', p.x);
  hair.style.opacity = 1;

  const place = (dot, y) => {{
    if (y === null) {{ dot.style.opacity = 0; return; }}
    dot.setAttribute('cx', p.x); dot.setAttribute('cy', y);
    dot.style.opacity = 1;
  }};
  place(dots.c, p.cy); place(dots.s, p.sy); place(dots.l, p.ly);

  tip.innerHTML = `<div class="d">${{p.d}}</div>` +
    row('Close', 'var(--series-close)', p.c) +
    row(SHORT, 'var(--series-short)', p.s) +
    row(LONG, 'var(--series-long)', p.l);

  const box = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  const px = p.x / vb.width * box.width;
  tip.style.opacity = 1;
  tip.style.left = Math.min(Math.max(px + 14, 0),
                            box.width - tip.offsetWidth - 4) + 'px';
  tip.style.top = '14px';
}}

function hide() {{
  tip.style.opacity = 0; hair.style.opacity = 0;
  Object.values(dots).forEach(d => d.style.opacity = 0);
}}

hit.addEventListener('mousemove', show);
hit.addEventListener('mouseleave', hide);
hit.addEventListener('touchmove', e => {{ show(e.touches[0]); }}, {{passive:true}});

document.getElementById('theme').addEventListener('click', () => {{
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const now = document.documentElement.getAttribute('data-theme')
           || (dark ? 'dark' : 'light');
  document.documentElement.setAttribute('data-theme',
    now === 'dark' ? 'light' : 'dark');
}});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build() -> None:
    """Gather everything and write dashboard.html. Called by bot.py too."""
    bot.validate_config()

    data = bot.add_moving_averages(bot.fetch_history(config.SYMBOL, CHART_DAYS))
    signal, detail = bot.detect_crossover(data)

    page = render(
        data, signal, detail,
        find_crossovers(data),
        gather_account(),
        gather_runs(),
        gather_schedule(),
    )
    OUTPUT.write_text(page, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true",
                        help="open the dashboard in your browser once built")
    args = parser.parse_args()

    build()
    print(f"Wrote {OUTPUT}")

    if args.open:
        webbrowser.open(OUTPUT.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
