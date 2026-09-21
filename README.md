# Track Tracker

## Archived
This was a project created for Steelhacks XIII, and we have no intention on maintaining it. We had a blast, see you next time ;)
---


Track Tracker is a Vercel-native Next.js application for proactive Pittsburgh Regional Transit bus monitoring. The browser, API routes, Nemotron integration, PRT polling, and dashboard are deployed as one project.

See [LOCAL_DEVELOPMENT.md](./LOCAL_DEVELOPMENT.md) for complete local setup and testing instructions.

## Run locally

```powershell
npm install
npm run dev
```

The app runs at `http://localhost:3000`. Configure the services described in [DEPLOYMENT.md](./DEPLOYMENT.md) for live integrations.

## API

- `GET /api/state` returns normalized telemetry and action history.
- `POST /api/poll` runs an immediate poll.
- `POST /api/anomaly` checks a bus stop and line against the latest PRT telemetry.
- `GET /api/stops` returns the cached PRT GTFS stop catalog used by the searchable stop picker.
- `GET /api/cron/poll` is invoked by Vercel Cron once daily at 00:00 UTC on Hobby plans.
- `POST /api/actions/{id}/execute` executes a simulated email or calendar mitigation.

The dashboard includes a live coordinate map and a complete tracked-vehicle table. Provider and Nemotron requests emit structured JSON logs on the server; API keys are never included in those logs.
The status bar reports whether Nemotron is connected, unavailable with deterministic fallback active, or not configured.
