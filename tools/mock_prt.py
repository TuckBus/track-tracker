"""Serve a recorded or simulated world over HTTP as if it were PRT.

The recorder is the piece that has to work at the venue, so it needs a test
that does not depend on the venue's network. This replays a fixture world at
the same URL paths PRT uses, advancing one snapshot per request, so
`dispatch record` can be pointed at it and the whole fetch-store-replay loop
verified offline.

    python tools/mock_prt.py --world fixtures/world --port 8800
    python -m dispatch.cli record --out /tmp/rec --interval 1 --duration 10 \\
        --base http://127.0.0.1:8800

Paths mirror the real ones:
    /gtfsrt-bus/vehiclePositions
    /gtfsrt-bus/alerts
    /gtfsrt-train/vehiclePositions
    /gtfsrt-train/alerts
"""

from __future__ import annotations

import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORLD = {"dir": "fixtures/world", "cursor": 0, "loop": True}

ROUTES = {
    "/gtfsrt-bus/vehiclePositions": "prt-bus",
    "/gtfsrt-bus/alerts": "prt-bus-alerts",
    "/gtfsrt-train/vehiclePositions": "prt-bus",
    "/gtfsrt-train/alerts": "prt-bus-alerts",
}


def _frames(lane: str) -> list[str]:
    path = os.path.join(WORLD["dir"], lane)
    if not os.path.isdir(path):
        return []
    return sorted(
        (os.path.join(path, n) for n in os.listdir(path) if n.endswith(".pb")),
        key=lambda p: int(os.path.basename(p).split(".")[0]),
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        lane = ROUTES.get(path)
        if lane is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        frames = _frames(lane)
        if not frames:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Vehicle lanes advance the shared cursor; alert lanes follow it, so
        # the two feeds stay in step the way the real ones do.
        if "vehiclePositions" in path:
            WORLD["cursor"] += 1
        idx = WORLD["cursor"] % len(frames) if WORLD["loop"] else min(
            WORLD["cursor"], len(frames) - 1
        )
        with open(frames[idx], "rb") as fh:
            blob = fh.read()

        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


def serve(port: int, world: str, loop: bool = True) -> ThreadingHTTPServer:
    WORLD.update(dir=world, cursor=0, loop=loop)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--world", default="fixtures/world")
    ap.add_argument("--port", type=int, default=8800)
    args = ap.parse_args()
    httpd = serve(args.port, args.world)
    print(f"mock PRT on http://127.0.0.1:{args.port} serving {args.world}")
    for path in ROUTES:
        print(f"  http://127.0.0.1:{args.port}{path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
