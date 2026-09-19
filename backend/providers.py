from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import TelemetryRecord

logger = logging.getLogger(__name__)


def fetch_json(url: str, *, timeout: float = 10) -> Any:
    request = Request(url, headers={"User-Agent": "ProjectDispatch/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class PRTProvider:
    """Reads PRT GTFS-RT vehicle positions when gtfs-realtime-bindings is installed."""

    default_url = "https://truetime.portauthority.org/gtfsrt/vehiclePositions"

    def __init__(self, url: str | None = None) -> None:
        self.url = url or self.default_url

    def fetch(self) -> list[TelemetryRecord]:
        try:
            from google.transit import gtfs_realtime_pb2
            from google.protobuf.message import DecodeError
        except ImportError:
            logger.warning("PRT polling skipped: install gtfs-realtime-bindings to parse protobuf")
            return []

        request = Request(self.url, headers={"User-Agent": "ProjectDispatch/1.0"})
        with urlopen(request, timeout=10) as response:
            feed = gtfs_realtime_pb2.FeedMessage()
            try:
                feed.ParseFromString(response.read())
            except DecodeError as exc:
                raise ValueError("PRT response was not a valid GTFS-RT protobuf feed") from exc

        observed = datetime.now(timezone.utc)
        records: list[TelemetryRecord] = []
        for entity in feed.entity:
            if not entity.HasField("vehicle"):
                continue
            vehicle = entity.vehicle
            position = vehicle.position if vehicle.HasField("position") else None
            records.append(
                TelemetryRecord(
                    source="prt",
                    vehicle_id=vehicle.vehicle.id or entity.id,
                    route=vehicle.trip.route_id or None,
                    latitude=position.latitude if position else None,
                    longitude=position.longitude if position else None,
                    speed_mph=(position.speed * 2.23694) if position and position.speed else None,
                    altitude_ft=None,
                    observed_at=observed,
                    metadata={"current_status": vehicle.current_status},
                )
            )
        return records


class OpenSkyProvider:
    default_url = "https://opensky-network.org/api/states/all"
    # A small PIT bounding box keeps the response useful and inexpensive.
    bounds = {"lamin": 40.2, "lamax": 40.7, "lomin": -80.5, "lomax": -79.6}

    def __init__(self, url: str | None = None) -> None:
        self.url = url or self.default_url

    def fetch(self) -> list[TelemetryRecord]:
        url = f"{self.url}?{urlencode(self.bounds)}"
        payload = fetch_json(url)
        observed = datetime.now(timezone.utc)
        records: list[TelemetryRecord] = []
        for state in payload.get("states") or []:
            if len(state) < 11 or state[5] is None or state[6] is None:
                continue
            records.append(
                TelemetryRecord(
                    source="opensky",
                    vehicle_id=(state[1] or state[0]).strip(),
                    route=None,
                    latitude=state[6],
                    longitude=state[5],
                    speed_mph=(state[9] * 2.23694) if state[9] is not None else None,
                    altitude_ft=state[7] * 3.28084 if state[7] is not None else None,
                    observed_at=observed,
                    metadata={"callsign": (state[1] or "").strip(), "on_ground": state[8]},
                )
            )
        return records


class AmtrakProvider:
    """Adapter for the unofficial Track Your Train JSON endpoint.

    The endpoint is intentionally configurable because Amtrak can change its map
    implementation without notice. An empty URL disables this provider.
    """

    def __init__(self, url: str | None = None) -> None:
        self.url = url

    def fetch(self) -> list[TelemetryRecord]:
        if not self.url:
            return []
        payload = fetch_json(self.url)
        items = payload if isinstance(payload, list) else payload.get("trains", [])
        records: list[TelemetryRecord] = []
        for item in items:
            if str(item.get("trainNumber", item.get("number", ""))) not in {"42", "43"}:
                continue
            records.append(
                TelemetryRecord(
                    source="amtrak",
                    vehicle_id=str(item.get("trainNumber", item.get("number"))),
                    route="Pennsylvanian",
                    latitude=item.get("latitude"),
                    longitude=item.get("longitude"),
                    speed_mph=item.get("speed"),
                    altitude_ft=None,
                    observed_at=datetime.now(timezone.utc),
                    metadata=item,
                )
            )
        return records
