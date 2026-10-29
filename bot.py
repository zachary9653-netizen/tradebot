"""
Moving average crossover trading bot.

Pulls daily prices from Yahoo Finance, computes a fast and a slow simple
moving average, and looks for a genuine crossover between the two most recent
trading days. A fast line crossing above the slow line is a buy signal; the
fast line crossing below it is a sell signal. Anything else is no signal at
all - a fast line that is merely above the slow one is not a crossover.

When a crossover fires, the bot sends a market order to Alpaca.

    python bot.py             # check for a signal and trade on it
    python bot.py --dry-run   # check for a signal, print it, trade nothing
    python bot.py --status    # show the account, position and current averages
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from dotenv import load_dotenv

import config

# yfinance still calls a few pandas methods that pandas 3 has deprecated, and
# the warnings drown out our own output. They are the library's to fix, not
# ours. This has to run after `import yfinance`, because yfinance installs a
# filter of its own that re-enables them and the first matching filter wins.
warnings.filterwarnings("ignore", module=r".*yfinance.*")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "logs" / "bot.log"

BUY = "BUY"
SELL = "SELL"

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def start_logging() -> None:
    """Send everything this run prints to logs/bot.log instead of the console.

    The scheduled task runs the bot with pythonw, which has no console at all,
    so without this its output would go nowhere.
    """
    LOG_FILE.parent.mkdir(exist_ok=True)
    stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def timing_note(now: datetime | None = None,
                scheduled: bool | None = None) -> str:
    """Say where this run sits relative to the opening bell.

    A scheduled run is meant to land shortly before the open. GitHub's
    scheduler is best-effort and has been hours late, so the log says plainly
    how late the run actually was rather than leaving it to be worked out
    from a timestamp. Only a run the scheduler started gets blamed on the
    scheduler - running it yourself at 7pm is not a fault.
    """
    now = now or datetime.now(MARKET_TZ)
    if scheduled is None:
        scheduled = os.getenv("GITHUB_EVENT_NAME") == "schedule"
    bell = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
                       second=0, microsecond=0)
    minutes = int((now - bell).total_seconds() // 60)

    if minutes < 0:
        return f"{-minutes} min before the open. Any order will queue for it."
    if minutes == 0:
        return "right at the open."
    if minutes <= 20:
        return f"{minutes} min after the open."

    hours, mins = divmod(minutes, 60)
    late = f"{hours}h {mins:02d}m" if hours else f"{mins} min"
    if not scheduled:
        return f"{late} after the open."
    return (f"{late} after the open - the scheduler was late. The signal is "
            f"unaffected, but any fill is at the price now, not at the open.")


def symbols() -> list[str]:
    """The symbols to trade, tidied and de-duplicated."""
    seen, out = set(), []
    for raw in config.SYMBOLS:
        s = raw.strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def validate_config() -> None:
    """Stop early on settings that can never produce a sane signal."""
    if not symbols():
        raise SystemExit("config error: SYMBOLS is empty. Add at least one "
                         "ticker, for example SYMBOLS = [\"AAPL\"].")
    if config.SHORT_WINDOW >= config.LONG_WINDOW:
        raise SystemExit(
            f"config error: SHORT_WINDOW ({config.SHORT_WINDOW}) must be smaller "
            f"than LONG_WINDOW ({config.LONG_WINDOW})."
        )
    if config.LOOKBACK_DAYS <= config.LONG_WINDOW:
        raise SystemExit(
            f"config error: LOOKBACK_DAYS ({config.LOOKBACK_DAYS}) must be larger "
            f"than LONG_WINDOW ({config.LONG_WINDOW}), otherwise the slow average "
            f"never has enough data to compare against."
        )
    if config.TRADE_QTY <= 0:
        raise SystemExit(f"config error: TRADE_QTY ({config.TRADE_QTY}) must be positive.")


def credentials() -> tuple[str, str]:
    """The Alpaca key pair, from .env locally or the environment in the cloud."""
    load_dotenv(BASE_DIR / ".env")
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()

    if not api_key or not secret_key:
        raise SystemExit(
            "No Alpaca credentials found.\n"
            f"Open {BASE_DIR / '.env'} and fill in ALPACA_API_KEY and "
            "ALPACA_SECRET_KEY.\n"
            "You can create a key pair at https://app.alpaca.markets "
            "under Home -> API Keys."
        )

    return api_key, secret_key


def connect() -> TradingClient:
    """Build an Alpaca trading client."""
    api_key, secret_key = credentials()
    return TradingClient(api_key, secret_key, paper=config.PAPER_TRADING)


# ---------------------------------------------------------------------------
# Market data and strategy
# ---------------------------------------------------------------------------

def fetch_history(symbol: str, trading_days: int) -> pd.DataFrame:
    """Return the last `trading_days` daily bars for `symbol`.

    Yahoo only returns bars for days the market was open, so we ask for a
    wider calendar window - weekends and holidays eat roughly a third of it -
    and keep the tail.
    """
    calendar_days = int(trading_days * 1.6) + 15
    history = yf.Ticker(symbol).history(
        start=date.today() - timedelta(days=calendar_days),
        end=date.today() + timedelta(days=1),
        interval="1d",
        auto_adjust=True,
    )

    if history.empty:
        raise SystemExit(
            f"Yahoo Finance returned no data for '{symbol}'. "
            "Check the symbol in config.py."
        )

    history = drop_unfinished_session(history)
    history = history.tail(trading_days)

    if len(history) < config.LONG_WINDOW + 1:
        raise SystemExit(
            f"Only {len(history)} trading days available for {symbol}, but a "
            f"{config.LONG_WINDOW}-day average needs at least "
            f"{config.LONG_WINDOW + 1} to detect a crossover."
        )

    return history


def drop_unfinished_session(
    history: pd.DataFrame, now: datetime | None = None
) -> pd.DataFrame:
    """Discard today's bar while today's session is still running.

    Yahoo publishes the current day's bar as soon as trading opens and keeps
    updating it, so its "close" is really just the last price so far. At 9:30
    that is the opening print. Averaging it in would let the first seconds of
    trading decide the signal, and a crossover found that way can be gone by
    the real close. The bot only ever decides on finished sessions.

    `now` is injectable so the tests can place themselves inside a session.
    """
    now = now or datetime.now(MARKET_TZ)
    last_bar = history.index[-1]
    if last_bar.tzinfo is not None:
        last_bar = last_bar.tz_convert(MARKET_TZ)

    if last_bar.date() != now.date() or now.time() >= MARKET_CLOSE:
        return history

    print(f"Ignoring today's unfinished bar ({last_bar.date()}); "
          f"deciding on the last completed session.")
    return history.iloc[:-1]


def add_moving_averages(history: pd.DataFrame) -> pd.DataFrame:
    """Attach the fast and slow simple moving averages of the close price."""
    data = history.copy()
    data["sma_short"] = data["Close"].rolling(window=config.SHORT_WINDOW).mean()
    data["sma_long"] = data["Close"].rolling(window=config.LONG_WINDOW).mean()
    return data


def detect_crossover(data: pd.DataFrame) -> tuple[str | None, dict]:
    """Compare the last two days and report a crossover, if there was one.

    The gap between the averages is what matters: when its sign flips between
    two consecutive days, the lines crossed. A gap that keeps the same sign
    means the lines never met, however far apart they are.
    """
    usable = data.dropna(subset=["sma_short", "sma_long"])
    previous, current = usable.iloc[-2], usable.iloc[-1]

    previous_gap = previous["sma_short"] - previous["sma_long"]
    current_gap = current["sma_short"] - current["sma_long"]

    signal: str | None = None
    if previous_gap <= 0 and current_gap > 0:
        signal = BUY
    elif previous_gap >= 0 and current_gap < 0:
        signal = SELL

    details = {
        "previous_date": usable.index[-2].date().isoformat(),
        "current_date": usable.index[-1].date().isoformat(),
        "close": float(current["Close"]),
        "sma_short": float(current["sma_short"]),
        "sma_long": float(current["sma_long"]),
        "previous_gap": float(previous_gap),
        "current_gap": float(current_gap),
    }
    return signal, details


# ---------------------------------------------------------------------------
# Trade memory
# ---------------------------------------------------------------------------

def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        print(f"warning: {STATE_FILE.name} is unreadable, ignoring it.")
        return {}


def write_state(symbol: str, signal: str, bar_date: str, order_id: str) -> None:
    state = read_state()
    state[symbol] = {
        "signal": signal,
        "bar_date": bar_date,
        "order_id": order_id,
        "traded_on": datetime.now(MARKET_TZ).date().isoformat(),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))


def already_traded(symbol: str, signal: str, bar_date: str) -> bool:
    """True when this exact crossover has already been acted on."""
    last = read_state().get(symbol)
    return bool(last and last["bar_date"] == bar_date and last["signal"] == signal)


def already_traded_today(symbol: str) -> str | None:
    """The bar we traded on earlier today, or None.

    A day can hold more than one scheduled run, and a late one can land on
    the far side of the 4pm close - where the session it treats as the last
    completed one is no longer the same session an earlier run saw. Two runs,
    two different bars, two orders. This caps the day at one.
    """
    last = read_state().get(symbol)
    if not last:
        return None
    today = datetime.now(MARKET_TZ).date().isoformat()
    return last["bar_date"] if last.get("traded_on") == today else None


# ---------------------------------------------------------------------------
# Account and orders
# ---------------------------------------------------------------------------

def held_shares(client: TradingClient, symbol: str) -> float:
    """How many shares of `symbol` the account holds right now, 0 if none."""
    try:
        return float(client.get_open_position(symbol).qty)
    except APIError:
        # Alpaca answers 404 for a symbol with no open position.
        return 0.0


def can_act_on(client: TradingClient, symbol: str, signal: str,
               price: float) -> tuple[bool, str]:
    """Check the account can actually carry out this signal.

    Buying power is read fresh each time, because an order placed for an
    earlier symbol in the same run has already reserved its cash.
    """
    account = client.get_account()

    if account.trading_blocked:
        return False, "the Alpaca account has trading blocked."

    if signal == BUY:
        cost = price * config.TRADE_QTY
        buying_power = float(account.buying_power)
        if buying_power < cost:
            return False, (
                f"buying power is ${buying_power:,.2f} but {config.TRADE_QTY} "
                f"share(s) cost about ${cost:,.2f}."
            )
        return True, ""

    position = held_shares(client, symbol)
    if config.BLOCK_SHORT_SELLS and position < config.TRADE_QTY:
        return False, (
            f"you hold {position:g} share(s) of {symbol} and selling "
            f"{config.TRADE_QTY} would open a short position, which this "
            "account cannot do."
        )
    return True, ""


def place_order(client: TradingClient, symbol: str, signal: str):
    """Send a market order for the configured quantity."""
    request = MarketOrderRequest(
        symbol=symbol,
        qty=config.TRADE_QTY,
        side=OrderSide.BUY if signal == BUY else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    return client.submit_order(request)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_reading(symbol: str, details: dict, signal: str | None) -> None:
    print(f"Symbol            {symbol}")
    print(f"Latest bar        {details['current_date']}")
    print(f"Close             ${details['close']:,.2f}")
    print(f"{config.SHORT_WINDOW}-day average     ${details['sma_short']:,.2f}")
    print(f"{config.LONG_WINDOW}-day average     ${details['sma_long']:,.2f}")
    print(
        f"Gap               ${details['previous_gap']:+,.2f} on "
        f"{details['previous_date']} -> ${details['current_gap']:+,.2f} today"
    )
    print()

    if signal == BUY:
        print(f"Signal            BUY - the {config.SHORT_WINDOW}-day average "
              f"crossed above the {config.LONG_WINDOW}-day.")
    elif signal == SELL:
        print(f"Signal            SELL - the {config.SHORT_WINDOW}-day average "
              f"crossed below the {config.LONG_WINDOW}-day.")
    else:
        side = "above" if details["current_gap"] > 0 else "below"
        print(f"Signal            none - the averages did not cross. The fast "
              f"line stayed {side} the slow one.")


def print_status(client: TradingClient) -> None:
    account = client.get_account()
    mode = "paper" if config.PAPER_TRADING else "LIVE MONEY"

    print()
    print(f"Account           {account.account_number} ({mode})")
    print(f"Cash              ${float(account.cash):,.2f}")
    print(f"Portfolio value   ${float(account.portfolio_value):,.2f}")
    print(f"Buying power      ${float(account.buying_power):,.2f}")
    for symbol in symbols():
        print(f"{symbol:<17} {held_shares(client, symbol):g} share(s)")

    clock = client.get_clock()
    if clock.is_open:
        print("Market            open")
    else:
        print(f"Market            closed, next open "
              f"{clock.next_open.strftime('%Y-%m-%d %H:%M %Z')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def rebuild_dashboard() -> None:
    """Refresh dashboard.html so the page matches what just happened.

    Imported here rather than at the top because dashboard.py imports this
    module, and a top-level import either way would be circular.
    """
    try:
        import dashboard

        dashboard.build()
        print(f"Rebuilt {dashboard.OUTPUT.name}")
    except Exception as exc:
        # A broken dashboard must never stop the bot reporting its run.
        print(f"warning: could not rebuild the dashboard: "
              f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="work out the signal and print it, but place no order",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="show the account and the current averages, then exit",
    )
    parser.add_argument(
        "--log",
        action="store_true",
        help="append output to logs/bot.log instead of the console",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="rebuild dashboard.html once the run is done",
    )
    args = parser.parse_args()

    if args.log:
        start_logging()
        # Always market time. A cloud runner's own clock is UTC, and a log
        # half in UTC and half in Eastern would be unreadable.
        print(f"\n===== {datetime.now(MARKET_TZ):%Y-%m-%d %H:%M:%S %Z} =====")
        print(f"Started {timing_note()}")

    try:
        return decide_and_trade(args)
    finally:
        # The dashboard is rebuilt whatever the run did, including when it
        # placed an order and when it failed, so the page never shows a
        # state the bot has already moved on from.
        if args.dashboard:
            rebuild_dashboard()


def decide_and_trade(args) -> int:
    validate_config()

    if args.status:
        print_status(connect())
        return 0

    worst = 0
    client = None
    watched = symbols()

    for i, symbol in enumerate(watched):
        if len(watched) > 1:
            print()
            print("-" * 56)
            print(f"{symbol}  ({i + 1} of {len(watched)})")
            print()
        try:
            code, client = handle_symbol(symbol, args, client)
        except APIError as exc:
            if "unauthorized" in str(exc).lower():
                raise          # no symbol will work; let the top level say so
            print(f"Alpaca refused the {symbol} order: {exc}")
            code = 1
        except Exception as exc:
            # One bad symbol must not cost the others their turn.
            print(f"Could not handle {symbol}: {type(exc).__name__}: {exc}")
            code = 1
        worst = max(worst, code)

    return worst


def handle_symbol(symbol: str, args, client):
    """Judge one symbol and act on it. Returns (exit code, client).

    The client is threaded through so a run with several symbols opens one
    connection rather than one each, and is only opened at all if something
    actually needs the account.
    """
    print(f"Fetching {config.LOOKBACK_DAYS} trading days of {symbol} data...")
    data = add_moving_averages(fetch_history(symbol, config.LOOKBACK_DAYS))
    signal, details = detect_crossover(data)

    print()
    print_reading(symbol, details, signal)

    if signal is None:
        print("Nothing to do.")
        return 0, client

    if config.PREVENT_DUPLICATE_TRADES and already_traded(
        symbol, signal, details["current_date"]
    ):
        print(f"Already traded this crossover ({details['current_date']}). "
              f"Nothing to do.")
        return 0, client

    if config.ONE_TRADE_PER_DAY:
        traded_bar = already_traded_today(symbol)
        if traded_bar:
            print(f"Already placed a {symbol} order today, on the "
                  f"{traded_bar} session. This run reads the "
                  f"{details['current_date']} session, so acting would mean "
                  f"two orders in one day. Leaving it for tomorrow.")
            return 0, client

    if args.dry_run:
        print(f"Dry run: would place a {signal} order for "
              f"{config.TRADE_QTY} share(s) of {symbol}.")
        return 0, client

    client = client or connect()
    allowed, reason = can_act_on(client, symbol, signal, details["close"])
    if not allowed:
        print(f"Skipping the {symbol} {signal} order: {reason}")
        return 1, client

    order = place_order(client, symbol, signal)
    print(f"Placed a {signal} market order for {config.TRADE_QTY} "
          f"share(s) of {symbol}.")
    print(f"Order id          {order.id}")
    print(f"Status            {order.status.value}")

    if not client.get_clock().is_open:
        print("The market is closed, so it will fill at the next open.")

    write_state(symbol, signal, details["current_date"], str(order.id))
    return 0, client


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except APIError as exc:
        # A raw traceback is no use when you are reading the log on a phone.
        print(f"\nAlpaca rejected the request: {exc}")
        if "unauthorized" in str(exc).lower():
            print("That means the API keys are wrong, expired, or belong to "
                  "the live account while PAPER_TRADING is True.")
            print("Running on your PC, the keys come from the .env file.")
            print("Running on GitHub, they come from the repository secrets "
                  "ALPACA_API_KEY and ALPACA_SECRET_KEY.")
        sys.exit(1)
