"""Live telemetry adapters.

Two wire formats that share nothing -- PRT ships GTFS-RT protobuf, Amtraker
ships hand-rolled JSON -- normalised to the same Observation so the detectors
never branch on source.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import gtfsrt
from ..schema import Observation, OfficialAlert
from .base import register_telemetry

# Feeds PRT publishes without an API key.
PRT_BUS_URL = "https://truetime.portauthority.org/gtfsrt-bus/vehiclePositions"
PRT_BUS_TRIPS_URL = "https://truetime.portauthority.org/gtfsrt-bus/tripUpdates"
PRT_BUS_ALERTS_URL = "https://truetime.portauthority.org/gtfsrt-bus/alerts"
PRT_RAIL_URL = "https://truetime.portauthority.org/gtfsrt-train/vehiclePositions"
PRT_RAIL_ALERTS_URL = "https://truetime.portauthority.org/gtfsrt-train/alerts"
AMTRAKER_URL = "https://api.amtraker.com/v3/trains"


@register_telemetry("prt-bus")
class PrtBus:
    """PRT bus GTFS-RT. Same class serves rail; only the label differs."""

    media = "application/x-protobuf"
    label = "prt-bus"

    def parse(
        self, blob: bytes, retrieved_at: int
    ) -> tuple[list[Observation], list[OfficialAlert]]:
        feed = gtfsrt.decode_feed(blob)
        feed_ts = feed.timestamp or retrieved_at

        # TripUpdate carries schedule deviation; VehiclePosition carries
        # location. They are separate entities keyed by trip, so join them.
        delay_by_trip: dict[str, int] = {}
        canceled: set[str] = set()
        for tu in feed.trip_updates:
            if tu.trip.schedule_relationship == "CANCELED":
                canceled.add(tu.trip.trip_id)
            delay = tu.delay
            if delay is None and tu.stop_time_updates:
                nxt = tu.stop_time_updates[0]
                delay = (
                    nxt.arrival_delay
                    if nxt.arrival_delay is not None
                    else nxt.departure_delay
                )
            if delay is not None:
                delay_by_trip[tu.trip.trip_id] = delay

        obs = []
        for vp in feed.vehicles:
            obs.append(
                Observation(
                    source_type=self.label,
                    vehicle_id=vp.vehicle_id or vp.entity_id,
                    route_id=vp.trip.route_id,
                    trip_id=vp.trip.trip_id,
                    observed_at=vp.timestamp or feed_ts,
                    lat=vp.lat,
                    lon=vp.lon,
                    speed=vp.speed,
                    status=vp.current_status,
                    stop_id=vp.stop_id,
                    stop_sequence=vp.current_stop_sequence,
                    delay_s=delay_by_trip.get(vp.trip.trip_id),
                    raw_ref=vp.entity_id,
                )
            )

        alerts = [
            OfficialAlert(
                source_type=self.label,
                alert_id=a.entity_id,
                # First sighting in the feed is the only publication time a
                # polling consumer can actually observe.
                published_at=feed_ts,
                active_start=a.active_start,
                effect=a.effect,
                cause=a.cause,
                header=a.header,
                description=a.description,
                route_ids=a.route_ids,
                url=a.url,
            )
            for a in feed.alerts
        ]
        return obs, alerts


@register_telemetry("prt-rail")
class PrtRail(PrtBus):
    label = "prt-rail"


@register_telemetry("amtrak")
class Amtraker:
    """Amtraker v3. Data licensed ODC-By; attribution is in the README.

    Shape is {trainNum: [train, ...]}. Field names and presence vary between
    trains, so every read is defensive.
    """

    media = "application/json"
    label = "amtrak"

    @staticmethod
    def _epoch(value) -> int | None:
        if not value or not isinstance(value, str):
            return None
        txt = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(txt)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    def parse(
        self, blob: bytes, retrieved_at: int
    ) -> tuple[list[Observation], list[OfficialAlert]]:
        try:
            payload = json.loads(blob.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return [], []
        if not isinstance(payload, dict):
            return [], []

        obs = []
        for train_num, entries in payload.items():
            if not isinstance(entries, list):
                continue
            for tr in entries:
                if not isinstance(tr, dict):
                    continue
                stations = tr.get("stations") or []
                delay_s = None
                stop_id = ""
                for st in stations:
                    if not isinstance(st, dict):
                        continue
                    if st.get("status") in ("Enroute", "Station"):
                        stop_id = st.get("code", "") or ""
                        sched = self._epoch(st.get("schArr") or st.get("schDep"))
                        actual = self._epoch(st.get("arr") or st.get("dep"))
                        if sched and actual:
                            delay_s = actual - sched
                        break
                updated = self._epoch(tr.get("updatedAt")) or retrieved_at
                obs.append(
                    Observation(
                        source_type=self.label,
                        vehicle_id=str(tr.get("objectID") or train_num),
                        route_id=str(tr.get("routeName") or train_num),
                        trip_id=f"{train_num}-{tr.get('trainID', '')}",
                        observed_at=updated,
                        lat=tr.get("lat"),
                        lon=tr.get("lon"),
                        speed=tr.get("velocity"),
                        status=str(tr.get("trainState") or ""),
                        stop_id=stop_id,
                        delay_s=delay_s,
                        raw_ref=str(tr.get("objectID") or train_num),
                    )
                )
        return obs, []
