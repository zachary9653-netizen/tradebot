"""
Trading bot settings.

Edit the values below to change what the bot trades and how.
Your API keys do NOT go here - they live in the .env file.
"""

# ---------------------------------------------------------------------------
# What to trade
# ---------------------------------------------------------------------------

# Ticker symbols the bot watches. Each one is judged on its own: its own
# averages, its own crossover, its own order, its own memory of what it has
# already traded.
#
# Each symbol costs one Yahoo request per run, so a long list makes runs
# slower. Every buy also needs its own cash: at 1 share each, a $1,500
# account can hold a handful of names at these prices and no more.
SYMBOLS = [
    "AAPL",   # Apple
    "JPM",    # JPMorgan Chase
    "XOM",    # Exxon Mobil
]

# How many shares to buy or sell per signal, per symbol.
TRADE_QTY = 1

# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

# Short moving average period, in trading days. This is the "fast" line.
SHORT_WINDOW = 20

# Long moving average period, in trading days. This is the "slow" line.
LONG_WINDOW = 50

# How many trading days of price history to pull from Yahoo Finance.
# Must be larger than LONG_WINDOW or the slow average never has enough data.
LOOKBACK_DAYS = 100

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

# True  -> trade against the Alpaca paper account (fake money).
# False -> trade against the live account with real money. Leave this True
#          unless you genuinely mean to risk real funds.
PAPER_TRADING = True

# Only place one order per crossover. The bot remembers the last bar it acted
# on in state.json, so re-running it the same day will not trade twice.
PREVENT_DUPLICATE_TRADES = True

# At most one order a day, whatever happens. The schedule fires twice a day
# for daylight-saving cover, and a badly delayed run can land after the 4pm
# close - where the last completed session is a different one from the
# session an earlier run saw. Without this, those two runs are two different
# crossovers to the check above, and both would trade.
ONE_TRADE_PER_DAY = True

# A sell signal is skipped when you hold no shares, because the Alpaca account
# has shorting disabled. Set this to False only if your account can short.
BLOCK_SHORT_SELLS = True
