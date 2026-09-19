"""Read PRT's static GTFS for the things the realtime feed does not carry.

The realtime feed gives you a route_id and a coordinate. It does not tell you
that `61C` is "McKeesport - Homestead", that the agency paints it a particular
colour, or where its line actually runs. All of that lives in the static
feed, which PRT publishes openly at

    https://www.rideprt.org/developerresources/GTFS.zip

Drop that file at fixtures/prt-gtfs.zip (or point DISPATCH_GTFS at it) and the
map draws the real system. Without it everything still works; routes simply
render as plain lines with their id as the label.

Parsing is deliberately tolerant. Agency feeds have BOMs, stray quoting,
missing optional columns and occasionally a shape with two points in it, and
none of that should take the map down.
"""

from __future__ import annotations

import csv
import io
import os
import zipfile
from dataclasses import dataclass, field

DEFAULT_PATHS = (
    os.environ.get("DISPATCH_GTFS", ""),
    "fixtures/prt-gtfs.zip",
    "fixtures/GTFS.zip",
    "GTFS.zip",
    # Last resort: the synthetic world writes a static feed of the same shape,
    # so a demo with no real GTFS downloaded still draws route lines.
    "fixtures/world/gtfs-static.zip",
)

# Route types we care about. 0 tram/light rail, 1 subway, 2 rail, 3 bus,
# 5 cable car, 6 gondola, 7 funicular (Pittsburgh's inclines).
TYPE_NAME = {0: "light rail", 1: "subway", 2: "rail", 3: "bus",
             5: "incline", 6: "gondola", 7: "incline"}


@dataclass
class RouteInfo:
    route_id: str
    short_name: str = ""
    long_name: str = ""
    color: str = ""
    text_color: str = ""
    route_type: int = 3
    shapes: list[list[tuple[float, float]]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.short_name or self.route_id

    @property
    def mode(self) -> str:
        return TYPE_NAME.get(self.route_type, "bus")

    def to_dict(self) -> dict:
        return {
            "id": self.route_id,
            "label": self.label,
            "name": self.long_name,
            "color": ("#" + self.color.lstrip("#")) if self.color else "",
            "mode": self.mode,
            # Longest shape only. A busy route can carry a dozen variants and
            # drawing them all turns the map into spaghetti; the longest is the
            # one a rider recognises as "the route".
            "shape": max(self.shapes, key=len) if self.shapes else [],
        }


def find_gtfs() -> str | None:
    for path in DEFAULT_PATHS:
        if path and os.path.exists(path):
            return path
    return None


def _rows(zf: zipfile.ZipFile, name: str):
    match = next((n for n in zf.namelist() if n.endswith(name)), None)
    if not match:
        return
    with zf.open(match) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")
        for row in csv.DictReader(text):
            yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def load(path: str | None = None, max_shape_points: int = 120) -> dict[str, RouteInfo]:
    """Return {route_id: RouteInfo}. Empty dict if no usable feed is present."""
    path = path or find_gtfs()
    if not path or not os.path.exists(path):
        return {}

    routes: dict[str, RouteInfo] = {}
    try:
        with zipfile.ZipFile(path) as zf:
            for r in _rows(zf, "routes.txt"):
                rid = r.get("route_id", "")
                if not rid:
                    continue
                try:
                    rtype = int(r.get("route_type") or 3)
                except ValueError:
                    rtype = 3
                routes[rid] = RouteInfo(
                    route_id=rid,
                    short_name=r.get("route_short_name", ""),
                    long_name=r.get("route_long_name", ""),
                    color=r.get("route_color", ""),
                    text_color=r.get("route_text_color", ""),
                    route_type=rtype,
                )

            # shape_id -> ordered points
            shapes: dict[str, list[tuple[int, float, float]]] = {}
            for r in _rows(zf, "shapes.txt"):
                sid = r.get("shape_id", "")
                try:
                    seq = int(float(r.get("shape_pt_sequence") or 0))
                    lat = float(r["shape_pt_lat"])
                    lon = float(r["shape_pt_lon"])
                except (KeyError, ValueError):
                    continue
                shapes.setdefault(sid, []).append((seq, lat, lon))

            # trips.txt joins routes to shapes
            seen: set[tuple[str, str]] = set()
            for r in _rows(zf, "trips.txt"):
                rid, sid = r.get("route_id", ""), r.get("shape_id", "")
                if not rid or not sid or (rid, sid) in seen:
                    continue
                seen.add((rid, sid))
                pts = shapes.get(sid)
                info = routes.get(rid)
                if not pts or info is None:
                    continue
                pts.sort()
                info.shapes.append(_thin([(la, lo) for _s, la, lo in pts],
                                         max_shape_points))
    except (OSError, zipfile.BadZipFile, KeyError):
        return routes

    return routes


def _thin(points: list[tuple[float, float]], limit: int) -> list[tuple[float, float]]:
    """Drop intermediate points so the payload stays small.

    A PRT shape can be a couple of thousand coordinates. At city zoom the
    difference between 2000 points and 120 is invisible, and the whole-system
    payload goes from tens of megabytes to something a browser will accept.
    Endpoints are always kept.
    """
    if len(points) <= limit:
        return points
    step = len(points) / float(limit)
    out = [points[int(i * step)] for i in range(limit)]
    out[-1] = points[-1]
    return out


def summary(routes: dict[str, RouteInfo]) -> str:
    if not routes:
        return ("no static GTFS found; routes will draw unlabelled. "
                "Download https://www.rideprt.org/developerresources/GTFS.zip "
                "to fixtures/prt-gtfs.zip")
    with_shape = sum(1 for r in routes.values() if r.shapes)
    return f"{len(routes)} routes, {with_shape} with geometry"
