from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .flights import extract_flights
from .gtfs_static import flyer_stops_by_hub
from .transit import predictions_for_hubs, snapshot

PIT_TZ = ZoneInfo("America/New_York")
WALK_AFTER_ARRIVAL_MIN = 22
DOWNTOWN_RIDE_MIN = 35
OAKLAND_RIDE_MIN = 50


def plan(query: str) -> dict:
    text = query.strip()
    flights = extract_flights()
    live = snapshot()
    flight = _match_flight(text, flights)
    destination = _match_destination(text)
    intent = _intent(text, flight)

    itinerary = None
    if intent in {"airport_to_city", "flight_connection"} and flight:
        itinerary = _airport_connection(flight, destination, live)
    elif intent == "city_to_airport":
        itinerary = _city_to_airport(destination, live)
    elif "28x" in text.lower() or "airport flyer" in text.lower():
        itinerary = _flyer_status(live)

    return {
        "query": text,
        "intent": intent,
        "flight": flight,
        "destination": destination,
        "itinerary": itinerary,
        "live_context": {
            "flyer_buses": len(live["flyer_vehicles"]),
            "alerts": [a["header"] for a in live["alerts"] if "28X" in (a.get("routes") or [])][:3],
            "predictions": live["predictions"],
        },
        "method": (
            "Grounded planner: extracted FlyPittsburgh boards + PRT TrueTime predictions. "
            "No freeform model answer — every step cites a live source."
        ),
    }


def _intent(text: str, flight: dict | None) -> str:
    lower = text.lower()
    if flight and any(word in lower for word in ("cmu", "oakland", "downtown", "pitt", "get to", "after")):
        return "flight_connection"
    if any(word in lower for word in ("to the airport", "to pit", "catch a flight", "depart")):
        return "city_to_airport"
    if flight:
        return "airport_to_city"
    return "status"


def _match_flight(text: str, flights: dict) -> dict | None:
    code = re.search(r"\b([A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(\d{2,4})\b", text.upper())
    pool = flights["arrivals"] + flights["departures"]
    if code:
        needle = f"{code.group(1)}{code.group(2)}"
        for row in pool:
            if re.sub(r"\s+", "", row["flight"].upper()) == needle:
                return row
    upper = text.upper()
    cities = [row for row in pool if row["city"].split(",")[0].split()[0].upper() in upper and len(row["city"].split()[0]) > 4]
    if len(cities) == 1:
        return cities[0]
    return None


def _match_destination(text: str) -> str:
    lower = text.lower()
    if "cmu" in lower or "carnegie" in lower or "oakland" in lower or "pitt" in lower:
        return "oakland"
    if "robinson" in lower:
        return "robinson"
    return "downtown"


def _airport_connection(flight: dict, destination: str, live: dict) -> dict:
    now = datetime.now(PIT_TZ)
    arrive = _parse_today(flight.get("estimated") or flight.get("scheduled"), now)
    ready = arrive + timedelta(minutes=WALK_AFTER_ARRIVAL_MIN)
    hub_key = "oakland" if destination == "oakland" else "downtown"
    predictions = live["predictions"].get("airport") or []
    ride = next((p for p in predictions if p["minutes"] >= _minutes_until(ready, now) - 3), None)
    if not ride and predictions:
        ride = predictions[0]
    ride_minutes = OAKLAND_RIDE_MIN if destination == "oakland" else DOWNTOWN_RIDE_MIN
    depart_stop = _hub_stop("airport")
    arrive_stop = _hub_stop(hub_key)
    bus_leave = now + timedelta(minutes=ride["minutes"]) if ride else ready
    eta = bus_leave + timedelta(minutes=ride_minutes)
    return {
        "title": f"{flight['flight']} → {destination.title()} on 28X Airport Flyer",
        "steps": [
            {
                "label": "Land at PIT",
                "detail": f"{flight['airline']} {flight['flight']} from {flight['city']}",
                "time": _fmt(arrive),
                "meta": f"Gate {flight.get('gate') or 'TBD'} · {flight['remarks']}",
                "source": "flypittsburgh.com flight status",
            },
            {
                "label": "Walk to 28X",
                "detail": depart_stop["name"] if depart_stop else "Airport Flyer stop",
                "time": _fmt(ready),
                "meta": f"Allow {WALK_AFTER_ARRIVAL_MIN} min for deplane, bags, and the terminal walk",
                "source": "SteelLink connection rule",
            },
            {
                "label": "Board 28X Airport Flyer",
                "detail": f"Next usable bus in {ride['minutes']} min" if ride else "Watch TrueTime at the curb",
                "time": _fmt(bus_leave),
                "meta": f"Vehicle {ride.get('vehicle_id') or 'live'}" if ride else "No prediction in the current window",
                "source": "PRT TrueTime trip updates",
            },
            {
                "label": f"Arrive {destination.title()}",
                "detail": arrive_stop["name"] if arrive_stop else destination,
                "time": _fmt(eta),
                "meta": f"Typical ride {ride_minutes} min on the Airport Flyer",
                "source": "PRT GTFS 28X pattern",
            },
        ],
        "risk": _risk(flight, live),
    }


def _city_to_airport(origin_hub: str, live: dict) -> dict:
    now = datetime.now(PIT_TZ)
    hub = "oakland" if origin_hub == "oakland" else "downtown"
    predictions = live["predictions"].get(hub) or live["predictions"].get("downtown") or []
    ride = predictions[0] if predictions else None
    ride_minutes = OAKLAND_RIDE_MIN if hub == "oakland" else DOWNTOWN_RIDE_MIN
    leave = now + timedelta(minutes=ride["minutes"]) if ride else now
    eta = leave + timedelta(minutes=ride_minutes)
    return {
        "title": f"{hub.title()} → PIT on 28X Airport Flyer",
        "steps": [
            {
                "label": f"Wait at {hub.title()} Flyer stop",
                "detail": (_hub_stop(hub) or {}).get("name", "28X stop"),
                "time": _fmt(leave),
                "meta": f"{ride['minutes']} min" if ride else "No live prediction",
                "source": "PRT TrueTime",
            },
            {
                "label": "Ride 28X outbound",
                "detail": "Airport Flyer to Pittsburgh International Airport",
                "time": _fmt(eta),
                "meta": f"Typical {ride_minutes} min",
                "source": "PRT GTFS 28X pattern",
            },
        ],
        "risk": _risk(None, live),
    }


def _flyer_status(live: dict) -> dict:
    return {
        "title": "28X Airport Flyer live picture",
        "steps": [
            {
                "label": "Buses on the road",
                "detail": f"{len(live['flyer_vehicles'])} Airport Flyer vehicles reporting",
                "time": datetime.now(PIT_TZ).strftime("%I:%M %p"),
                "meta": ", ".join(v.get("id") or "?" for v in live["flyer_vehicles"][:6]) or "None reporting",
                "source": "PRT TrueTime vehicle positions",
            }
        ],
        "risk": _risk(None, live),
    }


def _risk(flight: dict | None, live: dict) -> str | None:
    notes = []
    if flight and flight["status"] == "cancelled":
        notes.append("Flight is cancelled — do not board 28X for this arrival.")
    if flight and (flight.get("delay_minutes") or 0) >= 30:
        notes.append("Long delay: wait for a later Flyer instead of leaving downtown early.")
    flyer_alerts = [a for a in live["alerts"] if "28X" in (a.get("routes") or [])]
    if flyer_alerts:
        notes.append(flyer_alerts[0]["header"])
    return " ".join(notes) if notes else None


def _hub_stop(hub: str) -> dict | None:
    grouped = flyer_stops_by_hub()
    stops = grouped.get(hub) or []
    return stops[0] if stops else None


def _parse_today(value: str | None, now: datetime) -> datetime:
    if not value:
        return now
    parsed = datetime.strptime(value, "%I:%M %p").time()
    stamp = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    if stamp < now - timedelta(hours=6):
        stamp += timedelta(days=1)
    return stamp


def _minutes_until(when: datetime, now: datetime) -> int:
    return int((when - now).total_seconds() // 60)


def _fmt(value: datetime) -> str:
    return value.strftime("%I:%M %p")
