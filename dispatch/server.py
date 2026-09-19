"""Dashboard server. Standard library only -- no framework, no build step.

Serves one page and a small JSON API. Replays run in a background thread so
the board advances while you watch, which is how the demo works: a recorded
or synthetic session plays through at speed and findings land in real time.

    GET  /                      the board
    GET  /api/state             everything the board renders
    GET  /api/scenarios         what can be replayed
    POST /api/replay            start a replay  {scenario, speed}
    GET  /api/document/<id>     full source text, for provenance highlighting
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import sources
from .detect import Detector, load_terminal_stops
from .llm import get_backend
from .pipeline import Pipeline
from .schema import Document

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class Session:
    """Mutable shared state between the replay thread and HTTP handlers."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pipeline: Pipeline | None = None
        self.documents: dict[str, Document] = {}
        self.clock: int = 0
        self.status: str = "idle"
        self.scenario: str = ""
        self.speed: float = 60.0
        self.progress: tuple[int, int] = (0, 0)
        self.backend_name: str = "rules"

    # -- state for the UI ---------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            pipe = self.pipeline
            if pipe is None:
                return {
                    "status": self.status,
                    "scenario": self.scenario,
                    "clock": 0,
                    "backend": self.backend_name,
                    "progress": {"done": 0, "total": 0},
                    "findings": [],
                    "itinerary": [],
                    "alerts": [],
                    "drafts": [],
                    "stats": {},
                }
            findings = [f.to_dict() for f in pipe.findings]
            leads = [
                f.lead_time_s for f in pipe.findings if f.lead_time_s is not None
            ]
            acted = [f for f in pipe.findings if f.verdict.act in ("NOTIFY", "INTERVENE")]
            return {
                "status": self.status,
                "scenario": self.scenario,
                "clock": self.clock,
                "backend": self.backend_name,
                "progress": {"done": self.progress[0], "total": self.progress[1]},
                "findings": findings,
                "itinerary": [i.to_dict() for i in pipe.itinerary],
                "alerts": [
                    {
                        "alert_id": a.alert_id,
                        "published_at": a.published_at,
                        "header": a.header,
                        "effect": a.effect,
                        "cause": a.cause,
                        "routes": a.route_ids,
                    }
                    for a in pipe.official
                ],
                "drafts": pipe.drafts,
                "documents": [
                    {
                        "source_id": d.source_id,
                        "source_type": d.source_type,
                        "title": d.title,
                        "chars": len(d.text),
                    }
                    for d in self.documents.values()
                ],
                "stats": {
                    "signals": len(pipe.findings),
                    "surfaced": len(acted),
                    "suppressed": len(
                        [f for f in pipe.findings if f.verdict.act == "SUPPRESS"]
                    ),
                    "median_lead_s": (
                        sorted(leads)[len(leads) // 2] if leads else None
                    ),
                    "official_alerts": len(pipe.official),
                },
            }

    # -- replay -------------------------------------------------------

    def start_replay(self, world: str, speed: float = 60.0) -> None:
        """Stream a world or recording through the pipeline in a worker thread.

        Speed is a multiplier on feed time: 60 means one simulated minute per
        real second. The UI polls /api/state while this runs, so findings
        appear in the order the system could actually have known them --
        which is the point of a proactive tool and the thing a static
        screenshot cannot show.
        """
        from . import replay as replay_mod

        snapshots = replay_mod.load_snapshots(world)
        if not snapshots:
            raise KeyError(world)

        gtfs_zip = os.path.join(world, "gtfs-static.zip")
        terminals = (
            load_terminal_stops(gtfs_zip) if os.path.exists(gtfs_zip) else set()
        )
        backend = get_backend()
        docs = sources.load_documents(
            [p for p in replay_mod.default_docs(world) if os.path.isfile(p)]
        )
        pipe = Pipeline(
            backend=backend,
            detector=Detector(terminal_stops=terminals),
        )
        pipe.load_documents(docs)

        with self.lock:
            self.pipeline = pipe
            self.documents = {d.source_id: d for d in docs}
            self.scenario = os.path.basename(world.rstrip("/")) or world
            self.status = "running"
            self.speed = speed
            self.progress = (0, len(snapshots))
            self.backend_name = backend.name
            self.clock = snapshots[0].epoch

        def worker() -> None:
            prev = None
            for i, snap in enumerate(snapshots):
                if prev is not None and speed > 0:
                    time.sleep(min(1.5, max(0.0, (snap.epoch - prev) / speed)))
                prev = snap.epoch
                with self.lock:
                    pipe.observe(snap.observations, snap.alerts, snap.epoch)
                    if PRED is not None:
                        PRED.observe(snap.observations, snap.epoch)
                    self.clock = snap.epoch
                    self.progress = (i + 1, len(snapshots))
            with self.lock:
                pipe.reconcile_alerts()
                self.status = "complete"

        threading.Thread(target=worker, daemon=True).start()


def replay_vehicles() -> dict:
    """Map payload built from the replay, shaped exactly like live mode."""
    from .simulate import build_routes

    cat = catalog()
    shapes = {r.route_id: [[s[1], s[2]] for s in r.stops] for r in build_routes()}
    disrupted = {e["route"] for e in cat["events"]
                 if e.get("act") in ("NOTIFY", "INTERVENE")}
    routes = [{
        "id": r["id"], "label": r["id"], "name": r["subtitle"], "color": "",
        "mode": "bus", "shape": shapes.get(r["id"], []),
        "vehicles": r["vehicles"], "disrupted": r["id"] in disrupted,
    } for r in cat["routes"]]
    vehicles = [{
        "id": v["id"], "route": v["route"], "lat": v["lat"], "lon": v["lon"],
        "status": v.get("stop") and "STOPPED_AT" or "", "stop": v.get("stop", ""),
        "trip": "", "at": v.get("seen", 0), "mode": "bus",
    } for v in cat["vehicles"]]
    return {"mode": "replay", "updated_at": 0, "polls": 0, "error": "",
            "vehicles": vehicles, "routes": routes, "alerts": [],
            "gtfs": "replay geometry"}


def catalog() -> dict:
    """Everything searchable, in one payload.

    Sent whole rather than queried per keystroke: the dataset is small (tens
    of vehicles, a handful of legs and documents) and filtering in the browser
    is instant, which is the difference between search that feels native and
    search that feels like a web form. Every entry carries `source` so the UI
    can be honest about where it knows this from -- live telemetry, or a
    document you gave it.
    """
    from .simulate import build_routes

    with SESSION.lock:
        pipe = SESSION.pipeline
        docs = list(SESSION.documents.values())

    findings = pipe.findings if pipe else []
    legs = pipe.itinerary if pipe else []

    # live vehicles, from detector state
    vehicles: list[dict] = []
    if pipe:
        for key, st in pipe.detector.states.items():
            obs = st.last_obs
            if obs is None or key.startswith("cascade:"):
                continue
            vehicles.append({
                "kind": "vehicle", "id": obs.vehicle_id, "title": obs.vehicle_id,
                "subtitle": f"{obs.route_id} · {obs.status or 'in service'}",
                "route": obs.route_id, "mode": "bus",
                "lat": obs.lat, "lon": obs.lon, "stop": obs.stop_id,
                "seen": st.last_seen_at, "source": "live feed",
            })

    # routes, with how many vehicles and events each has right now
    per_route: dict[str, int] = {}
    for v in vehicles:
        per_route[v["route"]] = per_route.get(v["route"], 0) + 1
    events_by_route: dict[str, int] = {}
    for f in findings:
        events_by_route[f.signal.route_id] = (
            events_by_route.get(f.signal.route_id, 0) + 1)

    routes = []
    for r in build_routes():
        routes.append({
            "kind": "route", "id": r.route_id, "title": r.route_id,
            "subtitle": f"{r.stops[0][0]} to {r.stops[-1][0]}",
            "mode": "bus", "headway_min": r.headway_min,
            "vehicles": per_route.get(r.route_id, 0),
            "events": events_by_route.get(r.route_id, 0),
            "stops": [s[0] for s in r.stops],
            # Geometry for the map. Sent as coordinates rather than rendered
            # tiles: no API key, no tile server, and it still draws when the
            # venue wifi is gone.
            "coords": [[s[1], s[2]] for s in r.stops],
            "source": "live feed",
        })

    # itinerary legs: these are where trains and flights come from
    mode_of = {"transit": "bus", "rail": "train", "flight": "flight",
               "appointment": "event"}
    leg_rows = []
    for leg in legs:
        prov = leg.provenance[0].source_id if leg.provenance else ""
        leg_rows.append({
            "kind": "leg", "id": leg.item_id, "title": leg.label,
            "subtitle": (f"{leg.route_hint} · " if leg.route_hint else "")
                        + (prov or "your documents"),
            "mode": mode_of.get(leg.kind, "event"), "route": leg.route_hint,
            "depart_at": leg.depart_at, "headway_min": leg.headway_min,
            "document": prov, "source": "your documents",
        })

    doc_rows = [{
        "kind": "document", "id": d.source_id, "title": d.source_id,
        "subtitle": f"{d.source_type} · {len(d.text)} characters",
        "mode": "document", "source": "your documents",
    } for d in docs]

    event_rows = [{
        "kind": "event", "id": f.signal.signal_id,
        "title": f"{f.signal.kind} on {f.signal.route_id}",
        "subtitle": f"{f.verdict.act} · {f.verdict.reason_code}",
        "mode": "bus", "route": f.signal.route_id,
        "act": f.verdict.act, "at": f.signal.detected_at,
        "source": "live feed",
    } for f in findings]

    return {"routes": routes, "vehicles": vehicles, "legs": leg_rows,
            "documents": doc_rows, "events": event_rows}


def list_worlds() -> list[dict]:
    """Anything on disk with a prt-bus lane is replayable."""
    roots = ["fixtures", "recordings", "."]
    out: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isdir(path) or path in seen:
                continue
            lane = os.path.join(path, "prt-bus")
            if not os.path.isdir(lane):
                continue
            seen.add(path)
            frames = len([f for f in os.listdir(lane) if f.endswith(".pb")])
            labelled = os.path.exists(os.path.join(path, "truth.json"))
            out.append({
                "name": path,
                "family": "synthetic (labelled)" if labelled else "recording",
                "source": "prt-bus",
                "note": (
                    "has ground truth, scorable with `dispatch eval`"
                    if labelled else "live capture, no ground truth"
                ),
                "gold_act": "",
                "snapshots": frames,
            })
    return out


def _load_fixture_docs() -> list[Document]:
    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures",
        "docs",
    )
    if not os.path.isdir(root):
        return []
    paths = [os.path.join(root, f) for f in sorted(os.listdir(root))]
    return sources.load_documents([p for p in paths if os.path.isfile(p)])


DEFAULT_WORLD = "fixtures/world"
LIVE = None  # a live.LiveFeed when serve(--live) is on
PRED = None  # a predict.Predictor, fed by whichever source is running

SESSION = Session()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, b"index.html missing", "text/plain")
            return
        if path == "/api/state":
            self._json(SESSION.snapshot())
            return
        if path == "/api/scenarios":
            self._json(list_worlds())
            return
        if path == "/api/eval":
            # Serve the last scored run. The evidence belongs in the product,
            # not only in a markdown file nobody opens during a demo.
            try:
                with open("EVAL.json") as fh:
                    self._json({"arms": json.load(fh)})
            except (OSError, ValueError):
                self._json({"arms": [], "note": "run: dispatch eval"})
            return
        if path == "/api/stops":
            self._json({"stops": (PRED.all_stops() if PRED else [])})
            return
        if path.startswith("/api/arrivals"):
            from urllib.parse import parse_qs as _q
            qs = _q(urlparse(self.path).query)
            stop = (qs.get("stop") or [""])[0]
            # `exclude` drops a specific vehicle from the board. Used when one
            # has broken down: the useful answer is not "no ETA" but "that one
            # is stuck, here is the next one behind it".
            skip = set(qs.get("exclude") or [])
            if PRED is None or not stop:
                self._json({"stop": stop, "arrivals": []})
                return
            rows = [a.to_dict() for a in PRED.arrivals(stop, limit=8)
                    if a.vehicle_id not in skip]
            self._json({"stop": stop, "arrivals": rows[:6]})
            return
        if path == "/api/vehicles":
            # Whole-system live view when live mode is on; otherwise the
            # replayed corridor, so the map works either way.
            if LIVE is not None:
                self._json(LIVE.payload())
                return
            self._json(replay_vehicles())
            return
        if path == "/api/catalog":
            self._json(catalog())
            return
        if path == "/api/truth":
            from .replay import load_truth
            self._json(load_truth(DEFAULT_WORLD) or {})
            return
        if path.startswith("/api/document/"):
            doc_id = path[len("/api/document/") :]
            with SESSION.lock:
                doc = SESSION.documents.get(doc_id)
            if doc is None:
                self._json({"error": "unknown document"}, 404)
                return
            self._json(
                {
                    "source_id": doc.source_id,
                    "source_type": doc.source_type,
                    "title": doc.title,
                    "text": doc.text,
                }
            )
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = parse_qs(raw.decode(errors="replace"))
            body = {k: v[0] for k, v in body.items()}
        if path == "/api/upload":
            # Drop a real confirmation email, ticket or boarding pass in and
            # watch the adapter layer handle it. This is the Xtract claim made
            # testable by whoever is holding the laptop.
            import base64
            from .sources.base import document_source_for

            name = str(body.get("name") or "upload.txt")
            blob = b""
            if body.get("b64"):
                try:
                    blob = base64.b64decode(body["b64"])
                except Exception:
                    self._json({"error": "could not decode that file"}, 400)
                    return
            elif body.get("text"):
                blob = str(body["text"]).encode("utf-8")
            if not blob:
                self._json({"error": "empty file"}, 400)
                return
            if len(blob) > 2_000_000:
                self._json({"error": "file too large (2MB limit)"}, 400)
                return

            src = document_source_for(name)
            if src is None:
                self._json({"error": "no adapter for that file type"}, 400)
                return
            doc = src.parse(blob, name, int(time.time()))

            with SESSION.lock:
                pipe = SESSION.pipeline
                if pipe is None:
                    self._json({"error": "start a replay first"}, 409)
                    return
                legs = pipe.backend.extract(doc)
                known = {i.item_id for i in pipe.itinerary}
                added = [l for l in legs if l.item_id not in known]
                pipe.itinerary = sorted(
                    pipe.itinerary + added,
                    key=lambda i: (i.depart_at or 1 << 62))
                SESSION.documents[doc.source_id] = doc
            self._json({
                "document": {"source_id": doc.source_id,
                             "source_type": doc.source_type,
                             "chars": len(doc.text)},
                "legs": [l.to_dict() for l in added],
                "found": len(legs), "added": len(added),
            })
            return
        if path == "/api/replay":
            name = body.get("scenario") or DEFAULT_WORLD
            speed = float(body.get("speed") or 60)
            try:
                SESSION.start_replay(name, speed)
            except KeyError:
                self._json({"error": f"unknown scenario {name}"}, 400)
                return
            self._json({"status": "running", "scenario": name, "speed": speed})
            return
        self._json({"error": "not found"}, 404)


def serve(
    world: str = DEFAULT_WORLD,
    *,
    port: int = 8000,
    host: str = "127.0.0.1",
    db: str | None = None,
    open_browser: bool = False,
    live: bool = False,
    base: str | None = None,
) -> None:
    global DEFAULT_WORLD, LIVE, PRED
    DEFAULT_WORLD = world

    # One predictor, fed by whichever source is running. Geometry comes from
    # static GTFS if it is present, otherwise from the world's own feed.
    from .predict import from_static_gtfs
    import os as _os
    gtfs = None
    for cand in ("fixtures/prt-gtfs.zip", _os.path.join(world, "gtfs-static.zip")):
        if _os.path.exists(cand):
            gtfs = cand
            break
    PRED = from_static_gtfs(gtfs)
    stops = len(PRED.all_stops())
    print(f"predictions: {len(PRED.geo)} routes with geometry, {stops} stops"
          + ("" if PRED.geo else " (run `dispatch gtfs` for real geometry)"))
    if live:
        from .live import LiveFeed
        LIVE = LiveFeed(base=base, predictor=PRED)
        LIVE.start()
        p = LIVE.payload()
        print(f"live mode: {len(p['vehicles'])} vehicles, "
              f"{len(p['routes'])} routes")
        print(f"  static GTFS: {p['gtfs']}")
        if p["error"]:
            print(f"  feed trouble: {p['error']}")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Dispatch board: http://{host}:{port}")
    print(f"default world: {world}")
    print("Pick a world on the page, or POST /api/replay to start one.")
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.shutdown()
