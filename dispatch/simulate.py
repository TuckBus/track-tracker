"""A labelled synthetic PRT world.

Why this exists. Dispatch detects rare events. You cannot stand at a bus stop
during judging and wait for a bus to break down, and you cannot build a
confusion matrix out of anecdotes. So we generate a world where we know the
answer, inject faults at known times, and score against that.

Two rules keep this honest:

1. The generator writes **real GTFS-Realtime protobuf**, byte-for-byte the
   same shape as `truetime.portauthority.org`. It goes through the same
   `PrtBus` adapter as live data. Nothing in the pipeline knows it is
   synthetic, so passing here is evidence about the pipeline, not about the
   fixtures.

2. Ground truth is written before the pipeline runs and never read by it.
   `should_notify` is a property of the injected scenario, not of anything
   the detector or the model says.

The benign scenarios matter more than the disruptive ones. It is easy to
detect a bus that stopped moving; the hard part is not crying wolf about the
eleven buses sitting at their terminals, and that is what the benign classes
measure.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import random
import zipfile
from dataclasses import asdict, dataclass, field

from . import gtfsrt

TICK_SECONDS = 20  # PRT publishes every ~20-30s
EARTH_R = 6_371_000.0


# --------------------------------------------------------------------------
# geography
# --------------------------------------------------------------------------


@dataclass
class Route:
    route_id: str
    stops: list[tuple[str, float, float]]  # (stop_id, lat, lon)
    headway_min: int

    @property
    def terminals(self) -> tuple[str, str]:
        return self.stops[0][0], self.stops[-1][0]


def _line(route_id: str, a: tuple[float, float], b: tuple[float, float], n: int,
          headway: int) -> Route:
    """A route as n evenly spaced stops between two endpoints."""
    stops = []
    for i in range(n):
        f = i / (n - 1)
        stops.append((f"{route_id}-S{i:02d}", a[0] + (b[0] - a[0]) * f,
                      a[1] + (b[1] - a[1]) * f))
    return Route(route_id=route_id, stops=stops, headway_min=headway)


# Real PRT corridors, roughly. Coordinates are approximate on purpose: the
# geometry only has to be self-consistent for distance arithmetic to mean
# something, and we are not claiming to be a map.
def build_routes() -> list[Route]:
    return [
        # Oakland <-> Squirrel Hill, the Pitt commute. Frequent.
        _line("61C", (40.4418, -79.9560), (40.4370, -79.9220), 9, headway=10),
        # Oakland <-> Highland Park.
        _line("71B", (40.4440, -79.9530), (40.4790, -79.9220), 8, headway=12),
        # Downtown <-> Pittsburgh International. Infrequent, unrecoverable.
        _line("28X", (40.4417, -80.0000), (40.4950, -80.2300), 7, headway=30),
        # East Busway. Very frequent, so a miss is cheap.
        _line("P1", (40.4415, -79.9950), (40.4520, -79.8850), 6, headway=6),
    ]


# Real routes are not one speed. A bus crawls through a congested commercial
# stretch and moves on a busway, and that variation is spatial: it belongs to
# the place, not to the vehicle. Without it in the fixture, a predictor that
# learns per-segment speeds has nothing to learn and cannot be evaluated --
# which is exactly what happened the first time we scored one.
#
# Multipliers on the nominal speed, indexed by how far along the route you are.
# Each route gets a rotation of the same pattern so the profiles differ.
SPEED_PROFILE = (0.45, 0.65, 1.35, 1.55, 1.25, 0.55, 0.95, 1.40)


def profile_at(fraction: float, rotation: int) -> float:
    n = len(SPEED_PROFILE)
    i = int(max(0.0, min(0.9999, fraction)) * n)
    return SPEED_PROFILE[(i + rotation) % n]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
# ground truth
# --------------------------------------------------------------------------

# Benign classes trip the detector but must not reach the user. Disruptive
# classes are genuine service failures.
BENIGN = ("TERMINAL_LAYOVER", "TIMEPOINT_HOLD", "FEED_ARTIFACT", "GARAGE_PULLIN")
DISRUPTIVE = ("STALL_DISABLED", "VANISHED_REAL", "BRIDGE_BLOCKAGE")


@dataclass
class Scenario:
    scenario_id: str
    kind: str
    route_id: str
    vehicle_id: str
    onset: int
    duration: int
    disruptive: bool
    on_itinerary: bool
    official_alert_at: int | None
    note: str

    @property
    def should_notify(self) -> bool:
        """The label the eval scores against.

        Two conditions, deliberately. A real breakdown on a route the user is
        not riding is worth logging and not worth interrupting them for, so a
        system that notifies on every genuine fault is still wrong. Splitting
        the label this way is what makes the itinerary join measurable.
        """
        return self.disruptive and self.on_itinerary

    def to_dict(self) -> dict:
        d = asdict(self)
        d["should_notify"] = self.should_notify
        return d


# --------------------------------------------------------------------------
# vehicles
# --------------------------------------------------------------------------


@dataclass
class Vehicle:
    vehicle_id: str
    route: Route
    offset_m: float
    direction: int = 1
    trip_seq: int = 0
    frozen_until: int = 0
    hidden_until: int = 0
    jump_on_return_m: float = 0.0
    never_returns: bool = False
    force_stop_id: str = ""
    speed_mps: float = 8.5
    profile_rotation: int = 0
    segment_len: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.segment_len = [
            haversine_m(*self.route.stops[i][1:], *self.route.stops[i + 1][1:])
            for i in range(len(self.route.stops) - 1)
        ]

    @property
    def total_len(self) -> float:
        return sum(self.segment_len)

    def offset_of_stop(self, index: int) -> float:
        """Distance along the route to a given stop index."""
        return sum(self.segment_len[:index])

    def position(self) -> tuple[float, float, str, int]:
        """Interpolate along the polyline. Returns lat, lon, nearest stop, seq."""
        d = self.offset_m % max(self.total_len, 1.0)
        travelled = 0.0
        for i, seg in enumerate(self.segment_len):
            if travelled + seg >= d:
                f = (d - travelled) / seg if seg else 0.0
                a, b = self.route.stops[i], self.route.stops[i + 1]
                lat = a[1] + (b[1] - a[1]) * f
                lon = a[2] + (b[2] - a[2]) * f
                near = a if f < 0.5 else b
                seq = i if f < 0.5 else i + 1
                return lat, lon, near[0], seq
            travelled += seg
        last = self.route.stops[-1]
        return last[1], last[2], last[0], len(self.route.stops) - 1

    def step(self, t: int, rng: random.Random) -> None:
        if t < self.frozen_until:
            return
        total = max(self.total_len, 1.0)
        local = profile_at((self.offset_m % total) / total, self.profile_rotation)
        self.offset_m += (self.speed_mps * local * TICK_SECONDS
                          * rng.uniform(0.9, 1.1))
        if self.offset_m >= self.total_len:
            self.offset_m = 0.0
            self.trip_seq += 1


# --------------------------------------------------------------------------
# the world
# --------------------------------------------------------------------------


class World:
    """Runs the simulation and emits protobuf snapshots."""

    def __init__(self, start_epoch: int, hours: float = 3.0, seed: int = 7,
                 itinerary_routes: tuple[str, ...] = ("61C", "28X")) -> None:
        self.rng = random.Random(seed)
        self.start = start_epoch
        self.ticks = int(hours * 3600 / TICK_SECONDS)
        self.routes = build_routes()
        self.itinerary_routes = itinerary_routes
        # Five vehicles per route. The detector latches one signal of each
        # kind per vehicle for the life of the run -- correct behaviour, since
        # re-alerting every 20s about the same stalled bus is exactly the
        # failure mode we are trying to avoid. But it means two scenarios
        # sharing a vehicle would make the second one invisible, so every
        # scenario below gets its own.
        self.vehicles: list[Vehicle] = []
        for route in self.routes:
            for n in range(5):
                self.vehicles.append(
                    Vehicle(
                        vehicle_id=f"{route.route_id}-{3000 + n}",
                        route=route,
                        offset_m=n * len(route.stops) * 95.0,
                        profile_rotation=len(self.vehicles) // 5,
                    )
                )
        self.scenarios: list[Scenario] = []
        self.pending_alerts: list[tuple[int, Scenario]] = []
        self._plan()

    # -- scenario planning ------------------------------------------------

    def _add(self, kind: str, veh: Vehicle, onset_min: float, duration_min: float,
             note: str, alert_lag_min: float | None = None) -> Scenario:
        onset = self.start + int(onset_min * 60)
        disruptive = kind in DISRUPTIVE
        sc = Scenario(
            scenario_id=f"sc{len(self.scenarios):03d}-{kind.lower()}",
            kind=kind,
            route_id=veh.route.route_id,
            vehicle_id=veh.vehicle_id,
            onset=onset,
            duration=int(duration_min * 60),
            disruptive=disruptive,
            on_itinerary=veh.route.route_id in self.itinerary_routes,
            official_alert_at=(
                onset + int((alert_lag_min or 0) * 60) if disruptive and alert_lag_min
                else None
            ),
            note=note,
        )
        self.scenarios.append(sc)
        if sc.official_alert_at:
            self.pending_alerts.append((sc.official_alert_at, sc))
        return sc

    def _plan(self) -> None:
        """Inject a spread of scenarios across routes, on and off itinerary.

        Alert lags are drawn from 11-24 minutes. That is the window we are
        claiming to beat, and it is the number a judge should push on: it is
        an assumption in the synthetic world, and the README says so.
        """
        by_route: dict[str, list[Vehicle]] = {}
        for v in self.vehicles:
            by_route.setdefault(v.route.route_id, []).append(v)

        # (kind, route, vehicle index, onset min, duration min, note)
        # Vehicle index is unique within each route. 61C and 28X are on the
        # itinerary; 71B and P1 are not, which is what makes the two
        # disruptive-but-irrelevant rows below the interesting negatives.
        plan = [
            # 61C -- on itinerary, 10 min headway
            ("STALL_DISABLED", "61C", 0, 18, 26, "disabled bus, mid-block on Forbes"),
            ("VANISHED_REAL", "61C", 1, 106, 27, "AVL dropout, never resumed trip"),
            ("TERMINAL_LAYOVER", "61C", 2, 12, 15, "scheduled recovery at terminal"),
            ("TIMEPOINT_HOLD", "61C", 3, 122, 10, "holding for schedule at timepoint"),
            ("FEED_ARTIFACT", "61C", 4, 46, 12, "AVL gap, reappears in place"),
            # 28X -- on itinerary, 30 min headway, airport run
            ("STALL_DISABLED", "28X", 0, 52, 31, "breakdown on the Parkway West"),
            ("BRIDGE_BLOCKAGE", "28X", 1, 132, 29, "blocked at the tunnel portal"),
            ("TERMINAL_LAYOVER", "28X", 2, 40, 18, "layover at the airport stand"),
            ("FEED_ARTIFACT", "28X", 3, 78, 11, "telemetry gap, no service impact"),
            ("GARAGE_PULLIN", "28X", 4, 150, 18, "pull-in, out of service"),
            # 71B -- not on the itinerary
            ("STALL_DISABLED", "71B", 0, 88, 24, "disabled coach, off itinerary"),
            ("TIMEPOINT_HOLD", "71B", 1, 58, 11, "holding at timepoint"),
            ("TERMINAL_LAYOVER", "71B", 2, 74, 14, "terminal recovery, Highland Park"),
            ("GARAGE_PULLIN", "71B", 3, 140, 20, "end of service, into garage"),
            # P1 -- not on the itinerary, 6 min headway
            ("VANISHED_REAL", "P1", 0, 64, 22, "vanished mid-busway, off itinerary"),
            ("TERMINAL_LAYOVER", "P1", 1, 96, 16, "layover at Swissvale"),
            ("TIMEPOINT_HOLD", "P1", 2, 30, 9, "timepoint hold, East Liberty"),
            ("FEED_ARTIFACT", "P1", 3, 114, 13, "feed hiccup"),
        ]

        seen: set[tuple[str, int]] = set()
        for kind, route_id, idx, onset, dur, note in plan:
            fleet = by_route.get(route_id)
            if not fleet:
                continue
            if (route_id, idx) in seen:
                raise AssertionError(
                    f"scenario collision on {route_id} vehicle {idx}: two "
                    "scenarios on one vehicle makes the second unobservable"
                )
            seen.add((route_id, idx))
            veh = fleet[idx % len(fleet)]
            lag = self.rng.uniform(11, 24) if kind in DISRUPTIVE else None
            self._add(kind, veh, onset, dur, note, lag)

    # -- per-tick application ---------------------------------------------

    def _mid_block(self, veh: Vehicle) -> float:
        """An offset halfway along a middle segment: between stops, not at one.

        A bus that breaks down does it wherever it happens to be, but a
        fixture has to be unambiguous. Vehicles wrap around their route, so
        without pinning, a "disabled mid-block" scenario can land on stop
        sequence 0 and be correctly read as a layover -- which tests nothing.
        """
        mid = max(1, min(len(veh.segment_len) // 2, len(veh.segment_len) - 1))
        return veh.offset_of_stop(mid) + veh.segment_len[mid] * 0.5

    def _apply(self, t: int) -> None:
        for sc in self.scenarios:
            veh = next(v for v in self.vehicles if v.vehicle_id == sc.vehicle_id)
            end = sc.onset + sc.duration

            # A vehicle that is about to disappear needs its last reported
            # position to be mid-route, otherwise the detector suppresses the
            # VANISHED signal as a terminal layover and the scenario is
            # unobservable for the wrong reason.
            if sc.kind in ("VANISHED_REAL", "FEED_ARTIFACT"):
                if sc.onset - 3 * TICK_SECONDS <= t < sc.onset:
                    veh.offset_m = self._mid_block(veh)
                    veh.force_stop_id = ""

            if not (sc.onset <= t < end):
                continue

            if sc.kind in ("STALL_DISABLED", "BRIDGE_BLOCKAGE"):
                veh.frozen_until = end
                veh.offset_m = self._mid_block(veh)
                veh.force_stop_id = ""  # disabled between stops, not dwelling
            elif sc.kind == "TERMINAL_LAYOVER":
                veh.frozen_until = end
                veh.offset_m = 0.0  # sit at the first terminal
                veh.force_stop_id = veh.route.terminals[0]
            elif sc.kind == "GARAGE_PULLIN":
                veh.frozen_until = end
                veh.offset_m = veh.total_len - 1.0
                veh.force_stop_id = veh.route.terminals[1]
            elif sc.kind == "TIMEPOINT_HOLD":
                # Pin it to a mid-route stop on purpose. The detector treats
                # stop_sequence <= 1 and terminal stops as layovers, so a hold
                # has to happen in the middle of the route to be the hard case
                # it is meant to be: a bus legitimately sitting still, at a
                # stop, nowhere near either end.
                veh.frozen_until = end
                mid = max(3, len(veh.route.stops) // 2)
                mid = min(mid, len(veh.route.stops) - 2)
                veh.offset_m = veh.offset_of_stop(mid)
                veh.force_stop_id = veh.route.stops[mid][0]
            elif sc.kind == "FEED_ARTIFACT":
                veh.hidden_until = end
                veh.frozen_until = end  # reappears where it vanished
                veh.jump_on_return_m = 40.0
            elif sc.kind == "VANISHED_REAL":
                veh.hidden_until = end
                veh.never_returns = True

    def snapshot(self, t: int) -> tuple[bytes, bytes]:
        """Return (vehiclePositions protobuf, alerts protobuf) for time t.

        Built through gtfsrt.encode_feed, the same code path the round-trip
        test exercises, so fixtures cannot drift from what the decoder expects.
        """
        self._apply(t)

        positions: list[gtfsrt.VehiclePos] = []
        for veh in self.vehicles:
            if t < veh.hidden_until:
                continue
            if veh.never_returns and t >= veh.hidden_until:
                continue  # stayed gone, which is the point of that class
            veh.step(t, self.rng)
            lat, lon, stop_id, seq = veh.position()
            stopped = t < veh.frozen_until
            positions.append(
                gtfsrt.VehiclePos(
                    entity_id=f"{veh.vehicle_id}@{t}",
                    vehicle_id=veh.vehicle_id,
                    trip=gtfsrt.TripRef(
                        trip_id=f"{veh.route.route_id}-T{veh.trip_seq:03d}",
                        route_id=veh.route.route_id,
                        direction_id=0,
                    ),
                    lat=lat + self.rng.gauss(0, 0.00004),
                    lon=lon + self.rng.gauss(0, 0.00004),
                    speed=0.0 if stopped else veh.speed_mps,
                    timestamp=t,
                    current_status=(
                        "STOPPED_AT" if (stopped and veh.force_stop_id)
                        else "IN_TRANSIT_TO"
                    ),
                    current_stop_sequence=seq,
                    stop_id=veh.force_stop_id or stop_id,
                )
            )

        alerts: list[gtfsrt.ServiceAlert] = []
        for published_at, sc in self.pending_alerts:
            if published_at <= t:
                alerts.append(
                    gtfsrt.ServiceAlert(
                        entity_id=f"alert-{sc.scenario_id}",
                        effect="SIGNIFICANT_DELAYS",
                        header=f"{sc.route_id}: delays",
                        description=(
                            f"Riders should expect delays on {sc.route_id}."
                        ),
                        route_ids=[sc.route_id],
                        active_start=sc.onset,
                    )
                )

        vehicles_pb = gtfsrt.encode_feed(
            gtfsrt.Feed(timestamp=t, version="2.0", vehicles=positions)
        )
        alerts_pb = gtfsrt.encode_feed(
            gtfsrt.Feed(timestamp=t, version="2.0", alerts=alerts)
        )
        return vehicles_pb, alerts_pb

    # -- output -----------------------------------------------------------

    def write(self, out_dir: str) -> dict:
        """Write the world, clearing any previous generation first.

        This clear is load-bearing. Snapshots are named by feed timestamp, so
        regenerating with a different --start leaves the old files in place
        and replay reads both timelines interleaved: vehicles teleport between
        generations, the stall anchor resets on every jump, and detections
        vanish. It presents as a quiet recall drop -- 1.00 to 0.50 on this set
        -- with no error anywhere. Found the hard way.
        """
        import shutil

        os.makedirs(out_dir, exist_ok=True)
        for stale in ("prt-bus", "prt-bus-alerts", "prt-rail", "prt-rail-alerts",
                      "amtrak"):
            shutil.rmtree(os.path.join(out_dir, stale), ignore_errors=True)
        for stale_file in ("truth.json", "gtfs-static.zip"):
            path = os.path.join(out_dir, stale_file)
            if os.path.exists(path):
                os.remove(path)

        veh_dir = os.path.join(out_dir, "prt-bus")
        alert_dir = os.path.join(out_dir, "prt-bus-alerts")
        os.makedirs(veh_dir, exist_ok=True)
        os.makedirs(alert_dir, exist_ok=True)

        for i in range(self.ticks):
            t = self.start + i * TICK_SECONDS
            vehicles_pb, alerts_pb = self.snapshot(t)
            with open(os.path.join(veh_dir, f"{t}.pb"), "wb") as fh:
                fh.write(vehicles_pb)
            with open(os.path.join(alert_dir, f"{t}.pb"), "wb") as fh:
                fh.write(alerts_pb)

        truth = {
            "start": self.start,
            "tick_seconds": TICK_SECONDS,
            "ticks": self.ticks,
            "itinerary_routes": list(self.itinerary_routes),
            "headways": {r.route_id: r.headway_min for r in self.routes},
            "scenarios": [s.to_dict() for s in self.scenarios],
        }
        with open(os.path.join(out_dir, "truth.json"), "w") as fh:
            json.dump(truth, fh, indent=2)
        self.write_static_gtfs(os.path.join(out_dir, "gtfs-static.zip"))
        return truth

    def write_static_gtfs(self, path: str) -> None:
        """A minimal static GTFS zip so the terminal-stop join has real input."""
        stops = io.StringIO()
        w = csv.writer(stops)
        w.writerow(["stop_id", "stop_name", "stop_lat", "stop_lon"])
        stop_times = io.StringIO()
        st = csv.writer(stop_times)
        st.writerow(["trip_id", "arrival_time", "departure_time", "stop_id",
                     "stop_sequence"])
        trips = io.StringIO()
        tw = csv.writer(trips)
        tw.writerow(["route_id", "service_id", "trip_id", "shape_id"])
        routes_csv = io.StringIO()
        rw = csv.writer(routes_csv)
        rw.writerow(["route_id", "route_short_name", "route_long_name",
                     "route_type", "route_color"])
        shapes_csv = io.StringIO()
        sw = csv.writer(shapes_csv)
        sw.writerow(["shape_id", "shape_pt_lat", "shape_pt_lon",
                     "shape_pt_sequence"])
        palette = ["0B5CD5", "0E7C5A", "B4231C", "6B3FD4"]

        for idx, route in enumerate(self.routes):
            rw.writerow([route.route_id, route.route_id,
                         f"{route.stops[0][0]} - {route.stops[-1][0]}",
                         3, palette[idx % len(palette)]])
            trip_id = f"{route.route_id}-T000"
            shape_id = f"{route.route_id}-SHP"
            tw.writerow([route.route_id, "WEEK", trip_id, shape_id])
            for seq, (_sid, lat, lon) in enumerate(route.stops):
                sw.writerow([shape_id, f"{lat:.6f}", f"{lon:.6f}", seq])
            for seq, (stop_id, lat, lon) in enumerate(route.stops):
                w.writerow([stop_id, f"{route.route_id} stop {seq}", f"{lat:.6f}",
                            f"{lon:.6f}"])
                clock = f"{6 + seq // 4:02d}:{(seq * 7) % 60:02d}:00"
                st.writerow([trip_id, clock, clock, stop_id, seq])

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("stops.txt", stops.getvalue())
            zf.writestr("stop_times.txt", stop_times.getvalue())
            zf.writestr("trips.txt", trips.getvalue())
            zf.writestr("routes.txt", routes_csv.getvalue())
            zf.writestr("shapes.txt", shapes_csv.getvalue())
            zf.writestr("agency.txt",
                        "agency_id,agency_name,agency_url,agency_timezone\n"
                        "PRT,Pittsburgh Regional Transit,"
                        "https://rideprt.org,America/New_York\n")
