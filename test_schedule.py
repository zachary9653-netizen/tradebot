"""
Check the run-window logic, and simulate it against real GitHub delays.

The second half is the one that matters: it replays the delays actually
observed on this repository and asks whether the ladder of scheduled times
would have produced a trade at 09:07 ET.
"""

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import schedule_window as sw

ET = ZoneInfo("America/New_York")
DAY = datetime(2026, 10, 29, tzinfo=ET)          # a Thursday
failures = 0


def at(hh, mm):
    return DAY.replace(hour=hh, minute=mm)


def check(label, got, want):
    global failures
    ok = got == want
    failures += not ok
    print(f"  {'pass' if ok else 'FAIL'}  {label:<42} -> {got}  (expected {want})")


print("Decisions by start time:")
check("03:00, hours early",      sw.plan(at(3, 0))[0],   sw.SKIP)
check("07:26, just too early",   sw.plan(at(7, 26))[0],  sw.SKIP)
check("07:28, window opens",     sw.plan(at(7, 28))[0],  sw.WAIT)
check("08:30, inside the window", sw.plan(at(8, 30))[0], sw.WAIT)
check("09:06, a minute to go",   sw.plan(at(9, 6))[0],   sw.WAIT)
check("09:07, on the target",    sw.plan(at(9, 7))[0],   sw.RUN)
check("12:00, late",             sw.plan(at(12, 0))[0],  sw.RUN)
check("15:50, very late",        sw.plan(at(15, 50))[0], sw.RUN)
check("16:00, the close",        sw.plan(at(16, 0))[0],  sw.SKIP)
check("18:00, evening",          sw.plan(at(18, 0))[0],  sw.SKIP)

print("\nWaiting the right amount:")
check("08:07 waits 60 min", sw.plan(at(8, 7))[1] // 60, 60)
check("09:00 waits 7 min",  sw.plan(at(9, 0))[1] // 60, 7)

print("\nStanding down once the day is done:")
check("already ran today",
      sw.plan(at(8, 30), last_run="2026-10-29")[0], sw.SKIP)
check("ran yesterday, not today",
      sw.plan(at(8, 30), last_run="2026-10-28")[0], sw.WAIT)
check("manual run beats everything",
      sw.plan(at(3, 0), last_run="2026-10-29", manual=True)[0], sw.RUN)


print("\n" + "=" * 68)
print("Against the delays this repository actually saw")
print("=" * 68)

# Every half hour from 05:00 to 14:30 UTC, which is what the workflow asks
# for. Expressed here in ET, the timezone the logic works in.
CRONS_ET = [(h, m) for h in range(1, 11) for m in (0, 30)]

# Delays measured from the logs: 27 and 28 October, plus the ideal and a
# couple of worse cases to see where it breaks down.
SCENARIOS = [
    ("GitHub on time (0h)",        0.0),
    ("28 Oct, best case (5h17m)",  5.28),
    ("28 Oct, worst case (6h20m)", 6.33),
    ("27 Oct (5h55m)",             5.92),
    ("worse than seen (8h)",       8.0),
    ("much worse (10h)",           10.0),
]

for label, delay_hours in SCENARIOS:
    delay = timedelta(hours=delay_hours)
    traded_at, waits, runs, skips = None, 0, 0, 0
    last_run = None

    for h, m in CRONS_ET:
        start = at(h, m) + delay
        if start.date() != DAY.date():
            skips += 1
            continue
        action, seconds, _ = sw.plan(start, last_run)
        if action == sw.SKIP:
            skips += 1
        elif action == sw.WAIT:
            waits += 1
            finished = start + timedelta(seconds=seconds)
            if traded_at is None:
                traded_at = finished
                last_run = DAY.date().isoformat()
        else:
            runs += 1
            if traded_at is None:
                traded_at = start
                last_run = DAY.date().isoformat()

    when = "never" if traded_at is None else f"{traded_at:%H:%M} ET"
    on_time = traded_at is not None and traded_at.time() <= sw.TARGET
    verdict = "ON TIME" if on_time else ("late" if traded_at else "NO TRADE")
    print(f"  {label:<28} traded {when:<9} {verdict:<8} "
          f"({waits} waited, {runs} ran, {skips} skipped)")
    # Every scenario must at least trade; the realistic ones must be on time.
    if traded_at is None:
        failures += 1
        print("        FAIL - no run would have traded at all")
    elif delay_hours <= 8 and not on_time:
        failures += 1
        print("        FAIL - should have been on time at this delay")

print()
print("All cases behaved as expected." if not failures
      else f"{failures} case(s) failed.")
sys.exit(1 if failures else 0)
