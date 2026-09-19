"""Deterministic anomaly detection.

Nothing here involves a model. Distances, dwell times and schedule deviations
are arithmetic, and arithmetic is exactly what a language model is worst at
and least auditable doing. Detectors answer "what measurably happened";
triage answers "does this matter to this person right now". Keeping the two
apart is what makes the pipeline testable: replay a recording and you get
byte-identical Signals every time.

Detectors are intentionally sensitive. Over-firing here is cheap because
triage suppresses; under-firing is unrecoverable because nothing downstream
can detect what was never measured.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .schema import Observation, Provenance, Signal

# Tuning. Each threshold is a claim about the physical world, so each gets a
# comment justifying it rather than being a magic number.
STALL_RADIUS_M = 75.0  # GPS jitter on a stationary bus runs ~20-40m
STALL_SECONDS = 420  # 7 min: longer than any red light or dwell on PRT
SLIP_SECONDS = 480  # 8 min late is when a connection starts failing
SLIP_WORSENING_S = 120  # ... and still growing, not recovering
VANISH_SECONDS = 780  # 13 min. Was 600; the eval sweep showed 600 fired on
                      # 11-13 min AVL gaps that resolved themselves, and 780
                      # cleared them at zero recall cost. See EVAL.md.
CASCADE_MIN_VEHICLES = 2  # one late bus is traffic; two is the corridor
CASCADE_WINDOW_S = 900


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class VehicleState:
    """Rolling state for one vehicle across snapshots."""

    key: str
    anchor_lat: float | None = None
    anchor_lon: float | None = None
    anchor_at: int = 0
    last_seen_at: int = 0
    last_obs: Observation | None = None
    delay_history: list[tuple[int, int]] = field(default_factory=list)
    emitted: set[str] = field(default_factory=set)
    # Recent (t, lat, lon) samples. Needed because distance-from-anchor is a
    # bad stationarity measure: the anchor is the last position before the
    # vehicle stopped, so a bus decelerating into a stop shows tens of metres
    # of "drift" that is really residual approach distance. Spread *within*
    # the dwell window is the honest feature.
    recent: list[tuple[int, float, float]] = field(default_factory=list)


class Detector:
    """Stateful across a replay. `ingest` returns Signals newly crossing
    threshold at this snapshot -- never a repeat for the same condition."""

    def __init__(
        self,
        terminal_stops: set[str] | None = None,
        *,
        layover_aware: bool = True,
        stall_seconds: int = STALL_SECONDS,
        vanish_seconds: int = VANISH_SECONDS,
    ) -> None:
        self.states: dict[str, VehicleState] = {}
        # Thresholds are instance state, not module constants, so the eval can
        # sweep them instead of us asserting the defaults are right.
        self.stall_seconds = stall_seconds
        self.vanish_seconds = vanish_seconds
        # Stops where sitting still is the job, not a fault. Populated from
        # static GTFS; see load_terminal_stops().
        self.terminal_stops = terminal_stops or set()
        # Switch exists so the eval can measure what the layover join is
        # worth, rather than us claiming it.
        self.layover_aware = layover_aware
        self.route_slips: dict[str, list[tuple[int, str]]] = {}

    # -- helpers ----------------------------------------------------------

    def _prov(self, obs: Observation) -> Provenance:
        return Provenance(
            source_id=obs.source_type,
            source_type=obs.source_type,
            locator=f"entity {obs.raw_ref or obs.vehicle_id} @ {obs.observed_at}",
            retrieved_at=obs.observed_at,
            snippet=(
                f"{obs.route_id} veh {obs.vehicle_id} "
                f"({obs.lat:.5f},{obs.lon:.5f}) status={obs.status or 'n/a'}"
                if obs.lat is not None and obs.lon is not None
                else f"{obs.route_id} veh {obs.vehicle_id} status={obs.status or 'n/a'}"
            ),
        )

    def _jitter(self, st: VehicleState, since: int) -> float:
        """Largest gap between any two positions reported since `since`.

        A genuinely stationary vehicle reports a tight cluster (GPS noise
        only). A vehicle that crept forward, or that coasted to a halt part
        way through the window, reports a wide one.
        """
        pts = [(la, lo) for t, la, lo in st.recent if t >= since]
        if len(pts) < 2:
            return 0.0
        worst = 0.0
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                d = haversine_m(pts[i][0], pts[i][1], pts[j][0], pts[j][1])
                if d > worst:
                    worst = d
        return worst

    def _at_layover(self, obs: Observation) -> bool:
        if not self.layover_aware:
            return False
        if obs.stop_id and obs.stop_id in self.terminal_stops:
            return True
        # A bus at sequence 1 that has not started moving is laying over at
        # the top of the route, which every route does on every trip.
        if obs.stop_sequence is not None and obs.stop_sequence <= 1:
            return True
        return bool(obs.scheduled_layover)

    # -- main -------------------------------------------------------------

    def ingest(self, batch: list[Observation], now: int) -> list[Signal]:
        signals: list[Signal] = []
        for obs in batch:
            signals.extend(self._per_vehicle(obs, now))
        signals.extend(self._cascade(now))
        signals.extend(self._vanished(now))
        return signals

    def _per_vehicle(self, obs: Observation, now: int) -> list[Signal]:
        st = self.states.setdefault(obs.key, VehicleState(key=obs.key))
        out: list[Signal] = []

        if obs.status == "CANCELED":
            sig = self._emit(
                st, "CANCELED", obs, now, obs.observed_at, {"status": obs.status}
            )
            if sig:
                out.append(sig)

        # --- stall: has the anchor point held for long enough ------------
        if obs.lat is not None and obs.lon is not None:
            st.recent.append((obs.observed_at, obs.lat, obs.lon))
            st.recent = st.recent[-24:]
            if st.anchor_lat is None:
                st.anchor_lat, st.anchor_lon, st.anchor_at = (
                    obs.lat,
                    obs.lon,
                    obs.observed_at,
                )
            else:
                moved = haversine_m(st.anchor_lat, st.anchor_lon, obs.lat, obs.lon)
                if moved > STALL_RADIUS_M:
                    # Real movement: reset the anchor and clear the stall
                    # latch so a later stall on the same vehicle re-fires.
                    st.anchor_lat, st.anchor_lon, st.anchor_at = (
                        obs.lat,
                        obs.lon,
                        obs.observed_at,
                    )
                    st.emitted.discard("STALL")
                else:
                    dwell = obs.observed_at - st.anchor_at
                    if dwell >= self.stall_seconds and not self._at_layover(obs):
                        sig = self._emit(
                            st,
                            "STALL",
                            obs,
                            now,
                            st.anchor_at,
                            {
                                "dwell_s": dwell,
                                # distance from the anchor: includes the
                                # approach into the stop, so do not use it to
                                # judge whether the vehicle is stationary
                                "drift_m": round(moved, 1),
                                # spread of positions inside the dwell window:
                                # this is the stationarity feature
                                "jitter_m": round(self._jitter(st, st.anchor_at), 1),
                                "stop_id": obs.stop_id,
                                "stop_sequence": obs.stop_sequence,
                                "status": obs.status,
                            },
                        )
                        if sig:
                            out.append(sig)

        # --- schedule slip: late and getting later -----------------------
        if obs.delay_s is not None:
            st.delay_history.append((obs.observed_at, obs.delay_s))
            st.delay_history = st.delay_history[-12:]
            if obs.delay_s >= SLIP_SECONDS:
                # One sample cannot tell you a delay is growing. Firing on the
                # first reading made every already-late-but-recovering vehicle
                # a false positive; see the SLIP_RECOVERING row in EVAL.md.
                worsening = False
                if len(st.delay_history) >= 2:
                    earliest = st.delay_history[0][1]
                    worsening = (obs.delay_s - earliest) >= SLIP_WORSENING_S
                if worsening:
                    sig = self._emit(
                        st,
                        "SCHEDULE_SLIP",
                        obs,
                        now,
                        st.delay_history[0][0],
                        {
                            "delay_s": obs.delay_s,
                            "delay_start_s": st.delay_history[0][1],
                            "samples": len(st.delay_history),
                        },
                    )
                    if sig:
                        out.append(sig)
                    self.route_slips.setdefault(obs.route_id, []).append(
                        (obs.observed_at, obs.vehicle_id)
                    )

        st.last_seen_at = obs.observed_at
        st.last_obs = obs
        return out

    def _cascade(self, now: int) -> list[Signal]:
        """Several vehicles slipping on one route inside a window is a
        corridor problem, not bad luck. Different class, higher severity."""
        out = []
        for route, hits in self.route_slips.items():
            recent = {v for ts, v in hits if now - ts <= CASCADE_WINDOW_S}
            if len(recent) < CASCADE_MIN_VEHICLES:
                continue
            key = f"cascade:{route}"
            st = self.states.setdefault(key, VehicleState(key=key))
            if "CASCADE" in st.emitted:
                continue
            st.emitted.add("CASCADE")
            first = min(ts for ts, _ in hits)
            out.append(
                Signal(
                    kind="CASCADE",
                    route_id=route,
                    vehicle_id=",".join(sorted(recent)),
                    source_type="derived",
                    detected_at=now,
                    first_seen_at=first,
                    evidence={"vehicles": sorted(recent), "count": len(recent)},
                    provenance=[
                        Provenance(
                            source_id="derived",
                            source_type="derived",
                            locator=f"route {route}",
                            retrieved_at=now,
                            snippet=f"{len(recent)} vehicles slipping on {route} within "
                            f"{CASCADE_WINDOW_S // 60} min",
                        )
                    ],
                )
            )
        return out

    def _vanished(self, now: int) -> list[Signal]:
        out = []
        for st in list(self.states.values()):
            if st.last_obs is None or st.last_seen_at == 0:
                continue
            gap = now - st.last_seen_at
            if gap < self.vanish_seconds or "VANISHED" in st.emitted:
                continue
            obs = st.last_obs
            if self._at_layover(obs):
                continue
            st.emitted.add("VANISHED")
            out.append(
                Signal(
                    kind="VANISHED",
                    route_id=obs.route_id,
                    vehicle_id=obs.vehicle_id,
                    source_type=obs.source_type,
                    detected_at=now,
                    first_seen_at=st.last_seen_at,
                    evidence={"silent_s": gap, "last_stop": obs.stop_id},
                    provenance=[self._prov(obs)],
                )
            )
        return out

    def _emit(
        self,
        st: VehicleState,
        kind: str,
        obs: Observation,
        now: int,
        first_seen: int,
        evidence: dict,
    ) -> Signal | None:
        if kind in st.emitted:
            return None
        st.emitted.add(kind)
        return Signal(
            kind=kind,  # type: ignore[arg-type]
            route_id=obs.route_id,
            vehicle_id=obs.vehicle_id,
            source_type=obs.source_type,
            detected_at=now,
            first_seen_at=first_seen,
            evidence=evidence,
            provenance=[self._prov(obs)],
        )


def load_terminal_stops(gtfs_zip_path: str) -> set[str]:
    """Read stop_times.txt from a static GTFS zip and return stops that are
    first or last on any trip.

    This is the fix for the layover false-positive class described in
    EVAL.md. Without it, every bus at the top of its route reads as stalled.
    """
    import csv as _csv
    import io as _io
    import zipfile

    terminals: set[str] = set()
    try:
        with zipfile.ZipFile(gtfs_zip_path) as zf:
            name = next(
                (n for n in zf.namelist() if n.endswith("stop_times.txt")), None
            )
            if not name:
                return terminals
            with zf.open(name) as fh:
                text = _io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                per_trip: dict[str, list[tuple[int, str]]] = {}
                for row in _csv.DictReader(text):
                    trip = row.get("trip_id", "")
                    try:
                        seq = int(row.get("stop_sequence", "0") or 0)
                    except ValueError:
                        continue
                    per_trip.setdefault(trip, []).append((seq, row.get("stop_id", "")))
                for stops in per_trip.values():
                    stops.sort()
                    if stops:
                        terminals.add(stops[0][1])
                        terminals.add(stops[-1][1])
    except (OSError, zipfile.BadZipFile, KeyError):
        return terminals
    terminals.discard("")
    return terminals
