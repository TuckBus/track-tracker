# SteelLink

Pittsburgh regional transit compiler for Steelhacks 2026. One site that extracts [FlyPittsburgh](https://flypittsburgh.com/) flight boards and [PRT TrueTime](https://truetime.rideprt.org/home) vehicle/prediction feeds, then connects PIT arrivals to the **28X Airport Flyer**.

Tracks this prototype is aimed at:

- **Xtract** — HTML flight tables and GTFS-Realtime protobuf become one JSON contract
- **Beyond the chatbot** — itineraries are grounded in those extracts, not a freeform model
- **Seed round** — a wedge product: gate → Flyer → downtown/Oakland

## Run it

```powershell
cd C:\Users\Shane\Downloads\SH-2026
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The first request downloads PRT’s public GTFS zip and caches a compact 28X stop file under `data/`.

## What is live

| Surface | Source |
| --- | --- |
| Arrivals / departures | `flypittsburgh.com` flight status HTML |
| Bus & rail positions, alerts | `truetime.portauthority.org/gtfsrt-*` |
| Airport Flyer stops | PRT static GTFS `google_transit.zip` |
| Connection planner | `/api/plan?q=UA%201285%20to%20Oakland` |

## Next for the weekend

- Persist favorite flights and campus stops
- Walk-time polygons from terminal door to 28X
- SMS when a delay means you should skip the bus currently at the curb
- Optional TrueTime developer key for stop-level Bustime predictions as a second source
