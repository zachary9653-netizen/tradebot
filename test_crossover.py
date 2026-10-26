"""Check detect_crossover against hand-built price series."""
import sys
import pandas as pd

import config
import bot


def frame(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return bot.add_moving_averages(pd.DataFrame({"Close": closes}, index=idx))


def signal_at(closes, end):
    """Signal as the bot would see it if `end` were the most recent bar."""
    return bot.detect_crossover(frame(closes).iloc[: end + 1])


n = config.LONG_WINDOW + 5
failures = 0


def check(label, got, want):
    global failures
    ok = got == want
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {label:<34} -> {got}  (expected {want})")


print("Crossovers landing on the final bar:")

# A flat stretch then a jump up: the fast average lifts off the slow one.
check("flat then jump up", signal_at([100.0] * n + [400.0], n)[0], "BUY")

# A flat stretch then a drop: the fast average falls through the slow one.
check("flat then drop", signal_at([400.0] * n + [100.0], n)[0], "SELL")

# A rise, then a decline, then a recovery. The fast line starts above the
# slow one, is dragged through it, and is pushed back up - so both crossings
# come from a clearly non-zero gap rather than the flat-line edge case.
wave = (
    [100.0 + i for i in range(60)]
    + [160.0 - i for i in range(60)]
    + [100.0 + i for i in range(60)]
)
gaps = (frame(wave)["sma_short"] - frame(wave)["sma_long"]).dropna()
offset = len(wave) - len(gaps)
flips = [
    (i + offset, "BUY" if gaps.iloc[i] > 0 else "SELL")
    for i in range(1, len(gaps))
    if (gaps.iloc[i - 1] <= 0 < gaps.iloc[i]) or (gaps.iloc[i - 1] >= 0 > gaps.iloc[i])
]
print(f"  (wave crosses at bars {[f[0] for f in flips]})")
for bar, want in flips:
    got, detail = signal_at(wave, bar)
    check(f"wave cross at bar {bar}", got, want)
    print(f"        gap {detail['previous_gap']:+.2f} -> {detail['current_gap']:+.2f}")

print("\nNo crossover on the final bar:")

# Fast line already above the slow one and staying there - not a crossover.
check("already above, still rising", signal_at([100.0] * n + [400.0] * 40, n + 39)[0], None)
# Fast line already below the slow one and staying there.
check("already below, still falling", signal_at([400.0] * n + [100.0] * 40, n + 39)[0], None)
# Perfectly flat: the averages sit on top of each other, the gap never flips.
check("flat series", signal_at([100.0] * (n + 20), n + 19)[0], None)
# The bar right after an upward cross must not re-trigger.
check("bar after a cross", signal_at([100.0] * n + [400.0, 400.0], n + 1)[0], None)


print("\nUnfinished-session guard (bars dated today, varying clock):")

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def bars_ending_today(tz=None):
    """Three daily bars whose last one is dated today."""
    end = datetime.now(ET).date()
    idx = pd.date_range(end=end, periods=3, freq="D", tz=tz)
    return pd.DataFrame({"Close": [10.0, 11.0, 12.0]}, index=idx)


for label, clock, want_dropped in [
    ("09:30, market just opened", datetime.now(ET).replace(hour=9, minute=30), True),
    ("12:00, mid-session", datetime.now(ET).replace(hour=12, minute=0), True),
    ("15:59, a minute to go", datetime.now(ET).replace(hour=15, minute=59), True),
    ("16:00, the close", datetime.now(ET).replace(hour=16, minute=0), False),
    ("18:00, after hours", datetime.now(ET).replace(hour=18, minute=0), False),
]:
    for tz in (None, ET):
        kept = bot.drop_unfinished_session(bars_ending_today(tz), now=clock)
        dropped = len(kept) == 2
        tag = "naive" if tz is None else "tz-aware"
        check(f"{label} [{tag}]", dropped, want_dropped)

# A bar from a previous day is finished whatever the clock says.
old = pd.DataFrame({"Close": [10.0]}, index=pd.date_range(end="2026-10-23", periods=1))
kept = bot.drop_unfinished_session(old, now=datetime.now(ET).replace(hour=9, minute=30))
check("yesterday's bar at 09:30", len(kept), 1)


print("\nOne trade per day, across the 4pm boundary:")

import json
from datetime import timedelta

# Work on a scratch state file so the real one is untouched.
real_state = bot.STATE_FILE
bot.STATE_FILE = real_state.parent / "state.test.json"
try:
    today = datetime.now(ET).date().isoformat()
    yesterday = (datetime.now(ET).date() - timedelta(days=1)).isoformat()

    bot.STATE_FILE.write_text("{}")
    check("nothing traded yet", bot.already_traded_today("AAPL"), None)

    # Today's 3:29pm run buys on Friday's bar - exactly what happened.
    bot.write_state("AAPL", "BUY", "2026-10-23", "order-1")
    check("after a trade today", bot.already_traded_today("AAPL"), "2026-10-23")

    # The 4:04pm run now reads a DIFFERENT bar. The old bar-date check lets
    # it through; the day cap is what stops it.
    check("old check would allow it",
          bot.already_traded("AAPL", "BUY", "2026-10-26"), False)
    check("day cap blocks it",
          bot.already_traded_today("AAPL") is not None, True)

    # A trade from yesterday must not block today.
    state = json.loads(bot.STATE_FILE.read_text())
    state["AAPL"]["traded_on"] = yesterday
    bot.STATE_FILE.write_text(json.dumps(state))
    check("yesterday's trade does not block", bot.already_traded_today("AAPL"), None)

    # State written before this fix existed has no traded_on at all.
    bot.STATE_FILE.write_text(json.dumps(
        {"AAPL": {"signal": "BUY", "bar_date": "2026-10-23", "order_id": "old"}}))
    check("state from before the fix", bot.already_traded_today("AAPL"), None)
finally:
    bot.STATE_FILE.unlink(missing_ok=True)
    bot.STATE_FILE = real_state

print("\nLateness reporting:")

for label, (h, m), sched, expect in [
    ("09:07, on time",        (9, 7),   True,  "before the open"),
    ("09:29, just early",     (9, 29),  True,  "before the open"),
    ("09:30, the bell",       (9, 30),  True,  "right at the open"),
    ("09:45, slightly late",  (9, 45),  True,  "15 min after the open"),
    ("10:30, an hour late",   (10, 30), True,  "1h 00m after the open"),
    ("15:29, today's mess",   (15, 29), True,  "5h 59m after the open"),
    # A scheduled run that late is the scheduler's doing; one you started
    # yourself at 7pm is not, and must not say so.
    ("15:29, scheduled",      (15, 29), True,  "the scheduler was late"),
    ("19:14, run by hand",    (19, 14), False, "9h 44m after the open."),
]:
    note = bot.timing_note(datetime.now(ET).replace(hour=h, minute=m),
                           scheduled=sched)
    ok = expect in note
    if label.endswith("by hand"):
        ok = ok and "scheduler" not in note
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {label:<24} -> {note[:50]}")

print()
print("All cases behaved as expected." if not failures
      else f"{failures} case(s) failed.")
sys.exit(1 if failures else 0)
