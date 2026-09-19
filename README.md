# Project Dispatch

Project Dispatch is a Vercel-native Next.js application for proactive Pittsburgh transit and logistics monitoring. The browser, API routes, Nemotron integration, provider polling, and dashboard are deployed as one project.

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
- `GET /api/cron/poll` is invoked by Vercel Cron every minute.
- `POST /api/actions/{id}/execute` executes a simulated email or calendar mitigation.