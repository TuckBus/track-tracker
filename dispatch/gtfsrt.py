"""GTFS-Realtime decoding on top of the raw wire codec.

Field numbers come from the published gtfs-realtime.proto (v2.0). We decode
only the entities Dispatch needs -- VehiclePosition, TripUpdate, Alert -- and
ignore everything else rather than failing, so a feed that adds fields or
extensions keeps working.

Also provides an encoder. That is not for talking to anyone; it builds the
labelled fixtures the evaluation harness replays, which is the only way to
test "did we notice this stall" without waiting for a real bus to break down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import wire

# ---- enums we care about -------------------------------------------------

VEHICLE_STOP_STATUS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}
SCHEDULE_RELATIONSHIP = {
    0: "SCHEDULED",
    1: "SKIPPED",
    2: "NO_DATA",
    3: "UNSCHEDULED",
}
TRIP_SCHEDULE_RELATIONSHIP = {
    0: "SCHEDULED",
    1: "ADDED",
    2: "UNSCHEDULED",
    3: "CANCELED",
    5: "REPLACEMENT",
    6: "DUPLICATED",
    7: "DELETED",
}
ALERT_EFFECT = {
    1: "NO_SERVICE",
    2: "REDUCED_SERVICE",
    3: "SIGNIFICANT_DELAYS",
    4: "DETOUR",
    5: "ADDITIONAL_SERVICE",
    6: "MODIFIED_SERVICE",
    7: "OTHER_EFFECT",
    8: "UNKNOWN_EFFECT",
    9: "STOP_MOVED",
}
ALERT_CAUSE = {
    1: "UNKNOWN_CAUSE",
    2: "OTHER_CAUSE",
    3: "TECHNICAL_PROBLEM",
    4: "STRIKE",
    5: "DEMONSTRATION",
    6: "ACCIDENT",
    7: "HOLIDAY",
    8: "WEATHER",
    9: "MAINTENANCE",
    10: "CONSTRUCTION",
    11: "POLICE_ACTIVITY",
    12: "MEDICAL_EMERGENCY",
}


# ---- decoded shapes ------------------------------------------------------


@dataclass
class TripRef:
    trip_id: str = ""
    route_id: str = ""
    direction_id: int | None = None
    start_time: str = ""
    start_date: str = ""
    schedule_relationship: str = ""


@dataclass
class VehiclePos:
    entity_id: str = ""
    vehicle_id: str = ""
    label: str = ""
    trip: TripRef = field(default_factory=TripRef)
    lat: float | None = None
    lon: float | None = None
    bearing: float | None = None
    speed: float | None = None
    timestamp: int = 0
    current_status: str = ""
    current_stop_sequence: int | None = None
    stop_id: str = ""


@dataclass
class StopTimeUpdate:
    stop_sequence: int | None = None
    stop_id: str = ""
    arrival_delay: int | None = None
    arrival_time: int | None = None
    departure_delay: int | None = None
    departure_time: int | None = None
    schedule_relationship: str = ""


@dataclass
class TripUpd:
    entity_id: str = ""
    vehicle_id: str = ""
    trip: TripRef = field(default_factory=TripRef)
    timestamp: int = 0
    delay: int | None = None
    stop_time_updates: list[StopTimeUpdate] = field(default_factory=list)


@dataclass
class ServiceAlert:
    entity_id: str = ""
    cause: str = ""
    effect: str = ""
    header: str = ""
    description: str = ""
    url: str = ""
    route_ids: list[str] = field(default_factory=list)
    stop_ids: list[str] = field(default_factory=list)
    active_start: int | None = None
    active_end: int | None = None


@dataclass
class Feed:
    timestamp: int = 0
    version: str = ""
    vehicles: list[VehiclePos] = field(default_factory=list)
    trip_updates: list[TripUpd] = field(default_factory=list)
    alerts: list[ServiceAlert] = field(default_factory=list)


# ---- decoding helpers ----------------------------------------------------


def _fields(buf: bytes) -> dict[int, list[tuple[int, Any]]]:
    """Group a message body by field number, preserving repeats."""
    out: dict[int, list[tuple[int, Any]]] = {}
    for fn, wt, val in wire.iter_fields(buf):
        out.setdefault(fn, []).append((wt, val))
    return out


def _s(f: dict, n: int) -> str:
    return wire.as_string(f[n][0][1]) if n in f else ""


def _i(f: dict, n: int) -> int | None:
    return f[n][0][1] if n in f else None


def _si(f: dict, n: int) -> int | None:
    """Signed int -- delays are frequently negative (running early)."""
    return wire.as_signed(f[n][0][1]) if n in f else None


def _f32(f: dict, n: int) -> float | None:
    return wire.as_float(f[n][0][1]) if n in f else None


def _trip(buf: bytes) -> TripRef:
    f = _fields(buf)
    return TripRef(
        trip_id=_s(f, 1),
        route_id=_s(f, 5),
        direction_id=_i(f, 6),
        start_time=_s(f, 2),
        start_date=_s(f, 3),
        schedule_relationship=TRIP_SCHEDULE_RELATIONSHIP.get(_i(f, 4) or 0, ""),
    )


def _translated(buf: bytes) -> str:
    """TranslatedString -> first English translation, else first available."""
    f = _fields(buf)
    best = ""
    for _wt, raw in f.get(1, []):
        t = _fields(raw)
        text, lang = _s(t, 1), _s(t, 2)
        if lang.lower().startswith("en"):
            return text
        best = best or text
    return best


def _vehicle_position(entity_id: str, buf: bytes) -> VehiclePos:
    f = _fields(buf)
    vp = VehiclePos(entity_id=entity_id)
    if 1 in f:
        vp.trip = _trip(f[1][0][1])
    if 8 in f:
        v = _fields(f[8][0][1])
        vp.vehicle_id, vp.label = _s(v, 1), _s(v, 2)
    if 2 in f:
        p = _fields(f[2][0][1])
        vp.lat, vp.lon = _f32(p, 1), _f32(p, 2)
        vp.bearing, vp.speed = _f32(p, 3), _f32(p, 5)
    vp.current_stop_sequence = _i(f, 3)
    vp.stop_id = _s(f, 7)
    vp.current_status = VEHICLE_STOP_STATUS.get(_i(f, 4), "") if 4 in f else ""
    vp.timestamp = _i(f, 5) or 0
    return vp


def _trip_update(entity_id: str, buf: bytes) -> TripUpd:
    f = _fields(buf)
    tu = TripUpd(entity_id=entity_id)
    if 1 in f:
        tu.trip = _trip(f[1][0][1])
    if 3 in f:
        tu.vehicle_id = _s(_fields(f[3][0][1]), 1)
    tu.timestamp = _i(f, 4) or 0
    tu.delay = _si(f, 5)
    for _wt, raw in f.get(2, []):
        s = _fields(raw)
        stu = StopTimeUpdate(
            stop_sequence=_i(s, 1),
            stop_id=_s(s, 4),
            schedule_relationship=SCHEDULE_RELATIONSHIP.get(_i(s, 5) or 0, ""),
        )
        if 2 in s:
            a = _fields(s[2][0][1])
            stu.arrival_delay, stu.arrival_time = _si(a, 1), _si(a, 2)
        if 3 in s:
            d = _fields(s[3][0][1])
            stu.departure_delay, stu.departure_time = _si(d, 1), _si(d, 2)
        tu.stop_time_updates.append(stu)
    return tu


def _alert(entity_id: str, buf: bytes) -> ServiceAlert:
    f = _fields(buf)
    al = ServiceAlert(
        entity_id=entity_id,
        cause=ALERT_CAUSE.get(_i(f, 6) or 0, ""),
        effect=ALERT_EFFECT.get(_i(f, 7) or 0, ""),
    )
    if 10 in f:
        al.header = _translated(f[10][0][1])
    if 11 in f:
        al.description = _translated(f[11][0][1])
    if 8 in f:
        al.url = _translated(f[8][0][1])
    for _wt, raw in f.get(1, []):
        t = _fields(raw)
        al.active_start = _i(t, 1)
        al.active_end = _i(t, 2)
    for _wt, raw in f.get(5, []):
        sel = _fields(raw)
        if 2 in sel:
            al.route_ids.append(_s(sel, 2))
        if 5 in sel:
            al.stop_ids.append(_s(sel, 5))
    return al


def decode_feed(buf: bytes) -> Feed:
    """Decode a FeedMessage. Unknown entities are skipped, not fatal."""
    feed = Feed()
    f = _fields(buf)
    if 1 in f:
        h = _fields(f[1][0][1])
        feed.version = _s(h, 1)
        feed.timestamp = _i(h, 3) or 0
    for _wt, raw in f.get(2, []):
        e = _fields(raw)
        eid = _s(e, 1)
        if 4 in e:
            feed.vehicles.append(_vehicle_position(eid, e[4][0][1]))
        if 3 in e:
            feed.trip_updates.append(_trip_update(eid, e[3][0][1]))
        if 5 in e:
            feed.alerts.append(_alert(eid, e[5][0][1]))
    return feed


# ---- encoding (fixtures only) -------------------------------------------


def _enc_trip(t: TripRef) -> bytes:
    b = b""
    if t.trip_id:
        b += wire.enc_string(1, t.trip_id)
    if t.start_time:
        b += wire.enc_string(2, t.start_time)
    if t.start_date:
        b += wire.enc_string(3, t.start_date)
    if t.route_id:
        b += wire.enc_string(5, t.route_id)
    if t.direction_id is not None:
        b += wire.enc_varint(6, t.direction_id)
    return b


def encode_feed(feed: Feed) -> bytes:
    header = wire.enc_string(1, feed.version or "2.0") + wire.enc_varint(
        3, feed.timestamp
    )
    out = wire.enc_message(1, header)

    for vp in feed.vehicles:
        body = b""
        if vp.trip.trip_id or vp.trip.route_id:
            body += wire.enc_message(1, _enc_trip(vp.trip))
        if vp.lat is not None:
            pos = wire.enc_float(1, vp.lat) + wire.enc_float(2, vp.lon or 0.0)
            if vp.bearing is not None:
                pos += wire.enc_float(3, vp.bearing)
            if vp.speed is not None:
                pos += wire.enc_float(5, vp.speed)
            body += wire.enc_message(2, pos)
        if vp.current_stop_sequence is not None:
            body += wire.enc_varint(3, vp.current_stop_sequence)
        if vp.current_status:
            inv = {v: k for k, v in VEHICLE_STOP_STATUS.items()}
            body += wire.enc_varint(4, inv[vp.current_status])
        body += wire.enc_varint(5, vp.timestamp)
        if vp.stop_id:
            body += wire.enc_string(7, vp.stop_id)
        if vp.vehicle_id:
            body += wire.enc_message(8, wire.enc_string(1, vp.vehicle_id))
        out += wire.enc_message(
            2, wire.enc_string(1, vp.entity_id) + wire.enc_message(4, body)
        )

    for tu in feed.trip_updates:
        body = wire.enc_message(1, _enc_trip(tu.trip))
        for stu in tu.stop_time_updates:
            s = b""
            if stu.stop_sequence is not None:
                s += wire.enc_varint(1, stu.stop_sequence)
            if stu.arrival_delay is not None:
                s += wire.enc_message(2, wire.enc_varint(1, stu.arrival_delay))
            if stu.departure_delay is not None:
                s += wire.enc_message(3, wire.enc_varint(1, stu.departure_delay))
            if stu.stop_id:
                s += wire.enc_string(4, stu.stop_id)
            body += wire.enc_message(2, s)
        if tu.vehicle_id:
            body += wire.enc_message(3, wire.enc_string(1, tu.vehicle_id))
        body += wire.enc_varint(4, tu.timestamp)
        if tu.delay is not None:
            body += wire.enc_varint(5, tu.delay)
        out += wire.enc_message(
            2, wire.enc_string(1, tu.entity_id) + wire.enc_message(3, body)
        )

    for al in feed.alerts:
        body = b""
        if al.active_start is not None:
            tr = wire.enc_varint(1, al.active_start)
            if al.active_end is not None:
                tr += wire.enc_varint(2, al.active_end)
            body += wire.enc_message(1, tr)
        for rid in al.route_ids:
            body += wire.enc_message(5, wire.enc_string(2, rid))
        for sid in al.stop_ids:
            body += wire.enc_message(5, wire.enc_string(5, sid))
        if al.cause:
            inv = {v: k for k, v in ALERT_CAUSE.items()}
            body += wire.enc_varint(6, inv[al.cause])
        if al.effect:
            inv = {v: k for k, v in ALERT_EFFECT.items()}
            body += wire.enc_varint(7, inv[al.effect])

        def _tstr(fn: int, text: str) -> bytes:
            tr = wire.enc_string(1, text) + wire.enc_string(2, "en")
            return wire.enc_message(fn, wire.enc_message(1, tr))

        if al.url:
            body += _tstr(8, al.url)
        if al.header:
            body += _tstr(10, al.header)
        if al.description:
            body += _tstr(11, al.description)
        out += wire.enc_message(
            2, wire.enc_string(1, al.entity_id) + wire.enc_message(5, body)
        )

    return out
