# Project Dispatch

Project Dispatch is a proactive Pittsburgh transit and logistics monitor. It polls PRT GTFS-RT, configurable Amtrak telemetry, and OpenSky state vectors, compiles observations into a strict action schema, and pushes actionable mitigations to a React dashboard over Server-Sent Events.

## Run locally

```powershell
python -m pip install -r requirements.txt
python -m backend
```

In another terminal:

```powershell
cd frontend
npm install
npm run dev
```

The dashboard runs at `http://localhost:5173`; the API runs at `http://localhost:8000`. Set `NEMOTRON_ENDPOINT`, `NEMOTRON_API_KEY`, and `AMTRAK_TELEMETRY_URL` to enable live integrations. Without credentials, Dispatch uses deterministic fallback logic and remains usable for demonstrations.

## API

- `GET /api/state` returns normalized telemetry and action history.
- `POST /api/poll` runs an immediate poll.
- `GET /api/events` streams state and action events via SSE.
- `POST /api/actions/{id}/execute` executes a simulated email or calendar mitigation.