# Local development

Project Dispatch runs locally as a single Next.js application. The browser UI and backend API routes are served from the same development server.

## Prerequisites

- Node.js 18.18 or newer
- npm
- Optional: an NVIDIA Nemotron API key
- Optional: an Upstash Redis database for persistent local state

Check your installed versions:

```powershell
node --version
npm --version
```

## Install dependencies

From the repository root:

```powershell
npm install
```

## Configure environment variables

Create a file named `.env.local` in the repository root. This file is ignored by Git and should never be committed.

Minimal local configuration:

```text
CRON_SECRET=local-development-secret
```

With Nemotron enabled:

```text
NEMOTRON_ENDPOINT=https://your-nemotron-endpoint/v1/chat/completions
NEMOTRON_API_KEY=your-api-key
NEMOTRON_MODEL=nvidia/nemotron
```

With Upstash Redis persistence enabled:

```text
KV_REST_API_URL=https://your-upstash-database.upstash.io
KV_REST_API_TOKEN=your-upstash-token
```

Optional provider overrides:

```text
PRT_VEHICLE_POSITIONS_URL=https://truetime.portauthority.org/gtfsrt/vehiclePositions
OPENSKY_STATES_URL=https://opensky-network.org/api/states/all
AMTRAK_TELEMETRY_URL=https://your-authorized-amtrak-adapter.example/trains
```

If Nemotron is not configured, Dispatch uses deterministic fallback logic. If Redis is not configured, the app still runs, but state persistence is best-effort and should not be expected across serverless-style requests.

## Start the development server

```powershell
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

The dashboard automatically:

- Loads its initial state from `/api/state`.
- Polls `/api/poll` every 60 seconds.
- Runs an immediate poll when **Run check now** is clicked.
- Displays provider failures without crashing the UI.

## Test the API manually

In a second PowerShell terminal:

```powershell
Invoke-RestMethod http://localhost:3000/api/health
Invoke-RestMethod http://localhost:3000/api/state
Invoke-RestMethod http://localhost:3000/api/poll -Method Post
```

Expected health response:

```json
{
  "status": "ok"
}
```

Test the cron route locally:

```powershell
Invoke-RestMethod `
  http://localhost:3000/api/cron/poll `
  -Method Get `
  -Headers @{ Authorization = "Bearer local-development-secret" }
```

The cron route accepts `GET` because Vercel Cron invokes it with a GET request. It rejects unauthorized requests when `CRON_SECRET` is set.

## Test mitigation actions

The mitigation buttons require an alert. To inspect or invoke an existing action:

```powershell
$state = Invoke-RestMethod http://localhost:3000/api/state
$alertId = $state.alerts[0].id

Invoke-RestMethod `
  "http://localhost:3000/api/actions/$alertId/execute" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"type":"draft_email"}'
```

Supported action types are:

- `draft_email`
- `reschedule_calendar`

These actions currently create review-ready entries; they do not send email or modify a real calendar.

## Run a production-like local build

Use this to verify the same build path Vercel uses:

```powershell
npm run build
npm run start
```

Then open [http://localhost:3000](http://localhost:3000) and repeat the API checks above.

## Troubleshooting

### Port 3000 is already in use

Run Next.js on another port:

```powershell
npm run dev -- --port 3001
```

### Provider errors appear in the dashboard

Check `/api/state` and inspect `provider_errors`. Common causes include:

- A provider endpoint is unavailable.
- OpenSky rate limiting.
- An Amtrak adapter returning an unexpected JSON shape.
- A PRT endpoint returning something other than GTFS-RT protobuf.

Provider errors are isolated so one unavailable source does not stop the other integrations.

### Nemotron is not producing alerts

Verify all three variables are set:

```powershell
$env:NEMOTRON_ENDPOINT
$env:NEMOTRON_API_KEY
$env:NEMOTRON_MODEL
```

Then restart `npm run dev`. API keys are read by server-side route handlers and are not exposed to the browser.

### Redis state is not persisting

Confirm both `KV_REST_API_URL` and `KV_REST_API_TOKEN` are present in `.env.local`, then restart the development server. The Upstash REST endpoint must be reachable from your network.

## Stop the server

Press `Ctrl+C` in the terminal running Next.js.
