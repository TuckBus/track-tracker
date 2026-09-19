"""Timezone resolution that works on a machine without a tz database.

Linux and macOS ship the IANA timezone database, so `zoneinfo` finds
"America/New_York" for free. Windows does not ship one, and `zoneinfo` raises
ZoneInfoNotFoundError unless the optional `tzdata` package is installed. A
teammate cloning this on a Windows laptop hit exactly that, with seven test
errors and a traceback that says nothing about timezones until the last line.

Rather than take a dependency for one zone, fall back to the machine's own
local timezone. That is correct for anyone running this in the region it
models -- which is everyone on this project, since the whole thing is about
Pittsburgh buses -- and it keeps the install dependency-free.

The degradation is honest but worth stating: on a machine whose clock is set
to a different region, document departure times will be read in that region's
zone. It is not wrong in a way that hides: `dispatch doctor` prints the zone
actually in use.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, tzinfo

DEFAULT_ZONE = "America/New_York"

_cache: dict[str, tzinfo] = {}


def resolve_tz(name: str | None = None) -> tzinfo:
    """Return a tzinfo for `name`, falling back to system local time."""
    name = name or os.environ.get("DISPATCH_TZ") or DEFAULT_ZONE
    cached = _cache.get(name)
    if cached is not None:
        return cached

    try:
        from zoneinfo import ZoneInfo

        tz: tzinfo = ZoneInfo(name)
    except Exception:
        # No tz database on this machine. The local zone carries the right
        # UTC offset and the right DST rules for whoever is sitting here,
        # which beats hardcoding -05:00 and being an hour off half the year.
        tz = datetime.now().astimezone().tzinfo or timezone.utc

    _cache[name] = tz
    return tz


def describe_tz(name: str | None = None) -> str:
    """Human-readable account of which zone is in use and why.

    Surfaced by `dispatch doctor` so a wrong-looking departure time is
    diagnosable in one command instead of by reading source.
    """
    name = name or os.environ.get("DISPATCH_TZ") or DEFAULT_ZONE
    tz = resolve_tz(name)
    now = datetime.now(tz)
    offset = now.strftime("%z") or "?"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return f"{name} (offset {offset})"
    except Exception:
        label = now.tzname() or "system local"
        return (
            f"{label} (offset {offset}) -- no tz database for {name!r}; "
            f"using system local time. `pip install tzdata` for exact "
            f"IANA rules."
        )


def fmt_clock(epoch: float | None) -> str:
    """A wall-clock time like "4:25 PM", portably.

    Two traps this avoids:

    `%-I` strips the leading zero on glibc and raises ValueError on Windows,
    because the dash modifier is a GNU extension. `%#I` is the MSVC spelling.
    Neither is portable, so format with %I and strip the zero in Python.

    `time.localtime` uses the system zone and ignores DISPATCH_TZ, so a
    drafted email could print a different time than the slack calculation
    that triggered it. This goes through the same resolved zone as everything
    else.
    """
    if not epoch:
        return ""
    dt = datetime.fromtimestamp(float(epoch), tz=resolve_tz())
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_day(epoch: float | None) -> str:
    """A date like "Fri, Sep 19", portably."""
    if not epoch:
        return ""
    return datetime.fromtimestamp(float(epoch), tz=resolve_tz()).strftime("%a, %b %d")
