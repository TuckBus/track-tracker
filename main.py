from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .flights import extract_flights
from .gtfs_static import load_gtfs
from .planner import plan
from .transit import extract_alerts, extract_vehicles, snapshot

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(
    title="SteelLink",
    description="Compile Pittsburgh International Airport boards and PRT TrueTime into one regional picture.",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "product": "SteelLink", "tracks": ["Beyond the chatbot", "Xtract", "Seed round"]}


@app.get("/api/flights")
def flights() -> dict:
    try:
        return extract_flights()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Flight extraction failed: {exc}") from exc


@app.get("/api/transit")
def transit() -> dict:
    try:
        return snapshot()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"TrueTime extraction failed: {exc}") from exc


@app.get("/api/vehicles")
def vehicles(mode: str = Query("bus", pattern="^(bus|train)$")) -> dict:
    return extract_vehicles(mode)


@app.get("/api/alerts")
def alerts(mode: str = Query("bus", pattern="^(bus|train)$")) -> dict:
    return extract_alerts(mode)


@app.get("/api/gtfs")
def gtfs() -> dict:
    data = load_gtfs()
    return {
        "routes": data["routes"],
        "flyer_stops": data["flyer_stops"],
    }


@app.get("/api/plan")
def plan_trip(q: str = Query(..., min_length=2, max_length=200)) -> dict:
    return plan(q)


@app.get("/api/extract")
def extract_pipeline() -> dict:
    flights = extract_flights()
    live = snapshot()
    return {
        "xtract": [
            {
                "source": "https://flypittsburgh.com/",
                "page": "Flight Status boards",
                "raw_form": "HTML tables",
                "records": flights["summary"]["arrivals"] + flights["summary"]["departures"],
                "sample": (flights["arrivals"][:1] + flights["departures"][:1]),
            },
            {
                "source": "https://truetime.rideprt.org/home",
                "page": "PRT TrueTime GTFS-RT",
                "raw_form": "protobuf vehicle, trip, and alert feeds",
                "records": live["buses"]["count"] + live["trains"]["count"],
                "sample": live["flyer_vehicles"][:2],
            },
        ],
        "compiled_fields": [
            "flight number, gate, bags, delay",
            "vehicle lat/lon, route, speed",
            "28X predictions at airport, downtown, Oakland",
        ],
    }
