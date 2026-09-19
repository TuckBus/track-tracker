"""Poll the live feeds and write raw bytes to disk.

This is the first thing to run and the last thing to stop. Everything else in
Dispatch reads recordings, which buys three things:

  - a demo that works when the venue wifi does not;
  - an eval that can be re-run against real data instead of only the
    simulator;
  - a bug at 4am that can be reproduced at 10am from the same bytes.

Raw bytes are stored, never parsed output. If a parser turns out to be wrong,
the recording is still good. Parsing on the way in would bake the bug into
the archive.

PRT needs no API key for GTFS-Realtime, which is why it is the default and the
only feed enabled out of the box. The aviation lane is deliberately omitted:
OpenSky meters requests in credits and a 30s poll exhausts the free tier, so
adding it is a decision someone should make on purpose.

    python -m dispatch.cli record --out recordings/friday --interval 30
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import gtfsrt
from .sources.telemetry import (
    AMTRAKER_URL,
    PRT_BUS_ALERTS_URL,
    PRT_BUS_URL,
    PRT_RAIL_ALERTS_URL,
    PRT_RAIL_URL,
)

USER_AGENT = "dispatch/0.1 (SteelHacks XIII project; contact via repo)"

# lane name -> (url, adapter name, extension)
DEFAULT_FEEDS: dict[str, tuple[str, str, str]] = {
    "prt-bus": (PRT_BUS_URL, "prt-bus", "pb"),
    "prt-bus-alerts": (PRT_BUS_ALERTS_URL, "prt-bus", "pb"),
    "prt-rail": (PRT_RAIL_URL, "prt-rail", "pb"),
    "prt-rail-alerts": (PRT_RAIL_ALERTS_URL, "prt-rail", "pb"),
}

OPTIONAL_FEEDS: dict[str, tuple[str, str, str]] = {
    "amtrak": (AMTRAKER_URL, "amtrak", "json"),
}


def feeds_for_base(base: str) -> dict[str, tuple[str, str, str]]:
    """Rewrite the default feeds onto another host.

    Used to point the recorder at tools/mock_prt.py so the fetch-store-replay
    loop can be tested without the agency's network, and to swap in a mirror
    if TrueTime is rate-limiting.
    """
    base = base.rstrip("/")
    return {
        "prt-bus": (f"{base}/gtfsrt-bus/vehiclePositions", "prt-bus", "pb"),
        "prt-bus-alerts": (f"{base}/gtfsrt-bus/alerts", "prt-bus", "pb"),
        "prt-rail": (f"{base}/gtfsrt-train/vehiclePositions", "prt-rail", "pb"),
        "prt-rail-alerts": (f"{base}/gtfsrt-train/alerts", "prt-rail", "pb"),
    }


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _stamp(lane: str, blob: bytes, fallback: int) -> int:
    """Prefer the feed's own timestamp over our clock.

    Two pollers on different machines should agree on what a snapshot is, and
    the feed timestamp is the only shared reference. Falls back to wall clock
    for JSON lanes and unparseable payloads.
    """
    if not lane.startswith("prt"):
        return fallback
    try:
        feed = gtfsrt.decode_feed(blob)
        return feed.timestamp or fallback
    except Exception:
        return fallback


def poll_once(out_dir: str, feeds: dict[str, tuple[str, str, str]]) -> dict:
    """One pass over every feed. Returns a per-lane status dict."""
    now = int(time.time())
    status: dict[str, str] = {}
    for lane, (url, _adapter, ext) in feeds.items():
        lane_dir = os.path.join(out_dir, lane)
        os.makedirs(lane_dir, exist_ok=True)
        try:
            blob = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                TimeoutError) as exc:
            status[lane] = f"error: {type(exc).__name__}"
            continue
        if not blob:
            status[lane] = "empty"
            continue
        stamp = _stamp(lane, blob, now)
        path = os.path.join(lane_dir, f"{stamp}.{ext}")
        if os.path.exists(path):
            # The feed has not advanced since the last poll; nothing new to
            # store. Common when polling faster than the agency publishes.
            status[lane] = "unchanged"
            continue
        with open(path, "wb") as fh:
            fh.write(blob)
        status[lane] = f"{len(blob)}B -> {stamp}.{ext}"
    return status


def record(
    out_dir: str,
    *,
    interval: int = 30,
    duration: int | None = None,
    feeds: dict[str, tuple[str, str, str]] | None = None,
    verbose: bool = True,
) -> str:
    """Poll until interrupted or `duration` seconds elapse."""
    feeds = feeds or dict(DEFAULT_FEEDS)
    os.makedirs(out_dir, exist_ok=True)
    started = time.time()

    manifest = {
        "started_at": int(started),
        "interval_s": interval,
        "feeds": {lane: url for lane, (url, _a, _e) in feeds.items()},
        "note": "raw feed bytes, unparsed. replay with: dispatch replay --world <dir>",
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    polls = 0
    try:
        while True:
            status = poll_once(out_dir, feeds)
            polls += 1
            if verbose:
                stamp = time.strftime("%H:%M:%S")
                summary = "  ".join(f"{k}={v}" for k, v in status.items())
                print(f"[{stamp}] poll {polls}: {summary}")
            if duration is not None and time.time() - started >= duration:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        if verbose:
            print("\nstopped.")

    manifest["polls"] = polls
    manifest["ended_at"] = int(time.time())
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return out_dir


def doctor(feeds: dict[str, tuple[str, str, str]] | None = None) -> dict[str, str]:
    """Check each feed is reachable and parseable. Run this before recording."""
    feeds = feeds or {**DEFAULT_FEEDS, **OPTIONAL_FEEDS}
    out: dict[str, str] = {}
    for lane, (url, _adapter, _ext) in feeds.items():
        try:
            blob = fetch(url, timeout=12)
        except Exception as exc:
            out[lane] = f"UNREACHABLE ({type(exc).__name__})"
            continue
        if lane.startswith("prt"):
            try:
                feed = gtfsrt.decode_feed(blob)
                out[lane] = (
                    f"ok {len(blob)}B  ts={feed.timestamp} "
                    f"vehicles={len(feed.vehicles)} alerts={len(feed.alerts)}"
                )
            except Exception as exc:
                out[lane] = f"UNPARSEABLE ({type(exc).__name__})"
        else:
            out[lane] = f"ok {len(blob)}B"
    return out
