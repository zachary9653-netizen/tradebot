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

print(f"\n{'All cases behaved as expected.' if not failures else f'{failures} case(s) failed.'}")
sys.exit(1 if failures else 0)
