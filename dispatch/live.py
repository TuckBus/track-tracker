"""Live mode: the whole PRT system, not one replayed corridor.

The replay path exists so the eval is reproducible and the demo is
deterministic. This is the other half: a background thread polling the real
feed so the map shows every bus and every light rail vehicle the agency is
currently reporting, around 600-700 of them across roughly a hundred routes.

Separate from the pipeline on purpose. The pipeline is stateful and is being
scored against ground truth; a live map is a read-only view of right now, and
mixing the two would mean a judge's map interaction could perturb the numbers.

No API key needed. PRT publishes GTFS-Realtime openly.
"""

from __future__ import annotations

import threading
import time

from . import gtfs_static
from .record import feeds_for_base, fetch
from .sources.base import TELEMETRY
from .sources.telemetry import PRT_BUS_ALERTS_URL, PRT_BUS_URL, PRT_RAIL_URL


class LiveFeed:
    """Latest system-wide snapshot, refreshed on a timer."""

    def __init__(self, interval: int = 20, base: str | None = None,
                 predictor=None) -> None:
        # Optional: keep an arrival predictor fed from the same polls, so the
        # arrivals board reflects the live system rather than a replay.
        self.predictor = predictor
        self.interval = interval
        self.lock = threading.Lock()
        self.vehicles: list[dict] = []
        self.alerts: list[dict] = []
        self.updated_at: int = 0
        self.error: str = ""
        self.polls = 0
        self.routes = gtfs_static.load()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        if base:
            f = feeds_for_base(base)
            self.sources = [(f["prt-bus"][0], "prt-bus"),
                            (f["prt-rail"][0], "prt-rail")]
            self.alert_url = f["prt-bus-alerts"][0]
        else:
            self.sources = [(PRT_BUS_URL, "prt-bus"), (PRT_RAIL_URL, "prt-rail")]
            self.alert_url = PRT_BUS_ALERTS_URL

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self.poll()  # one synchronous pass so the first page load has data
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception as exc:  # a poller must never take the server down
                with self.lock:
                    self.error = f"{type(exc).__name__}: {exc}"

    # -- polling ----------------------------------------------------------

    def poll(self) -> None:
        vehicles: list[dict] = []
        errors: list[str] = []

        observed = []
        for url, adapter in self.sources:
            src = TELEMETRY.get(adapter)
            if src is None:
                continue
            try:
                blob = fetch(url, timeout=15)
                obs, _alerts = src.parse(blob, int(time.time()))
            except Exception as exc:
                errors.append(f"{adapter}: {type(exc).__name__}")
                continue
            observed.extend(obs)
            for o in obs:
                if o.lat is None or o.lon is None:
                    continue
                vehicles.append({
                    "id": o.vehicle_id,
                    "route": o.route_id,
                    "lat": round(o.lat, 5),
                    "lon": round(o.lon, 5),
                    "status": o.status or "",
                    "stop": o.stop_id or "",
                    "trip": o.trip_id or "",
                    "at": o.observed_at,
                    "mode": "light rail" if adapter == "prt-rail" else "bus",
                })

        alerts: list[dict] = []
        try:
            src = TELEMETRY.get("prt-bus")
            blob = fetch(self.alert_url, timeout=15)
            _obs, raw = src.parse(blob, int(time.time()))
            seen: set[str] = set()
            for a in raw:
                if a.alert_id in seen:
                    continue
                seen.add(a.alert_id)
                alerts.append({
                    "id": a.alert_id, "header": a.header, "effect": a.effect,
                    "routes": a.route_ids, "published_at": a.published_at,
                })
        except Exception as exc:
            errors.append(f"alerts: {type(exc).__name__}")

        if self.predictor is not None and observed:
            try:
                self.predictor.observe(observed, int(time.time()))
            except Exception:
                pass  # a bad projection must not stop the poller

        with self.lock:
            # Keep the previous snapshot if this poll produced nothing. A blip
            # should not empty the map.
            if vehicles:
                self.vehicles = vehicles
                self.updated_at = int(time.time())
            self.alerts = alerts or self.alerts
            self.error = "; ".join(errors)
            self.polls += 1

    # -- read -------------------------------------------------------------

    def payload(self) -> dict:
        with self.lock:
            vehicles = list(self.vehicles)
            alerts = list(self.alerts)
            updated, error, polls = self.updated_at, self.error, self.polls

        disrupted: set[str] = set()
        for a in alerts:
            disrupted.update(a.get("routes") or [])

        counts: dict[str, int] = {}
        for v in vehicles:
            counts[v["route"]] = counts.get(v["route"], 0) + 1

        routes = []
        for rid, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            info = self.routes.get(rid)
            d = info.to_dict() if info else {
                "id": rid, "label": rid, "name": "", "color": "",
                "mode": "bus", "shape": []}
            d["vehicles"] = n
            d["disrupted"] = rid in disrupted
            routes.append(d)

        return {
            "mode": "live",
            "updated_at": updated,
            "polls": polls,
            "error": error,
            "vehicles": vehicles,
            "routes": routes,
            "alerts": alerts[:40],
            "gtfs": gtfs_static.summary(self.routes),
        }
