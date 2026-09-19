# Vercel deployment guide

Project Dispatch is a single Next.js application. The UI and all backend routes deploy to Vercel together; there is no Python service to run separately.

## Architecture

```text
Vercel Next.js deployment
  |-- React dashboard
  |-- /api/state
  |-- /api/poll
  |-- /api/cron/poll (Vercel Cron, every minute)
  |-- /api/actions/[id]/execute
  |-- PRT GTFS-RT + OpenSky + Amtrak fetchers
  `-- Nemotron API
             |
             `-- optional Upstash Redis state persistence
```

The app is request-driven because Vercel Functions are not persistent processes. Vercel Cron invokes `/api/cron/poll`; the browser also refreshes through `/api/poll` every minute and through the **Run check now** button. Upstash Redis is recommended for state shared across function invocations and deployments.

## Prerequisites

- GitHub repository containing this project.
- Vercel account connected to the repository.
- NVIDIA Nemotron endpoint and API key.
- An Upstash Redis database connected to Vercel Storage, recommended for production.
- Optional maintained Amtrak JSON telemetry endpoint.

## Deploy

1. Push the repository to GitHub.
2. In Vercel, choose **Add New Project** and import the repository.
3. Keep the project root at the repository root. Do not select `frontend`; the app is now Next.js at the root.
4. Vercel detects Next.js automatically.
5. Use the defaults:
   - Install command: `npm install`
   - Build command: `npm run build`
   - Output directory: `.next`
6. Configure the environment variables below for **Production**, **Preview**, and **Development** as appropriate.
7. Deploy.

## Environment variables

### Nemotron

These are server-only. Never prefix them with `NEXT_PUBLIC_`.

| Variable | Required | Example |
| --- | --- | --- |
| `NEMOTRON_ENDPOINT` | Yes for live AI | `https://integrate.api.nvidia.com/v1/chat/completions` |
| `NEMOTRON_API_KEY` | Yes for live AI | NVIDIA secret key |
| `NEMOTRON_MODEL` | No | `nvidia/llama-3.1-nemotron-ultra-253b-v1` |

The endpoint must accept an OpenAI-compatible chat-completions request. Dispatch sends a deterministic system prompt, `temperature: 0`, and `response_format: { "type": "json_object" }`. The model must return:

```json
{
  "status": "disrupted",
  "action": "trigger_ui_alert",
  "message": "Concise explanation of the detected anomaly and proactive action taken."
}
```

The parser also extracts a JSON object if the provider wraps it in incidental text. Invalid or unavailable Nemotron responses use deterministic fallback logic instead of failing the request.

### State persistence

Create an Upstash Redis database in Vercel Storage and connect it to the project. Vercel will usually add:

```text
KV_REST_API_URL=...
KV_REST_API_TOKEN=...
```

The app uses these variables through `@upstash/redis`. Without them, the app still runs, but state is limited to the current serverless invocation and alerts will not reliably persist. Redis stores the latest telemetry snapshot and up to 50 alerts.

### Cron protection

Set:

```text
CRON_SECRET=<long-random-secret>
```

Vercel Cron automatically sends `Authorization: Bearer <CRON_SECRET>` to the cron route. The route also works without this variable for local development, but production should set it.

### Provider endpoints

| Variable | Required | Default |
| --- | --- | --- |
| `PRT_VEHICLE_POSITIONS_URL` | No | `https://truetime.portauthority.org/gtfsrt/vehiclePositions` |
| `OPENSKY_STATES_URL` | No | `https://opensky-network.org/api/states/all` |
| `AMTRAK_TELEMETRY_URL` | No | Disabled when unset |

PRT is parsed as GTFS-Realtime protobuf. OpenSky is queried using a Pittsburgh bounding box. Amtrak accepts either a JSON array or `{ "trains": [...] }` and filters train numbers 42 and 43. Confirm that any Amtrak endpoint is authorized and stable before using it.

## Vercel Cron

[vercel.json](./vercel.json) schedules:

```json
{
  "crons": [{ "path": "/api/cron/poll", "schedule": "* * * * *" }]
}
```

The cron route:

1. Fetches PRT, Amtrak, and OpenSky telemetry.
2. Records individual provider errors without aborting the full poll.
3. Sends normalized observations to Nemotron.
4. Validates the action payload.
5. Saves the result in Redis.

Vercel Cron availability and minimum schedule frequency depend on the Vercel plan. If the plan does not support every-minute schedules, change the expression to a supported interval and keep the browser refresh behavior as a backup.

## Local development

```powershell
npm install
npm run dev
```

Create `.env.local`:

```text
NEMOTRON_ENDPOINT=https://your-nemotron-endpoint/v1/chat/completions
NEMOTRON_API_KEY=replace-me
NEMOTRON_MODEL=nvidia/nemotron
KV_REST_API_URL=https://your-upstash-endpoint
KV_REST_API_TOKEN=replace-me
CRON_SECRET=local-secret
```

Then verify:

```powershell
Invoke-RestMethod http://localhost:3000/api/health
Invoke-RestMethod http://localhost:3000/api/state
Invoke-RestMethod http://localhost:3000/api/poll -Method Post
Invoke-RestMethod http://localhost:3000/api/cron/poll -Headers @{ Authorization = "Bearer local-secret" }
```

## Verify production

```powershell
$base = "https://YOUR-PROJECT.vercel.app"
Invoke-RestMethod "$base/api/health"
Invoke-RestMethod "$base/api/state"
Invoke-RestMethod "$base/api/poll" -Method Post
```

In the Vercel dashboard, inspect **Logs** for `/api/cron/poll` and confirm:

- The cron request returns HTTP 200.
- `provider_errors` only contains expected unavailable sources.
- Nemotron is not returning authentication or schema errors.
- The dashboard's **Last checked** timestamp advances.
- A stationary Amtrak record creates a proactive alert when Nemotron is not configured.

## Operational and security notes

- API keys stay in server-side environment variables and are never sent to the browser.
- Protect `/api/poll` and `/api/actions/[id]/execute` with authentication before exposing this to multiple users. They are currently demo endpoints.
- Use HTTPS; Vercel provides it automatically.
- Redis is required for reliable state across serverless instances. The no-Redis fallback is intentionally best-effort.
- Provider timeouts and malformed responses are isolated to `provider_errors`; an empty telemetry list is not proof that service is on time.
- The current mitigation actions create review-ready entries only. Connect an email/calendar provider before allowing external side effects.
