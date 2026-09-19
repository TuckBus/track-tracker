"""Arrival prediction: when will this bus reach that stop.

Deliberately built on realtime data plus route geometry, not on the published
timetable. Two reasons. A schedule tells you when a bus was *supposed* to be
somewhere, which is exactly the thing that stops being true the moment
anything goes wrong; and the realtime feed plus shapes is available for every
PRT route right now, whereas a usable schedule join needs trip ids that line up
between the static and realtime feeds, which agencies get wrong more often than
you would hope.

How it works, in one line: project each vehicle onto its route's shape, measure
how fast it is actually covering ground, and divide the remaining distance to
each upcoming stop by that speed.

The interesting part is where it fails, and it fails in a specific, honest way.
Speed-based ETAs are good while a vehicle is moving and catastrophically wrong
the moment it stops, because a stationary bus has an infinite ETA. Rather than
hide that behind a clamp, the predictor reports `basis="stalled"` and declines
to give a number. That is the whole argument for Dispatch having a separate
disruption detector: prediction degrades exactly when you most need to know
something is wrong, so detecting the stall cannot be left to the ETA model.
See EVAL.md for the measured error, split by whether the vehicle was disrupted.
"""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass, field

EARTH_R = 6_371_000.0

# Tuning. Each is a claim about buses, so each gets a reason.
MIN_SAMPLES = 3            # two points give a speed; three give a believable one
SPEED_WINDOW_S = 240       # four minutes of history: long enough to smooth
                           # traffic lights, short enough to notice congestion
MIN_SPEED_MPS = 1.4        # ~5 km/h. Below this we are not predicting, we are
                           # extrapolating a stopped vehicle into next week
STALL_SPEED_MPS = 0.5      # effectively stationary
SNAP_TOLERANCE_M = 400     # further than this from the shape and we do not
                           # believe the vehicle is on this route
TRIP_RESET_M = 600         # a big backwards jump means a new trip, not reverse

# Segment speed model. A route is not one speed: the downtown end of the 61C
# crawls and the busway end does not, so extrapolating a vehicle's current
# speed across the whole remaining trip is wrong in a way that grows with
# distance. Splitting the route into bins and learning each bin's typical speed
# from vehicles that have already driven it fixes that.
BIN_M = 400.0              # ~2 city blocks: fine enough to separate a congested
                           # stretch from a clear one, coarse enough to fill up
SEG_ALPHA = 0.25           # EMA weight on each new observation
SEG_MIN_OBS = 2            # below this a bin is a guess, not a measurement
SEG_MIN_COVERAGE = 0.6     # need this much of the route ahead measured before
                           # trusting the segment model over current speed

# Beyond this, stop giving numbers. Chosen from the measured p90 error, not
# taste: a 20-minute cutoff takes p90 from 120s to 77s for about five points
# of extra refusals. See the horizon sweep in PREDICTIONS.md.
DEFAULT_HORIZON_S = 1200


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


@dataclass
class RouteGeometry:
    """A route's shape, with cumulative distances and stops projected onto it."""

    route_id: str
    points: list[tuple[float, float]]
    cum: list[float] = field(default_factory=list)
    stops: list[tuple[str, float, float, float]] = field(default_factory=list)
    # (stop_id, lat, lon, distance_along_shape)

    def __post_init__(self) -> None:
        self.cum = [0.0]
        for i in range(1, len(self.points)):
            a, b = self.points[i - 1], self.points[i]
            self.cum.append(self.cum[-1] + haversine_m(a[0], a[1], b[0], b[1]))

    @property
    def length(self) -> float:
        return self.cum[-1] if self.cum else 0.0

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """Return (distance along shape, distance from shape) in metres.

        Brute force over segments. A PRT shape is thinned to ~120 points and a
        poll has a few hundred vehicles, so this is tens of thousands of cheap
        operations per poll -- not worth a spatial index.
        """
        best_along, best_off = 0.0, float("inf")
        for i in range(len(self.points) - 1):
            ax, ay = self.points[i]
            bx, by = self.points[i + 1]
            # Work in a local planar frame; at segment scale this is exact enough.
            k = math.cos(math.radians(ax))
            vx, vy = (bx - ax), (by - ay) * k
            px, py = (lat - ax), (lon - ay) * k
            seg2 = vx * vx + vy * vy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (px * vx + py * vy) / seg2))
            cx, cy = ax + vx * t, ay + (vy * t) / (k or 1.0)
            off = haversine_m(lat, lon, cx, cy)
            if off < best_off:
                best_off = off
                seg_len = self.cum[i + 1] - self.cum[i]
                best_along = self.cum[i] + seg_len * t
        return best_along, best_off

    def attach_stops(self, stops: list[tuple[str, float, float]]) -> None:
        out = []
        for sid, lat, lon in stops:
            along, off = self.project(lat, lon)
            if off <= SNAP_TOLERANCE_M:
                out.append((sid, lat, lon, along))
        out.sort(key=lambda s: s[3])
        self.stops = out

    def stops_ahead(self, along: float, limit: int = 6) -> list[tuple[str, float]]:
        """Upcoming stops as (stop_id, metres away)."""
        dists = [s[3] for s in self.stops]
        i = bisect.bisect_right(dists, along)
        return [(self.stops[j][0], self.stops[j][3] - along)
                for j in range(i, min(i + limit, len(self.stops)))]


# --------------------------------------------------------------------------
# prediction
# --------------------------------------------------------------------------


@dataclass
class Sample:
    at: int
    along: float


@dataclass
class VehicleTrack:
    vehicle_id: str
    route_id: str
    samples: list[Sample] = field(default_factory=list)
    along: float = 0.0
    off_route_m: float = 0.0
    trips: int = 0

    def speed_mps(self) -> tuple[float, int]:
        """Average ground speed over the recent window, and sample count used."""
        if len(self.samples) < 2:
            return 0.0, len(self.samples)
        newest = self.samples[-1]
        window = [s for s in self.samples if newest.at - s.at <= SPEED_WINDOW_S]
        if len(window) < 2:
            window = self.samples[-2:]
        dt = window[-1].at - window[0].at
        if dt <= 0:
            return 0.0, len(window)
        return max(0.0, (window[-1].along - window[0].along) / dt), len(window)


@dataclass
class SegmentSpeeds:
    """Learned typical speed per distance-bin of one route."""

    route_id: str
    length: float
    speeds: list[float] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        n = max(1, int(math.ceil(self.length / BIN_M)))
        self.speeds = [0.0] * n
        self.counts = [0] * n

    def index(self, along: float) -> int:
        return max(0, min(len(self.speeds) - 1, int(along / BIN_M)))

    def record(self, from_along: float, to_along: float, dt: float) -> None:
        """Fold one observed span into every bin it crossed."""
        if dt <= 0 or to_along <= from_along:
            return
        speed = (to_along - from_along) / dt
        if speed <= 0 or speed > 35:   # 126 km/h: a bad projection, not a bus
            return
        # Half-open: a span ending exactly on a bin boundary must not be
        # credited to the bin it never entered. Without this, a slow stretch
        # bleeds one bin forward and the learned profile is smeared.
        last = self.index(max(from_along, to_along - 1e-6))
        for i in range(self.index(from_along), last + 1):
            if self.counts[i] == 0:
                self.speeds[i] = speed
            else:
                self.speeds[i] += SEG_ALPHA * (speed - self.speeds[i])
            self.counts[i] += 1

    def coverage(self, from_along: float, to_along: float) -> float:
        lo, hi = self.index(from_along), self.index(to_along)
        known = sum(1 for i in range(lo, hi + 1)
                    if self.counts[i] >= SEG_MIN_OBS)
        return known / max(1, hi - lo + 1)

    def travel_time(self, from_along: float, to_along: float,
                    fallback_mps: float) -> float:
        """Integrate bin-by-bin rather than assuming one speed for the lot."""
        total, pos = 0.0, from_along
        while pos < to_along:
            i = self.index(pos)
            edge = min(to_along, (i + 1) * BIN_M)
            if edge <= pos:
                break
            v = self.speeds[i] if self.counts[i] >= SEG_MIN_OBS else fallback_mps
            total += (edge - pos) / max(v, MIN_SPEED_MPS)
            pos = edge
        return total


@dataclass
class Prediction:
    stop_id: str
    route_id: str
    vehicle_id: str
    eta_s: int | None        # None when we decline to predict
    arrives_at: int | None
    distance_m: float
    basis: str               # segment | speed | stalled | insufficient_data
                             # | off_route | beyond_horizon
    confidence: float
    speed_mps: float

    def to_dict(self) -> dict:
        return {
            "stop_id": self.stop_id, "route": self.route_id,
            "vehicle": self.vehicle_id, "eta_s": self.eta_s,
            "arrives_at": self.arrives_at,
            "distance_m": round(self.distance_m),
            "basis": self.basis, "confidence": round(self.confidence, 2),
            "speed_mps": round(self.speed_mps, 2),
        }


class Predictor:
    """Stateful across polls. Feed it Observations, ask it for arrivals."""

    def __init__(self, geometries: dict[str, RouteGeometry] | None = None,
                 mode: str = "segment", horizon_s: int | None = None) -> None:
        self.geo: dict[str, RouteGeometry] = geometries or {}
        self.tracks: dict[str, VehicleTrack] = {}
        # "segment" integrates learned per-bin speeds; "speed" is the simpler
        # constant-speed extrapolation. Both are scored in PREDICTIONS.md.
        self.mode = mode
        # Refuse past this horizon rather than emit a number we have measured
        # ourselves to be unreliable. None means predict at any range.
        self.horizon_s = horizon_s
        self.segments: dict[str, SegmentSpeeds] = {}

    # -- ingest -----------------------------------------------------------

    def observe(self, observations, now: int | None = None) -> None:
        now = now or int(time.time())
        for obs in observations:
            if obs.lat is None or obs.lon is None:
                continue
            geo = self.geo.get(obs.route_id)
            if geo is None or len(geo.points) < 2:
                continue
            along, off = geo.project(obs.lat, obs.lon)
            key = f"{obs.route_id}:{obs.vehicle_id}"
            tr = self.tracks.get(key)
            if tr is None:
                tr = VehicleTrack(vehicle_id=obs.vehicle_id, route_id=obs.route_id)
                self.tracks[key] = tr
            # A large backwards jump is the vehicle starting its next trip, not
            # driving in reverse. Reset history so the speed estimate does not
            # go negative for the next four minutes.
            if tr.samples and along < tr.along - TRIP_RESET_M:
                tr.samples.clear()
                tr.trips += 1
            # Learn the route's speed profile from this span before moving on.
            t_now = obs.observed_at or now
            if tr.samples:
                prev = tr.samples[-1]
                seg = self.segments.get(obs.route_id)
                if seg is None:
                    seg = SegmentSpeeds(route_id=obs.route_id, length=geo.length)
                    self.segments[obs.route_id] = seg
                seg.record(prev.along, along, t_now - prev.at)
            tr.along, tr.off_route_m = along, off
            tr.samples.append(Sample(at=t_now, along=along))
            if len(tr.samples) > 40:
                tr.samples = tr.samples[-40:]

    # -- predict ----------------------------------------------------------

    def predict_vehicle(self, key: str, limit: int = 6,
                        now: int | None = None) -> list[Prediction]:
        now = now or int(time.time())
        tr = self.tracks.get(key)
        if tr is None:
            return []
        geo = self.geo.get(tr.route_id)
        if geo is None:
            return []

        speed, n = tr.speed_mps()
        ahead = geo.stops_ahead(tr.along, limit)

        if tr.off_route_m > SNAP_TOLERANCE_M:
            basis, usable = "off_route", False
        elif n < MIN_SAMPLES:
            basis, usable = "insufficient_data", False
        elif speed < STALL_SPEED_MPS:
            basis, usable = "stalled", False
        else:
            basis, usable = "speed", True

        seg = self.segments.get(tr.route_id)
        out: list[Prediction] = []
        for stop_id, dist in ahead:
            eta, how = None, basis
            if usable:
                target = tr.along + dist
                use_seg = (self.mode == "segment" and seg is not None
                           and seg.coverage(tr.along, target) >= SEG_MIN_COVERAGE)
                if use_seg:
                    eta = int(seg.travel_time(tr.along, target, speed))
                    how = "segment"
                else:
                    eta = int(dist / max(speed, MIN_SPEED_MPS))
                    how = "speed"
                if self.horizon_s is not None and eta > self.horizon_s:
                    # We measured our own error at long range and it was poor.
                    # Saying nothing beats saying something wrong.
                    eta, how = None, "beyond_horizon"
            # Confidence decays with horizon and rises with sample count. A
            # 40-minute ETA from a bus we have watched for one minute is not
            # worth the same as a 3-minute one from ten samples.
            conf = 0.0
            if eta is not None:
                horizon = min(1.0, 900.0 / max(eta, 1))
                depth = min(1.0, n / 8.0)
                conf = round(0.30 + 0.40 * horizon + 0.15 * depth
                             + (0.15 if how == "segment" else 0.0), 3)
            out.append(Prediction(
                stop_id=stop_id, route_id=tr.route_id, vehicle_id=tr.vehicle_id,
                eta_s=eta, arrives_at=(now + eta) if eta is not None else None,
                distance_m=dist, basis=how, confidence=min(conf, 0.95),
                speed_mps=speed,
            ))
        return out

    def arrivals(self, stop_id: str, limit: int = 5,
                 now: int | None = None) -> list[Prediction]:
        """The board a rider actually wants: next arrivals at one stop."""
        now = now or int(time.time())
        found: list[Prediction] = []
        for key, tr in self.tracks.items():
            geo = self.geo.get(tr.route_id)
            if geo is None or not any(s[0] == stop_id for s in geo.stops):
                continue
            for p in self.predict_vehicle(key, limit=8, now=now):
                if p.stop_id == stop_id:
                    found.append(p)
                    break
        # Predictable arrivals first, soonest first; the ones we declined to
        # predict go last rather than being dropped, because "a bus is coming
        # but it is not moving" is information.
        found.sort(key=lambda p: (p.eta_s is None, p.eta_s if p.eta_s else 0))
        return found[:limit]

    def all_stops(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for rid, geo in self.geo.items():
            for sid, lat, lon, _along in geo.stops:
                row = seen.setdefault(sid, {"stop_id": sid, "lat": lat,
                                            "lon": lon, "routes": []})
                if rid not in row["routes"]:
                    row["routes"].append(rid)
        return sorted(seen.values(), key=lambda s: s["stop_id"])


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def from_static_gtfs(path: str | None = None, mode: str = "segment",
                     horizon_s: int | None = DEFAULT_HORIZON_S) -> Predictor:
    """Build a predictor from a static GTFS zip (shapes + stops + stop_times)."""
    import csv
    import io
    import zipfile

    from . import gtfs_static

    path = path or gtfs_static.find_gtfs()
    routes = gtfs_static.load(path)
    geo: dict[str, RouteGeometry] = {}
    for rid, info in routes.items():
        shape = info.to_dict()["shape"]
        if len(shape) >= 2:
            geo[rid] = RouteGeometry(route_id=rid,
                                     points=[(p[0], p[1]) for p in shape])
    if not geo or not path:
        return Predictor(geo, mode=mode, horizon_s=horizon_s)

    # stop coordinates, then which stops belong to which route via stop_times
    stops: dict[str, tuple[float, float]] = {}
    trip_route: dict[str, str] = {}
    route_stops: dict[str, list[str]] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            def rows(name):
                match = next((n for n in zf.namelist() if n.endswith(name)), None)
                if not match:
                    return
                with zf.open(match) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8-sig",
                                            errors="replace")
                    for r in csv.DictReader(text):
                        yield r

            for r in rows("stops.txt"):
                try:
                    stops[r["stop_id"]] = (float(r["stop_lat"]),
                                           float(r["stop_lon"]))
                except (KeyError, ValueError):
                    continue
            for r in rows("trips.txt"):
                trip_route[r.get("trip_id", "")] = r.get("route_id", "")
            for r in rows("stop_times.txt"):
                rid = trip_route.get(r.get("trip_id", ""))
                sid = r.get("stop_id", "")
                if not rid or not sid:
                    continue
                lst = route_stops.setdefault(rid, [])
                if sid not in lst:
                    lst.append(sid)
    except (OSError, zipfile.BadZipFile, KeyError):
        return Predictor(geo, mode=mode, horizon_s=horizon_s)

    for rid, g in geo.items():
        ids = route_stops.get(rid) or []
        g.attach_stops([(sid, *stops[sid]) for sid in ids if sid in stops])
    return Predictor(geo, mode=mode, horizon_s=horizon_s)
