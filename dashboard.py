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
from pathlib import Path

from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

import bot
import config

OUTPUT = bot.BASE_DIR / "dashboard.html"

# Shown under the ticker in the overview, the way the Stocks app does.
# Anything not listed just shows the ticker, which is no loss.
NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA",
    "JPM": "JPMorgan Chase", "XOM": "Exxon Mobil", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla", "KO": "Coca-Cola",
    "JNJ": "Johnson & Johnson", "WMT": "Walmart", "DIS": "Disney",
    "NFLX": "Netflix", "V": "Visa", "PG": "Procter & Gamble",
}
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
    out: dict = {"ok": False, "error": None, "positions": {}, "orders": []}
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

        out["positions"] = {}
        for symbol in bot.symbols():
            try:
                p = client.get_open_position(symbol)
                out["positions"][symbol] = {
                    "qty": float(p.qty),
                    "avg": float(p.avg_entry_price),
                    "value": float(p.market_value),
                    "pl": float(p.unrealized_pl),
                }
            except APIError:
                # Alpaca answers 404 for a symbol with no open position.
                out["positions"][symbol] = None

        orders = client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=10)
        )
        still_working = {"new", "accepted", "pending_new", "held",
                         "partially_filled", "accepted_for_bidding"}
        out["orders"] = [
            {
                "submitted": o.submitted_at.astimezone(bot.MARKET_TZ)
                              .strftime("%Y-%m-%d %H:%M"),
                "symbol": o.symbol,
                "side": o.side.value.upper(),
                "qty": float(o.qty or 0),
                "status": o.status.value,
                "filled": float(o.filled_avg_price) if o.filled_avg_price else None,
                "open": o.status.value in still_working,
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


def gather_quote(symbol: str) -> dict | None:
    """The current state of the stock itself: last trade, spread, day so far.

    Alpaca's free data is the IEX feed, so the last price can differ a little
    from the consolidated tape you see on a brokerage site. Close enough to
    watch a position, not a substitute for a real-time terminal.
    """
    try:
        client = StockHistoricalDataClient(*bot.credentials())
        snap = client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbol)
        )[symbol]

        last = float(snap.latest_trade.price)
        prev = float(snap.previous_daily_bar.close)
        day = snap.daily_bar
        quote = snap.latest_quote

        return {
            "last": last,
            "at": snap.latest_trade.timestamp.astimezone(bot.MARKET_TZ),
            "prev_close": prev,
            "change": last - prev,
            "change_pct": (last - prev) / prev * 100 if prev else 0.0,
            "open": float(day.open),
            "high": float(day.high),
            "low": float(day.low),
            "volume": int(day.volume),
            "bid": float(quote.bid_price),
            "ask": float(quote.ask_price),
        }
    except Exception:
        # The page is still worth showing without a quote.
        return None


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
        if state and state.group(1).strip().lower() != "enabled":
            # The Windows task is off on purpose - GitHub Actions runs the
            # bot now. Reporting a next run for a disabled task would be a
            # lie, and naming the cloud schedule here would be guesswork.
            return None
        return nxt.group(1).strip()
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

def build_chart(data, crossovers, symbol: str) -> tuple[str, list[dict]]:
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
         role="img" aria-label="{symbol} close with the
         {config.SHORT_WINDOW} and {config.LONG_WINDOW} day moving averages">
      {''.join(grid)}
      <polyline class="line s-close" points="{path(closes)}"/>
      <polyline class="line s-long"  points="{path(long_)}"/>
      <polyline class="line s-short" points="{path(short)}"/>
      {''.join(marks)}
      {''.join(labels)}
      <line class="crosshair" x1="0" y1="{MT}" x2="0" y2="{H - MB}"/>
      <circle class="dot dot-close" r="4"/>
      <circle class="dot dot-short" r="4"/>
      <circle class="dot dot-long"  r="4"/>
      <rect class="hit" x="{ML}" y="{MT}" width="{W - ML - MR}"
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


# The sparkline and the change beside it must cover the same stretch of
# time, or a row can show a rising line next to a falling number. The Stocks
# app keeps them in step because both follow its selected range; this has no
# range picker, so one window serves both.
SPARK_DAYS = 40


def window_change(data, days: int = SPARK_DAYS) -> tuple[float, float]:
    """Move across the sparkline's own window, in dollars and percent."""
    closes = [float(c) for c in data["Close"].tolist()[-days:]]
    if len(closes) < 2:
        return 0.0, 0.0
    last, first = closes[-1], closes[0]
    return last - first, ((last - first) / first * 100) if first else 0.0


def sparkline(data, days: int = SPARK_DAYS, w: int = 112, h: int = 34) -> str:
    """A small line of the recent closes, as the Stocks app shows per row."""
    closes = [float(c) for c in data["Close"].tolist()[-days:]]
    if len(closes) < 2:
        return ""
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0
    step = w / (len(closes) - 1)
    pts = " ".join(
        f"{i * step:.1f},{h - 2 - (c - lo) / span * (h - 4):.1f}"
        for i, c in enumerate(closes)
    )
    rising = closes[-1] >= closes[0]
    cls = "spark-up" if rising else "spark-down"
    return (f'<svg class="spark {cls}" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline points="{pts}"/></svg>')


def change_pill(change: float, pct: float) -> str:
    """The rounded badge. The sign and number carry the meaning; the colour
    only reinforces it, so it still reads without colour vision."""
    tone = "up" if change >= 0 else "down"
    sign = "+" if change >= 0 else "-"
    return (f'<span class="pill {tone}">{sign}{abs(pct):.2f}%</span>')


def signal_wording(signal, detail) -> tuple[str, str, str]:
    """The headline value, tone and explanation for one symbol's signal."""
    if signal == bot.BUY:
        return ("BUY", "good",
                f"{config.SHORT_WINDOW}-day crossed above the "
                f"{config.LONG_WINDOW}-day")
    if signal == bot.SELL:
        return ("SELL", "warn",
                f"{config.SHORT_WINDOW}-day crossed below the "
                f"{config.LONG_WINDOW}-day")
    side = "above" if detail["current_gap"] > 0 else "below"
    return ("No signal", "",
            f"fast line sitting {side} the slow one, no crossing")


def live_panel(symbol, account, quote) -> str:
    """Holding, working order and the stock's own state. Empty when flat."""
    if not account["ok"]:
        return ""

    pos = account["positions"].get(symbol)
    pending = [o for o in account.get("orders", [])
               if o["open"] and o["symbol"] == symbol]
    if not pos and not pending:
        return ""

    rows = []
    for o in pending:
        rows.append(
            f'<div class="live-row"><span class="live-k">Pending</span>'
            f'<span class="live-v {"good" if o["side"] == "BUY" else "warn"}">'
            f'{o["side"]} {o["qty"]:g} {o["symbol"]}</span>'
            f'<span class="live-note">placed {o["submitted"]}, '
            f'{o["status"]} - fills at the next open</span></div>'
        )
    if pos:
        pl = pos["pl"]
        rows.append(
            f'<div class="live-row"><span class="live-k">Holding</span>'
            f'<span class="live-v">{pos["qty"]:g} {symbol}</span>'
            f'<span class="live-note">bought at {money(pos["avg"])}, '
            f'now worth {money(pos["value"])}</span></div>'
        )
        rows.append(
            f'<div class="live-row"><span class="live-k">Profit</span>'
            f'<span class="live-v {"good" if pl >= 0 else "warn"}">'
            f'{"+" if pl >= 0 else ""}{money(pl)}</span>'
            f'<span class="live-note">unrealised, moves with the price</span></div>'
        )

    q = quote
    if q:
        arrow = "&#9650;" if q["change"] >= 0 else "&#9660;"
        tone = "good" if q["change"] >= 0 else "warn"
        rows.append(
            f'<div class="live-row"><span class="live-k">Last price</span>'
            f'<span class="live-v">{money(q["last"])}</span>'
            f'<span class="live-note {tone}">{arrow} {money(abs(q["change"]))} '
            f'({q["change_pct"]:+.2f}%) since yesterday\'s close</span></div>'
        )
        rows.append(
            f'<div class="live-row"><span class="live-k">Today</span>'
            f'<span class="live-v">{money(q["low"])} &ndash; {money(q["high"])}</span>'
            f'<span class="live-note">opened {money(q["open"])}, '
            f'{q["volume"]:,} shares traded</span></div>'
        )
        # Only while trading. Outside hours the book is stale and the spread
        # goes absurdly wide - $326/$351 on a $344 stock - which looks like a
        # bug rather than the nothing it is.
        if account.get("market_open"):
            rows.append(
                f'<div class="live-row"><span class="live-k">Bid / ask</span>'
                f'<span class="live-v">{money(q["bid"])} / {money(q["ask"])}</span>'
                f'<span class="live-note">what you could sell and buy at '
                f'right now</span></div>'
            )

    source = (f"Prices from Alpaca's free IEX feed, read at "
              f"{q['at']:%H:%M:%S %Z} when this page was built. They can "
              f"differ slightly from a brokerage site, which uses the full "
              f"consolidated tape." if q else "")

    return f"""
  <div class="card">
    <div class="live">{''.join(rows)}</div>
    <p class="live-source">{source}
      <a href="https://app.alpaca.markets/trade/{symbol}"
         target="_blank" rel="noopener">Open the live chart &rarr;</a></p>
  </div>
"""


def crossover_table(crossovers) -> str:
    if not crossovers:
        return '<p class="empty">No crossovers in this window.</p>'
    rows = "".join(
        f"<tr><td>{c['date']}</td>"
        f"<td class=\"{'good' if c['side'] == bot.BUY else 'warn'}\">{c['side']}</td>"
        f"<td class=\"num\">{money(c['close'])}</td></tr>"
        for c in reversed(crossovers)
    )
    return ("<table><thead><tr><th>Date</th><th>Signal</th>"
            f"<th class='num'>Close</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def symbol_section(block, account, public, uid) -> tuple[str, list]:
    """Everything the page says about one symbol. Returns (html, points)."""
    symbol = block["symbol"]
    detail = block["detail"]
    svg, points = build_chart(block["data"], block["crossovers"], symbol)

    value, tone, note = signal_wording(block["signal"], detail)
    tiles = [
        tile("Signal", value, note, tone),
        tile("Last close", money(detail["close"]),
             f"session of {detail['current_date']}"),
        tile(f"{config.SHORT_WINDOW}-day average", money(detail["sma_short"]),
             "the fast line"),
        tile(f"{config.LONG_WINDOW}-day average", money(detail["sma_long"]),
             "the slow line"),
    ]

    recent = "".join(
        f"<tr><td>{p['d']}</td><td class='num'>{p['c']:,.2f}</td>"
        f"<td class='num'>{'-' if p['s'] is None else format(p['s'], ',.2f')}</td>"
        f"<td class='num'>{'-' if p['l'] is None else format(p['l'], ',.2f')}</td></tr>"
        for p in points[-30:][::-1]
    )

    live = "" if public else live_panel(symbol, account, block.get("quote"))

    change, pct = window_change(block["data"])
    html_out = f"""
  <section class="symbol" id="{symbol}">
    <div class="symbol-head">
      <div>
        <h2 class="symbol-name">{symbol}</h2>
        <div class="symbol-sub">{NAMES.get(symbol, 'Watched by the bot')}</div>
      </div>
      <div class="symbol-price">
        <div class="symbol-last">{money(detail['close'])}</div>
        {change_pill(change, pct)}
      </div>
    </div>
    <div class="tiles">{''.join(tiles)}</div>
    {live}
    <div class="card chart-wrap" data-uid="{uid}">
      {svg}
      <div class="tip"></div>
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
    <h3>Crossovers in this window</h3>
    <div class="card">{crossover_table(block['crossovers'])}</div>
  </section>
"""
    return html_out, points


def render(blocks, account, runs, next_run, public: bool = False) -> str:
    """Build the page. `public` strips everything about the account.

    The public page carries market data and the strategy's own reading of it,
    both of which anyone could work out from public prices. It carries no
    balance, no position, no orders, no account number and no run log.
    """
    sections, series, rows = [], {}, []
    for uid, block in enumerate(blocks):
        section_html, points = symbol_section(block, account, public, uid)
        sections.append(section_html)
        series[str(uid)] = points

        # The overview row, in the shape the Stocks app uses: name on the
        # left, a sparkline in the middle, price and change on the right.
        detail = block["detail"]
        change, pct = window_change(block["data"])
        value, tone, _ = signal_wording(block["signal"], detail)
        flag = (f'<span class="row-flag {tone}">{value}</span>'
                if tone else "")
        rows.append(
            f'<a class="row" href="#{block["symbol"]}">'
            f'  <span class="row-id">'
            f'    <span class="row-ticker">{block["symbol"]}</span>'
            f'    <span class="row-sub">{NAMES.get(block["symbol"], "")}{flag}</span>'
            f'  </span>'
            f'  {sparkline(block["data"])}'
            f'  <span class="row-right">'
            f'    <span class="row-price">{money(detail["close"])}</span>'
            f'    {change_pill(change, pct)}'
            f'  </span>'
            f'</a>'
        )
    series_js = series
    overview = (f'<div class="rows-head">Watchlist '
                f'<span>last {SPARK_DAYS} sessions</span></div>'
                f'<div class="rows">{"".join(rows)}</div>')

    # Account tiles sit once at the top, above the per-symbol sections.
    account_tiles = ""
    if not public:
        if account["ok"]:
            held = sum(1 for p in account["positions"].values() if p)
            account_tiles = "".join([
                tile("Account value", money(account["value"]),
                     f"{money(account['cash'])} cash"),
                tile("Buying power", money(account["buying_power"]),
                     "cash account, no margin"),
                tile("Holdings", f"{held} of {len(blocks)}",
                     "symbols currently held"),
            ])
        else:
            account_tiles = tile("Account", "Unavailable",
                                 "could not reach Alpaca", "warn")

    # Orders.
    if public:
        orders_html = ""
    elif account["ok"] and account["orders"]:
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

    # Recent runs.
    if public:
        runs_html = ""
    elif runs:
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

    market = ""
    if not public and account["ok"]:
        market = ("Market open" if account["market_open"]
                  else f"Market closed &middot; next open {account['next_open']}")

    # The two sections that describe the account, dropped entirely when the
    # page is going somewhere public.
    private_sections = "" if public else f"""
  <h2>Orders on the account</h2>
  <div class="card">{orders_html}</div>

  <h2>Recent scheduled runs</h2>
  <div class="card">{runs_html}</div>
"""

    footer = (
        "Market data and the strategy's reading of it. Nothing here describes "
        "any account, balance, holding or order."
        if public else
        "Rebuilt every weekday morning by the scheduled run, and whenever you "
        'run <span class="mono">python dashboard.py</span>. This page only '
        'reports; trades are placed by <span class="mono">bot.py</span>.'
    )

    banner = ""
    if not public and not account["ok"]:
        banner = (f'<div class="banner">Could not reach Alpaca, so the balance and '
                  f'orders are missing. The charts and signals below are still live.'
                  f'<br><span class="mono">{html.escape(account["error"] or "")}</span></div>')

    watched = ", ".join(b["symbol"] for b in blocks)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{watched} crossover bot</title>
<style>
  /* iOS system colours, so this reads like the Stocks app. Dark is the
     home ground; light is its own set of steps, not an inversion. */
  :root {{
    color-scheme: light;
    --surface-0:#f2f2f7; --surface-1:#ffffff; --border:#e3e3e8;
    --text-primary:#000000; --text-secondary:#3c3c43; --text-muted:#8e8e93;
    --series-close:#aeaeb2; --series-short:#007aff; --series-long:#ff9500;
    --good:#34c759; --warn:#ff3b30;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --surface-0:#000000; --surface-1:#1c1c1e; --border:#2c2c2e;
      --text-primary:#ffffff; --text-secondary:#ebebf5; --text-muted:#8e8e93;
      --series-close:#636366; --series-short:#0a84ff; --series-long:#ff9f0a;
      --good:#30d158; --warn:#ff453a;
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface-0:#000000; --surface-1:#1c1c1e; --border:#2c2c2e;
    --text-primary:#ffffff; --text-secondary:#ebebf5; --text-muted:#8e8e93;
    --series-close:#636366; --series-short:#0a84ff; --series-long:#ff9f0a;
    --good:#30d158; --warn:#ff453a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; padding:28px 16px 64px; background:var(--surface-0);
    color:var(--text-primary);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",
         ui-sans-serif,system-ui,"Segoe UI",sans-serif;
    -webkit-font-smoothing:antialiased;
  }}

  /* The overview list: one tappable row per symbol, as the Stocks app
     lays it out - identity left, sparkline middle, price and move right. */
  .rows-head {{ display:flex; justify-content:space-between;
                align-items:baseline; margin:22px 2px 8px;
                font-size:13px; font-weight:650;
                text-transform:uppercase; letter-spacing:0.06em;
                color:var(--text-muted); }}
  .rows-head span {{ text-transform:none; letter-spacing:0;
                     font-weight:400; }}
  .rows {{ background:var(--surface-1); border-radius:14px;
           overflow:hidden; }}
  .row {{ display:grid; grid-template-columns:1fr auto auto; gap:14px;
          align-items:center; padding:12px 16px; text-decoration:none;
          color:inherit; border-top:1px solid var(--border); }}
  .row:first-child {{ border-top:none; }}
  .row:hover {{ background:var(--surface-0); }}
  .row-id {{ display:flex; flex-direction:column; gap:2px; min-width:0; }}
  .row-ticker {{ font-size:19px; font-weight:650; letter-spacing:-0.02em; }}
  .row-sub {{ font-size:13px; color:var(--text-muted);
              display:flex; align-items:center; gap:7px;
              overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .row-flag {{ font-size:11px; font-weight:650; padding:1px 6px;
               border-radius:5px; letter-spacing:0.01em; }}
  .row-flag.good {{ background:var(--good); color:#fff; }}
  .row-flag.warn {{ background:var(--warn); color:#fff; }}
  .spark {{ width:112px; height:34px; }}
  .spark polyline {{ fill:none; stroke-width:1.8;
                     stroke-linejoin:round; stroke-linecap:round;
                     vector-effect:non-scaling-stroke; }}
  .spark-up polyline {{ stroke:var(--good); }}
  .spark-down polyline {{ stroke:var(--warn); }}
  .row-right {{ display:flex; flex-direction:column; align-items:flex-end;
                gap:4px; }}
  .row-price {{ font-size:17px; font-weight:600;
                font-variant-numeric:tabular-nums; }}
  /* The sign and the number carry the meaning. Colour only reinforces it,
     so the row still reads without colour vision. */
  .pill {{ display:inline-block; min-width:74px; text-align:center;
           padding:3px 9px; border-radius:7px; color:#fff;
           font-size:13.5px; font-weight:600;
           font-variant-numeric:tabular-nums; }}
  .pill.up {{ background:var(--good); }}
  .pill.down {{ background:var(--warn); }}

  .symbol-head {{ display:flex; align-items:flex-start;
                  justify-content:space-between; gap:16px;
                  margin:18px 0 14px; }}
  .symbol-sub {{ font-size:13.5px; color:var(--text-muted); margin-top:2px; }}
  .symbol-price {{ display:flex; flex-direction:column;
                   align-items:flex-end; gap:5px; }}
  .symbol-last {{ font-size:25px; font-weight:650; letter-spacing:-0.02em;
                  font-variant-numeric:tabular-nums; }}
  .wrap {{ max-width:1000px; margin:0 auto; }}
  header {{ display:flex; flex-wrap:wrap; gap:12px; align-items:baseline;
            justify-content:space-between; margin-bottom:4px; }}
  h1 {{ font-size:22px; margin:0; letter-spacing:-0.01em; }}
  h2 {{ font-size:15px; margin:34px 0 10px; letter-spacing:-0.01em; }}
  h3 {{ font-size:13px; margin:18px 0 8px; letter-spacing:-0.01em;
        color:var(--text-secondary); font-weight:600; }}
  /* Each symbol is its own band, so a page of several reads as a list
     rather than one long undifferentiated scroll. */
  .symbol {{ margin-top:40px; padding-top:8px;
             border-top:1px solid var(--border);
             scroll-margin-top:16px; }}
  .symbol-name {{ font-size:24px; margin:0; letter-spacing:-0.025em;
                  font-weight:680; }}
  .symbol .tiles {{ margin-top:0; }}
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
  .live {{ display:grid; gap:1px; background:var(--border);
           border:1px solid var(--border); border-radius:9px;
           overflow:hidden; }}
  .live-row {{ display:grid; gap:2px 14px; padding:10px 13px;
               background:var(--surface-1);
               grid-template-columns:92px minmax(0,auto) 1fr;
               align-items:baseline; }}
  .live-k {{ font-size:11.5px; color:var(--text-muted);
             text-transform:uppercase; letter-spacing:0.06em; }}
  .live-v {{ font-size:19px; letter-spacing:-0.01em;
             font-variant-numeric:tabular-nums; }}
  .live-v.good {{ color:var(--good); }}
  .live-v.warn {{ color:var(--warn); }}
  .live-note {{ font-size:12.5px; color:var(--text-secondary); }}
  .live-note.good {{ color:var(--good); }}
  .live-note.warn {{ color:var(--warn); }}
  .live-source {{ font-size:12px; color:var(--text-muted); margin:11px 0 0; }}
  .live-source a {{ color:var(--series-short); text-decoration:none;
                    white-space:nowrap; }}
  .live-source a:hover {{ text-decoration:underline; }}
  @media (max-width:560px) {{
    .live-row {{ grid-template-columns:1fr; }}
  }}
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
  .crosshair {{ stroke:var(--text-muted); stroke-width:1;
                stroke-dasharray:3 3; opacity:0; }}
  .dot {{ opacity:0; stroke:var(--surface-1); stroke-width:2; }}
  .dot-close {{ fill:var(--series-close); }}
  .dot-short {{ fill:var(--series-short); }}
  .dot-long  {{ fill:var(--series-long); }}
  .tip {{ position:absolute; pointer-events:none; opacity:0; z-index:5;
          background:var(--surface-1); border:1px solid var(--border);
          border-radius:9px; padding:9px 11px; font-size:12.5px;
          min-width:160px; box-shadow:0 6px 20px rgba(0,0,0,.14); }}
  .tip .d {{ color:var(--text-muted); margin-bottom:5px; }}
  .tip .r {{ display:flex; align-items:center; gap:7px;
             justify-content:space-between; }}
  .tip .n {{ display:flex; align-items:center; gap:7px;
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
      <h1>{watched} &middot; {config.SHORT_WINDOW}/{config.LONG_WINDOW} moving average crossover</h1>
      <div class="sub">
        Built {datetime.now(bot.MARKET_TZ):%d %b %Y, %H:%M %Z}
        {' &middot; ' + market if market else ''}
        {' &middot; next run ' + html.escape(next_run) if next_run else ''}
      </div>
    </div>
    <button id="theme">Dark / light</button>
  </header>

  {banner}

  {overview}

  <div class="tiles">{account_tiles}</div>

  {''.join(sections)}

  {private_sections}

  <footer>{footer}</footer>
</div>

<script>
// One entry per charted symbol, keyed by the wrapper's data-uid.
const SERIES = {json.dumps(series_js)};
const SHORT = {json.dumps(f"{config.SHORT_WINDOW}-day")};
const LONG  = {json.dumps(f"{config.LONG_WINDOW}-day")};

const row = (name, colour, value) =>
  value === null || value === undefined ? '' :
  `<div class="r"><span class="n"><i class="sw" style="background:${{colour}}"></i>${{name}}</span>` +
  `<strong>$${{value.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})}}</strong></div>`;

// Each chart wires up its own handlers against its own elements, so any
// number of them can sit on the page without colliding.
function wire(wrap) {{
  const pts = SERIES[wrap.dataset.uid];
  if (!pts) return;

  const svg  = wrap.querySelector('.chart');
  const hit  = wrap.querySelector('.hit');
  const tip  = wrap.querySelector('.tip');
  const hair = wrap.querySelector('.crosshair');
  const dots = {{
    c: wrap.querySelector('.dot-close'),
    s: wrap.querySelector('.dot-short'),
    l: wrap.querySelector('.dot-long'),
  }};

  function show(evt) {{
    const box = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    const ux = (evt.clientX - box.left) / box.width * vb.width;

    let best = 0, dist = Infinity;
    for (let i = 0; i < pts.length; i++) {{
      const d = Math.abs(pts[i].x - ux);
      if (d < dist) {{ dist = d; best = i; }}
    }}
    const p = pts[best];

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
  hit.addEventListener('touchmove', e => {{ show(e.touches[0]); }},
                       {{passive:true}});
}}

document.querySelectorAll('.chart-wrap').forEach(wire);

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

def build(public: bool = False, out=None) -> None:
    """Gather everything and write the page. Called by bot.py too.

    With `public`, the account is never even contacted, so there is nothing
    sensitive in memory to leak into the page by accident.
    """
    bot.validate_config()

    blocks = []
    for symbol in bot.symbols():
        data = bot.add_moving_averages(bot.fetch_history(symbol, CHART_DAYS))
        signal, detail = bot.detect_crossover(data)
        blocks.append({
            "symbol": symbol,
            "data": data,
            "signal": signal,
            "detail": detail,
            "crossovers": find_crossovers(data),
            "quote": None if public else gather_quote(symbol),
        })

    page = render(
        blocks,
        {"ok": False, "error": None, "positions": {}, "orders": []}
        if public else gather_account(),
        [] if public else gather_runs(),
        None if public else gather_schedule(),
        public=public,
    )

    target = Path(out) if out else OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", action="store_true",
                        help="open the dashboard in your browser once built")
    parser.add_argument("--public", action="store_true",
                        help="leave out everything about the account, for a "
                             "page that will be published")
    parser.add_argument("--out", default=None,
                        help="write somewhere other than dashboard.html")
    args = parser.parse_args()

    target = build(public=args.public, out=args.out)
    print(f"Wrote {target}" + (" (public, no account data)" if args.public else ""))

    if args.open:
        webbrowser.open(target.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
