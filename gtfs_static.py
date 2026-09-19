from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import httpx

from .cache import cache

GTFS_URL = "https://www.rideprt.org/developerresources/google_transit.zip"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA_DIR / "gtfs_compact.json"

AIRPORT_STOP_HINTS = ("PITTSBURGH INTERNATIONAL AIRPORT",)
OAKLAND_HINTS = ("CARNEGIE MELLON", "UNIVERSITY PL", "ATWOOD", "THACKERAY", "CRAIG ST")
DOWNTOWN_HINTS = ("GATEWAY", "SMITHFIELD", "WILLIAM PENN", "WYNDHAM")


def load_gtfs() -> dict:
    return cache.get_or_set("gtfs", 60 * 60 * 12, _load_gtfs)


def _load_gtfs() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    with httpx.Client(timeout=90.0, follow_redirects=True) as client:
        response = client.get(GTFS_URL)
        response.raise_for_status()
        payload = response.content

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        routes = _read_csv(archive, "routes.txt")
        trips = _read_csv(archive, "trips.txt")
        stops = _read_csv(archive, "stops.txt")
        stop_times = _read_csv(archive, "stop_times.txt")

    airport_trips = [t for t in trips if t["route_id"] == "28X"]
    trip_ids = {t["trip_id"] for t in airport_trips}
    stop_ids = {row["stop_id"] for row in stop_times if row["trip_id"] in trip_ids}
    flyer_stops = [s for s in stops if s["stop_id"] in stop_ids]

    compact = {
        "routes": [
            {
                "id": r["route_id"],
                "short_name": r.get("route_short_name", ""),
                "long_name": r.get("route_long_name", ""),
                "color": r.get("route_color") or "C5A46A",
            }
            for r in routes
        ],
        "flyer_stops": [
            {
                "id": s["stop_id"],
                "name": s["stop_name"],
                "lat": float(s["stop_lat"]),
                "lon": float(s["stop_lon"]),
                "hub": _classify_stop(s["stop_name"]),
            }
            for s in flyer_stops
        ],
    }
    CACHE_PATH.write_text(json.dumps(compact), encoding="utf-8")
    return compact


def _read_csv(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with archive.open(name) as handle:
        text = handle.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _classify_stop(name: str) -> str:
    upper = name.upper()
    if any(hint in upper for hint in AIRPORT_STOP_HINTS):
        return "airport"
    if any(hint in upper for hint in OAKLAND_HINTS):
        return "oakland"
    if any(hint in upper for hint in DOWNTOWN_HINTS):
        return "downtown"
    if "ROBINSON" in upper or "IKEA" in upper:
        return "robinson"
    if "BUSWAY" in upper:
        return "west_busway"
    return "corridor"


def flyer_stops_by_hub() -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for stop in load_gtfs()["flyer_stops"]:
        grouped.setdefault(stop["hub"], []).append(stop)
    return grouped
