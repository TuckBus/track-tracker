from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .cache import cache
from .gtfs_static import load_gtfs
from .gtfsrt import as_list, parse_protobuf_text

BUS_BASE = "https://truetime.portauthority.org/gtfsrt-bus"
TRAIN_BASE = "https://truetime.portauthority.org/gtfsrt-train"
USER_AGENT = "SteelLink/0.1 (Steelhacks 2026; regional transit compiler)"


def extract_vehicles(mode: str = "bus") -> dict:
    key = f"vehicles:{mode}"
    return cache.get_or_set(key, 12, lambda: _extract_vehicles(mode))


def extract_trip_updates(mode: str = "bus") -> dict:
    key = f"trips:{mode}"
    return cache.get_or_set(key, 12, lambda: _extract_trip_updates(mode))


def extract_alerts(mode: str = "bus") -> dict:
    key = f"alerts:{mode}"
    return cache.get_or_set(key, 45, lambda: _extract_alerts(mode))


def _extract_vehicles(mode: str) -> dict:
    raw = _fetch_debug(_base(mode) + "/vehicles?debug")
    parsed = parse_protobuf_text(raw)
    vehicles = []
    for entity in as_list(parsed.get("entity")):
        vehicle = entity.get("vehicle") or {}
        trip = vehicle.get("trip") or {}
        position = vehicle.get("position") or {}
        ident = vehicle.get("vehicle") or {}
        vehicles.append(
            {
                "id": ident.get("id") or entity.get("id"),
                "route_id": trip.get("route_id"),
                "trip_id": str(trip.get("trip_id") or ""),
                "lat": position.get("latitude"),
                "lon": position.get("longitude"),
                "bearing": position.get("bearing"),
                "speed_mph": _mps_to_mph(position.get("speed")),
                "timestamp": vehicle.get("timestamp"),
            }
        )
    header = parsed.get("header") or {}
    return {
        "source": _base(mode) + "/vehicles",
        "mode": mode,
        "feed_timestamp": header.get("timestamp"),
        "vehicles": [v for v in vehicles if v.get("lat") and v.get("lon")],
        "count": len(vehicles),
    }


def _extract_trip_updates(mode: str) -> dict:
    raw = _fetch_debug(_base(mode) + "/trips?debug")
    parsed = parse_protobuf_text(raw)
    updates = []
    for entity in as_list(parsed.get("entity")):
        trip_update = entity.get("trip_update") or {}
        trip = trip_update.get("trip") or {}
        vehicle = trip_update.get("vehicle") or {}
        stops = []
        for stop in as_list(trip_update.get("stop_time_update")):
            arrival = stop.get("arrival") or {}
            departure = stop.get("departure") or {}
            when = arrival.get("time") or departure.get("time")
            stops.append(
                {
                    "stop_id": str(stop.get("stop_id") or ""),
                    "stop_sequence": stop.get("stop_sequence"),
                    "time": when,
                    "minutes": _minutes_from_now(when),
                }
            )
        updates.append(
            {
                "route_id": trip.get("route_id"),
                "trip_id": str(trip.get("trip_id") or ""),
                "vehicle_id": vehicle.get("id"),
                "stops": stops,
            }
        )
    header = parsed.get("header") or {}
    return {
        "source": _base(mode) + "/trips",
        "mode": mode,
        "feed_timestamp": header.get("timestamp"),
        "updates": updates,
        "count": len(updates),
    }


def _extract_alerts(mode: str) -> dict:
    raw = _fetch_debug(_base(mode) + "/alerts?debug")
    parsed = parse_protobuf_text(raw)
    alerts = []
    for entity in as_list(parsed.get("entity")):
        alert = entity.get("alert") or {}
        informed = as_list(alert.get("informed_entity"))
        alerts.append(
            {
                "id": entity.get("id"),
                "header": _translated(alert.get("header_text")),
                "description": _translated(alert.get("description_text")),
                "routes": [item.get("route_id") for item in informed if item.get("route_id")],
            }
        )
    return {
        "source": _base(mode) + "/alerts",
        "mode": mode,
        "alerts": alerts,
        "count": len(alerts),
    }


def predictions_for_hubs() -> dict[str, list[dict]]:
    gtfs = load_gtfs()
    stop_lookup = {s["id"]: s for s in gtfs["flyer_stops"]}
    trips = extract_trip_updates("bus")["updates"]
    grouped: dict[str, list[dict]] = {"airport": [], "downtown": [], "oakland": []}
    for update in trips:
        if update.get("route_id") != "28X":
            continue
        for stop in update["stops"]:
            meta = stop_lookup.get(stop["stop_id"])
            if not meta or meta["hub"] not in grouped:
                continue
            if stop["minutes"] is None or stop["minutes"] < 0 or stop["minutes"] > 90:
                continue
            grouped[meta["hub"]].append(
                {
                    "route_id": "28X",
                    "vehicle_id": update.get("vehicle_id"),
                    "stop_id": stop["stop_id"],
                    "stop_name": meta["name"],
                    "minutes": stop["minutes"],
                    "hub": meta["hub"],
                }
            )
    for hub, rows in grouped.items():
        rows.sort(key=lambda item: item["minutes"])
        grouped[hub] = _dedupe_predictions(rows)[:8]
    return grouped


def snapshot() -> dict[str, Any]:
    buses = extract_vehicles("bus")
    try:
        trains = extract_vehicles("train")
    except Exception:
        trains = {"source": TRAIN_BASE + "/vehicles", "mode": "train", "vehicles": [], "count": 0}
    try:
        alerts = extract_alerts("bus")
    except Exception:
        alerts = {"alerts": []}
    flyer = [v for v in buses["vehicles"] if v.get("route_id") == "28X"]
    routes = {}
    for vehicle in buses["vehicles"]:
        route = vehicle.get("route_id") or "unassigned"
        routes[route] = routes.get(route, 0) + 1
    return {
        "buses": buses,
        "trains": trains,
        "alerts": alerts["alerts"][:12],
        "flyer_vehicles": flyer,
        "active_routes": sorted(routes.items(), key=lambda kv: kv[1], reverse=True)[:12],
        "predictions": _safe_predictions(),
        "gtfs": _safe_gtfs_counts(),
    }


def _safe_predictions() -> dict:
    try:
        return predictions_for_hubs()
    except Exception:
        return {"airport": [], "downtown": [], "oakland": []}


def _safe_gtfs_counts() -> dict:
    try:
        data = load_gtfs()
        return {"route_count": len(data["routes"]), "flyer_stop_count": len(data["flyer_stops"])}
    except Exception:
        return {"route_count": 0, "flyer_stop_count": 0}


def _fetch_debug(url: str) -> str:
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _base(mode: str) -> str:
    return TRAIN_BASE if mode == "train" else BUS_BASE


def _translated(node: Any) -> str | None:
    if not node:
        return None
    translations = as_list(node.get("translation"))
    for item in translations:
        if item.get("language") in (None, "en"):
            return item.get("text")
    if translations:
        return translations[0].get("text")
    return None


def _mps_to_mph(speed: Any) -> float | None:
    if speed is None:
        return None
    return round(float(speed) * 2.23694, 1)


def _minutes_from_now(epoch: Any) -> int | None:
    if not epoch:
        return None
    now = datetime.now(timezone.utc).timestamp()
    return int((int(epoch) - now) / 60)


def _dedupe_predictions(rows: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique = []
    for row in rows:
        key = (row["stop_id"], row["minutes"], row.get("vehicle_id"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique
