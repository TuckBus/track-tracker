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
                    self.clock = snap.epoch
                    self.progress = (i + 1, len(snapshots))
            with self.lock:
                pipe.reconcile_alerts()
                self.status = "complete"

        threading.Thread(target=worker, daemon=True).start()


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
) -> None:
    global DEFAULT_WORLD
    DEFAULT_WORLD = world
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
