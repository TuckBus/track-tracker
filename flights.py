from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from .cache import cache

FLIGHT_STATUS_URL = (
    "https://flypittsburgh.com/pittsburgh-international-airport/flights/flight-status/"
)
PIT_TZ = ZoneInfo("America/New_York")
USER_AGENT = "SteelLink/0.1 (Steelhacks 2026; regional transit compiler)"


def extract_flights() -> dict:
    return cache.get_or_set("flights", 45, _extract_flights)


def _extract_flights() -> dict:
    html = _fetch_html()
    soup = BeautifulSoup(html, "lxml")
    arrivals = _parse_table(soup, "arrivals", kind="arrival")
    departures = _parse_table(soup, "departures", kind="departure")
    clock = soup.select_one(".current-time, .flight-status-time, h4")
    return {
        "source": FLIGHT_STATUS_URL,
        "extracted_at": datetime.now(PIT_TZ).isoformat(),
        "airport_clock": clock.get_text(" ", strip=True) if clock else None,
        "arrivals": arrivals,
        "departures": departures,
        "summary": _summarize(arrivals, departures),
        "extraction": {
            "method": "server-side HTML extraction from FlyPittsburgh flight status boards",
            "fields": ["scheduled", "estimated", "city", "flight", "gate", "baggage", "status"],
        },
    }


def _fetch_html() -> str:
    with httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(FLIGHT_STATUS_URL)
        response.raise_for_status()
        return response.text


def _parse_table(soup: BeautifulSoup, tab_id: str, kind: str) -> list[dict]:
    tab = soup.select_one(f"#{tab_id}")
    if not tab:
        return []
    rows = []
    for tr in tab.select("table.flight-table tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        time_text = cells[0].get_text(" ", strip=True)
        scheduled, estimated = _split_times(time_text)
        city = cells[1].get_text(" ", strip=True)
        airline_el = tr.select_one(".airline-name-flightstatus")
        code_el = tr.select_one(".flight-code")
        airline = airline_el.get_text(" ", strip=True) if airline_el else tr.get("data-airline", "")
        flight = code_el.get_text(" ", strip=True) if code_el else ""
        gate = cells[3].get_text(" ", strip=True) if len(cells) > 3 else ""
        baggage = ""
        remarks = ""
        if kind == "arrival":
            baggage = cells[4].get_text(" ", strip=True) if len(cells) > 4 else ""
            remarks = cells[5].get_text(" ", strip=True) if len(cells) > 5 else ""
        else:
            remarks = cells[4].get_text(" ", strip=True) if len(cells) > 4 else ""
        status = _normalize_status(remarks, time_text)
        rows.append(
            {
                "kind": kind,
                "scheduled": scheduled,
                "estimated": estimated,
                "city": city,
                "airline": airline,
                "flight": flight,
                "gate": gate or None,
                "baggage": baggage or None,
                "remarks": remarks or "Scheduled",
                "status": status,
                "delay_minutes": _delay_minutes(scheduled, estimated),
            }
        )
    return rows


def _split_times(text: str) -> tuple[str | None, str | None]:
    times = re.findall(r"\d{1,2}:\d{2}\s*[AP]M", text, flags=re.I)
    if not times:
        return None, None
    scheduled = times[0]
    estimated = times[1] if len(times) > 1 else times[0]
    return scheduled, estimated


def _normalize_status(remarks: str, time_text: str) -> str:
    lower = f"{remarks} {time_text}".lower()
    if "cancel" in lower:
        return "cancelled"
    if "landed" in lower or "arrived" in lower:
        return "landed"
    if "departed" in lower or "airborne" in lower:
        return "departed"
    if "delay" in lower or "now " in lower:
        return "delayed"
    if "boarding" in lower:
        return "boarding"
    return "on_time"


def _delay_minutes(scheduled: str | None, estimated: str | None) -> int | None:
    if not scheduled or not estimated or scheduled == estimated:
        return 0 if scheduled else None
    try:
        start = datetime.strptime(scheduled, "%I:%M %p")
        end = datetime.strptime(estimated, "%I:%M %p")
        delta = int((end - start).total_seconds() // 60)
        if delta < -12 * 60:
            delta += 24 * 60
        return delta
    except ValueError:
        return None


def _summarize(arrivals: list[dict], departures: list[dict]) -> dict:
    def count(rows: list[dict], status: str) -> int:
        return sum(1 for row in rows if row["status"] == status)

    return {
        "arrivals": len(arrivals),
        "departures": len(departures),
        "delayed": count(arrivals, "delayed") + count(departures, "delayed"),
        "cancelled": count(arrivals, "cancelled") + count(departures, "cancelled"),
        "on_time": count(arrivals, "on_time") + count(departures, "on_time"),
    }
