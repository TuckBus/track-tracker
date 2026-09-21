# Track Tracker
Track Tracker is a Vercel-native Next.js application for proactive Pittsburgh Regional Transit bus monitoring. The browser, API routes, Nemotron integration, PRT polling, and dashboard are deployed as one project.


## ⚠️ Archived
This was a project created for [SteelHacks XIII](https://steelhacks.org), and we have no intention on maintaining it. We had a blast, see you next time ;)

Submitted Tracks:
- Beyond the Chatbot
- Xtract
- Seed Round
- \[MLH] Best use of `.tech` domain

(we won none of the above, but congrats to the groups that did!)

It can still be found at [techtracker.tech](https://techtracker.tech) for the time being, though the brev instance that powered the AI backend has been deleted, so it is stuck in deterministic (i.e. bad) mode.
Also feel free to check out the [Devpost](https://devpost.com/software/tbd-cbjlpn) we made for the project


## Contributors
| Name            | Email           | Role                        |
|-----------------|-----------------|-----------------------------|
| Tucker Busfield | TJB229@pitt.edu | Implementation, Integration |
| Ethan Steiner   | EPS82@pitt.edu  | Implementation              |
| Shane Klein     | SFK34@pitt.edu  | Video, efficacy testing     |
| Jesse Huber     | JGH60@pitt.edu  | Video, efficacy testing     |

---


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
