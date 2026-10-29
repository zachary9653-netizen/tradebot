"""
Decide what a run that has just started should do: trade now, wait, or stop.

GitHub's scheduler has no timing guarantee. Runs here have started five to
six hours after the time they were asked for, every day, so asking it to
start at 9:07 does not produce a 9:07 run.

This takes the opposite approach. The workflow asks for runs all morning,
most of which start too early to be useful, and each one decides for itself:

  too early          -> stop, a later run will be better placed
  within the window  -> sleep until 9:07 ET, then trade exactly on time
  past 9:07          -> trade now, late but better than not at all
  after the close    -> stop, tomorrow's run reads the same session anyway
  already ran today  -> stop

Because the repository is public, GitHub Actions minutes are free, so a job
that sits waiting costs nothing.

Deliberately imports nothing outside the standard library: it runs before
the workflow installs anything.

    python3 schedule_window.py          # prints action=... for the workflow
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

# When we actually want to trade: shortly before the 09:30 open, so a market
# order is queued and fills in the opening auction.
TARGET = time(9, 7)

# The furthest ahead of TARGET a run may start and still be worth keeping
# alive. Runs earlier than this stop, because a later scheduled run will
# land closer and waiting that long wastes a job slot.
MAX_WAIT = timedelta(minutes=100)

# After this there is no point starting: the session is over, and today's
# bar becomes a different decision from the one the morning would have made.
GIVE_UP = time(16, 0)

STATE_FILE = Path(__file__).resolve().parent / "state.json"

SKIP, WAIT, RUN = "skip", "wait", "run"


def last_run_date() -> str | None:
    """The date of the last completed run, as the bot recorded it."""
    try:
        meta = json.loads(STATE_FILE.read_text()).get("_meta", {})
        return meta.get("last_run")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def plan(now: datetime, last_run: str | None = None,
         manual: bool = False) -> tuple[str, int, str]:
    """Return (action, seconds to wait, why)."""
    if manual:
        return RUN, 0, "started by hand, so run regardless of the time"

    if last_run == now.date().isoformat():
        return SKIP, 0, "a run already finished today"

    target = datetime.combine(now.date(), TARGET, tzinfo=now.tzinfo)

    if now < target - MAX_WAIT:
        early = int((target - now).total_seconds() // 60)
        return SKIP, 0, (f"{early} min before the target, too early to hold "
                         f"a job open; a later run will be better placed")

    if now < target:
        seconds = int((target - now).total_seconds())
        return WAIT, seconds, (f"waiting {seconds // 60} min to trade at "
                               f"{TARGET:%H:%M} ET")

    if now.time() < GIVE_UP:
        late = int((now - target).total_seconds() // 60)
        return RUN, 0, (f"{late} min past the target - running late rather "
                        f"than not at all")

    return SKIP, 0, ("the session has closed; tomorrow's run reads the same "
                     "session and is not the worse for waiting")


def main() -> int:
    now = datetime.now(MARKET_TZ)
    manual = os.getenv("GITHUB_EVENT_NAME", "") not in ("schedule", "")
    action, seconds, why = plan(now, last_run_date(), manual)

    print(f"Now {now:%Y-%m-%d %H:%M:%S %Z}", file=sys.stderr)
    print(f"Decision: {action} - {why}", file=sys.stderr)

    # The workflow reads these from $GITHUB_OUTPUT.
    print(f"action={action}")
    print(f"seconds={seconds}")
    print(f"why={why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
