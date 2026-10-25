"""
Dry run of the strategy over real history, so you can watch it work.

This places no orders and touches no account. It calls the same functions
bot.py calls - same data fetch, same moving averages, same crossover test -
and walks them forward one day at a time over the last couple of years,
showing every signal the bot would have acted on.

    python simulate.py            # replay the default 2 years
    python simulate.py --years 5  # replay a longer stretch
"""

from __future__ import annotations

import argparse

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

import bot
import config

TRADING_DAYS_PER_YEAR = 252


def replay(years: int) -> None:
    """Walk through history one day at a time, as the bot would see it."""
    days = years * TRADING_DAYS_PER_YEAR

    print(f"Replaying {years} year(s) of real {config.SYMBOL} data through the "
          f"bot's own logic.")
    print(f"Strategy: {config.SHORT_WINDOW}/{config.LONG_WINDOW} day crossover, "
          f"{config.TRADE_QTY} share(s) per signal.\n")

    data = bot.add_moving_averages(bot.fetch_history(config.SYMBOL, days))

    # Start the wallet where the real account started.
    cash = 1500.00
    shares = 0
    trades = []
    skipped = 0

    # Each step shows the bot only the days up to `i`, exactly the window it
    # would have had on the morning after that close.
    for i in range(config.LONG_WINDOW, len(data)):
        signal, detail = bot.detect_crossover(data.iloc[: i + 1])
        if signal is None:
            continue

        price = detail["close"]
        day = detail["current_date"]

        if signal == bot.BUY:
            if cash < price * config.TRADE_QTY:
                trades.append((day, signal, price, "skipped, not enough cash"))
                skipped += 1
                continue
            cash -= price * config.TRADE_QTY
            shares += config.TRADE_QTY
            trades.append((day, signal, price, f"bought {config.TRADE_QTY}"))
        else:
            # The real account cannot short, so a sell with nothing held is
            # skipped rather than opening a short position.
            if shares < config.TRADE_QTY:
                trades.append((day, signal, price, "skipped, nothing held"))
                skipped += 1
                continue
            cash += price * config.TRADE_QTY
            shares -= config.TRADE_QTY
            trades.append((day, signal, price, f"sold {config.TRADE_QTY}"))

    report(data, trades, cash, shares, skipped)


def report(data, trades, cash, shares, skipped) -> None:
    first = data.index[0].date()
    last = data.index[-1].date()
    last_price = float(data["Close"].iloc[-1])

    print(f"Window            {first} to {last}  ({len(data)} trading days)")
    print(f"Crossovers found  {len(trades)}\n")

    if not trades:
        print("No crossovers in this window. Try a longer --years.")
        return

    print(f"{'Date':<12} {'Signal':<7} {'Close':>10}   What the bot would do")
    print("-" * 64)
    for day, signal, price, action in trades:
        print(f"{day:<12} {signal:<7} {price:>10,.2f}   {action}")

    value = cash + shares * last_price
    print()
    print(f"Started with      $1,500.00 cash, 0 shares")
    print(f"Ended with        ${cash:,.2f} cash, {shares} share(s) "
          f"worth ${shares * last_price:,.2f}")
    print(f"Account value     ${value:,.2f}  ({value - 1500:+,.2f})")
    if skipped:
        print(f"Skipped           {skipped} signal(s), blocked by the "
              f"no-short and cash rules")

    # The honest comparison: one share bought at the start and never touched.
    first_price = float(data["Close"].iloc[0])
    hold = last_price - first_price
    print()
    print(f"For comparison, 1 share bought on {first} at ${first_price:,.2f} "
          f"and held")
    print(f"would be worth ${last_price:,.2f} today, a gain of ${hold:+,.2f}.")
    verdict = "beat" if (value - 1500) > hold else "trailed"
    print(f"The strategy {verdict} buy-and-hold over this window.")
    print("\nOne window of one stock is not evidence the strategy works. It "
          "shows the\nbot does what it claims, which is a different and "
          "smaller claim.")


def show_order_shape() -> None:
    """Build the exact order a live signal produces, without sending it."""
    print("\n" + "=" * 64)
    print("The order a BUY signal would send to Alpaca")
    print("=" * 64)

    request = MarketOrderRequest(
        symbol=config.SYMBOL,
        qty=config.TRADE_QTY,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )
    for field, value in request.model_dump(exclude_none=True).items():
        print(f"  {field:<16} {getattr(value, 'value', value)}")
    print("\nBuilt and validated, not submitted. Nothing was sent to Alpaca "
          "and no account was touched.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=2,
                        help="how many years of history to replay (default 2)")
    args = parser.parse_args()

    print("=" * 64)
    print("SIMULATED RUN - no orders are placed")
    print("=" * 64 + "\n")

    replay(args.years)
    show_order_shape()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
