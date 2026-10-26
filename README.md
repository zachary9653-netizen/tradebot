# Moving average crossover bot

Watches one stock, compares a fast and a slow moving average, and places a
market order on Alpaca when the two lines actually cross.

## Setup

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Then open `.env` and paste in your Alpaca key pair:

```
ALPACA_API_KEY=your_key_id_here
ALPACA_SECRET_KEY=your_secret_key_here
```

Create the pair at <https://app.alpaca.markets> under **Home -> API Keys**.
Make sure you are on the paper account when you generate them. `.env` is
listed in `.gitignore`, so the keys stay off any future commit.

## Running it

```bash
python bot.py --status    # account, position and today's averages
python bot.py --dry-run   # find the signal, print it, trade nothing
python bot.py             # find the signal and trade on it
python bot.py --log       # as above, but write to logs/bot.log
```

Start with `--dry-run` until the output reads the way you expect.

## Running it without your PC on

The task below needs this machine switched on. To run the bot on GitHub's
machines instead, follow **[SETUP_GITHUB.md](SETUP_GITHUB.md)** - the
workflow is already written and committed, and about ten minutes of clicking
on your side finishes it.

Do not leave both running. Two schedulers keep separate memories of what
they have traded and can both act on the same crossover.

## The schedule (this PC)

A Windows scheduled task named **Tradebot MA Crossover** runs the bot every
weekday at **9:30 AM Eastern**, as the market opens. This machine is already
on Eastern time, so the task follows the market through daylight saving on
its own.

The signal comes from the **last completed session**, and the trade goes in
at the open. Today's bar is still being written at 9:30 - Yahoo publishes it
the moment trading starts, with its "close" set to the last price so far - so
the bot drops it and decides on yesterday's finished close instead. Without
that, the opening print alone could conjure a crossover that is gone by 4 PM.
The log says so on the days it happens:

```
Ignoring today's unfinished bar (2026-10-26); deciding on the last completed session.
```

It runs `pythonw.exe bot.py --log`, which means no console window appears and
every run is appended to `logs/bot.log`. Check on it with:

```bash
python -c "print(open('logs/bot.log').read()[-2000:])"
```

Useful commands, in PowerShell:

```powershell
Get-ScheduledTaskInfo -TaskName "Tradebot MA Crossover"   # last result, next run
Start-ScheduledTask    -TaskName "Tradebot MA Crossover"  # run it now
Disable-ScheduledTask  -TaskName "Tradebot MA Crossover"  # pause it
Enable-ScheduledTask   -TaskName "Tradebot MA Crossover"  # resume it
Unregister-ScheduledTask -TaskName "Tradebot MA Crossover"  # remove it
```

A last result code of `0` means the run succeeded.

Two things about when it actually fires. The task runs **only while you are
logged in** — it was registered without storing your password, which is the
safer trade. And if the machine is asleep or off at 9:30, the run is not
skipped: it starts as soon as the machine is next available. A late run is
mostly harmless, because the signal comes from a session that has already
closed and `state.json` stops the same crossover being traded twice — but it
will fill at whatever the price is by then rather than at the open.

On a market holiday the bot still runs, finds the previous session as the
latest bar, and does nothing new.

## The dashboard

Live, and readable from anywhere:

**<https://zachary9653-netizen.github.io/tradebot/>**

That public page carries market data and the strategy's reading of it, and
nothing about the account - no balance, holding, order or account number.
`check_public.py` gates the deploy and fails the build if any of that ever
appears, so it cannot start leaking by accident later.

The full version, with your balance and orders, stays private: build it
locally, or download it from any run on the Actions tab.

```bash
python dashboard.py --open
```

Writes `dashboard.html` and opens it. One self-contained page - no internet
needed to view it, nothing loaded from a CDN - showing the signal, your
balance and position, a year of price with both averages and every crossover
marked, recent orders, and the last few scheduled runs. Hover the chart for
exact values; the button swaps light and dark.

The weekday task rebuilds it automatically, so whenever you open the file it
reflects the most recent run. It only ever reports - orders come from
`bot.py`.

## Seeing it work

```bash
python simulate.py            # replay 2 years, place nothing
python simulate.py --years 5  # a longer stretch
```

This calls the same functions `bot.py` calls - same fetch, same averages,
same crossover test - and walks them forward a day at a time, printing every
signal it would have acted on and what a $1,500 account would have done.
It ends by building the exact Alpaca order a buy signal produces, without
sending it. No account is touched.

It also prints what simply buying one share and holding would have returned
over the same window. Read that honestly: one stock over one window says
very little about whether the strategy is any good. The simulation shows the
bot does what it claims, which is a smaller claim than the strategy working.

## Settings

Everything you are likely to change lives in `config.py`:

| Setting | Default | What it does |
| --- | --- | --- |
| `SYMBOL` | `AAPL` | Ticker to watch and trade |
| `TRADE_QTY` | `1` | Shares per order |
| `SHORT_WINDOW` | `20` | Fast average, in trading days |
| `LONG_WINDOW` | `50` | Slow average, in trading days |
| `LOOKBACK_DAYS` | `100` | Trading days of history to pull |
| `PAPER_TRADING` | `True` | `False` sends orders to the live account |
| `PREVENT_DUPLICATE_TRADES` | `True` | One order per crossover |
| `BLOCK_SHORT_SELLS` | `True` | Never sell shares you do not hold |

## What counts as a signal

Only a genuine crossing between the two most recent trading days:

- **Buy** - the 20-day average was at or below the 50-day yesterday, and is
  above it today.
- **Sell** - the 20-day average was at or above the 50-day yesterday, and is
  below it today.

A fast line that is merely sitting above the slow one is *not* a signal. The
bot tracks the gap between the averages and fires only when its sign flips,
so a trend already under way is left alone. `test_crossover.py` checks this
against hand-built price series:

```bash
python test_crossover.py
```

## Guards

The bot refuses to trade rather than do something you did not intend:

- **One order per crossover.** The bar it acted on is recorded in
  `state.json`, so running the bot twice in a day will not double up.
- **One order per day, whatever happens.** The schedule fires twice daily for
  daylight-saving cover. A badly delayed run can land after the 4pm close,
  where the last completed session is no longer the one an earlier run saw -
  two runs, two different bars, and to the per-crossover check above, two
  different crossovers. The day cap is what stops that becoming two orders.
- **No accidental shorts.** A sell signal is skipped when you hold fewer
  shares than `TRADE_QTY`. Your Alpaca account has shorting disabled, so the
  order would be rejected anyway.
- **No overdrawn buys.** A buy is skipped when buying power will not cover it.
  The account is a cash account with no margin, so buying power is just cash.

Orders are market orders with a day time-in-force. One placed while the
market is closed queues for the next open.

## Files

| File | |
| --- | --- |
| `bot.py` | The bot |
| `config.py` | Your settings |
| `.env` | Your Alpaca keys, not committed |
| `.env.example` | Template for the above |
| `dashboard.py` | Builds the dashboard page |
| `dashboard.html` | Written at runtime, open it in a browser |
| `simulate.py` | Replays history, places nothing |
| `test_crossover.py` | Checks the crossover logic |
| `state.json` | Written at runtime, remembers the last trade |
| `logs/bot.log` | Written at runtime, one entry per scheduled run |
| `SETUP_GITHUB.md` | How to run it in the cloud |
| `.github/workflows/trade.yml` | The cloud schedule |

## Worth knowing

This trades on daily bars, so it makes at most one decision a day, and that
decision is always based on a session that has already finished.

Orders go in at 9:30, the most volatile minutes of the day, and a market
order takes whatever price it finds there. If the fills look poor, move the
task to 9:35 or 9:45 — the signal will not change, only the price you get:

```powershell
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:45AM
Set-ScheduledTask -TaskName "Tradebot MA Crossover" -Trigger $t
```

A crossover strategy is a trend follower: it is late by design, and it will
whipsaw in a sideways market, buying and selling into small reversals. Paper
trade it for a while before trusting it with anything real.
