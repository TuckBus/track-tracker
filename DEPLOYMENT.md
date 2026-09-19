# Project Dispatch deployment guide

This guide deploys the React dashboard to Vercel and the FastAPI telemetry engine to a small persistent Python host. This split is intentional: Dispatch runs a 30-60 second polling loop and maintains Server-Sent Event (SSE) subscriptions, which require a long-lived backend process. Vercel Functions are request-oriented and should not be used as the polling daemon.

## Architecture

```text
Browser
  |
  | HTTPS / SSE
  v
Vercel (frontend)
  |
  | REST + SSE to VITE_API_URL
  v
Persistent Python host (backend/main.py)
  |-- PRT GTFS-RT
  |-- OpenSky Network
  |-- optional Amtrak telemetry URL
  `-- NVIDIA Nemotron API
```

Recommended production arrangement:

- **Frontend:** Vercel, using the `frontend` directory as the project root.
- **Backend:** Render, Railway, Fly.io, a VM, or another host that supports a persistent Python web process.
- **Nemotron:** NVIDIA hosted API or an OpenAI-compatible Nemotron endpoint.
- **Secrets:** Store all API keys in the backend host's secret/environment settings. Only `VITE_API_URL` belongs in the frontend deployment.

## Prerequisites

Install or create:

- A GitHub repository containing this project.
- A Vercel account connected to the repository.
- A persistent Python service account (Render is used in the example below).
- An NVIDIA API key and the Nemotron endpoint/model you intend to use.
- Optional access to an Amtrak telemetry JSON endpoint.

Never commit `.env` files, API keys, or provider credentials.

## 1. Deploy the backend

### Render example

Create a new **Web Service** from the repository with these settings:

| Setting | Value |
| --- | --- |
| Root directory | repository root |
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn backend.main:app --host 0.0.0.0 --port $PORT` |
| Health check path | `/api/health` |

If the host does not provide a `PORT` variable, use `8000` in the start command.

Set the following backend environment variables:

| Variable | Required | Description |
| --- | --- | --- |
| `CORS_ORIGINS` | Yes | Comma-separated Vercel origin, for example `https://dispatch.example.com`. Do not leave this as `*` when using credentials. |
| `POLL_INTERVAL_SECONDS` | No | Polling interval. The service clamps this to at least 30 seconds. Use `60` for production. |
| `NEMOTRON_ENDPOINT` | Yes for live AI | Full chat-completions URL for your NVIDIA Nemotron deployment. |
| `NEMOTRON_API_KEY` | Yes for live AI | Secret bearer token for the Nemotron endpoint. |
| `NEMOTRON_MODEL` | No | Model identifier expected by the endpoint. Defaults to `nvidia/nemotron`. |
| `PRT_VEHICLE_POSITIONS_URL` | No | PRT GTFS-RT vehicle positions URL. The code has a default Pittsburgh endpoint. |
| `OPENSKY_STATES_URL` | No | OpenSky states endpoint. Defaults to `https://opensky-network.org/api/states/all`. |
| `AMTRAK_TELEMETRY_URL` | No | Configurable JSON endpoint for Pennsylvanian train 42/43. Empty means this provider is disabled. |

After deployment, verify:

```powershell
Invoke-WebRequest https://YOUR-BACKEND.example.com/api/health
Invoke-WebRequest https://YOUR-BACKEND.example.com/api/state
```

The first response must be `{"status":"ok"}`. `/api/state` should return JSON even when a provider is unavailable; provider errors are reported in `provider_errors`.

### Other persistent hosts

The same process works on Railway, Fly.io, or a VM:

```bash
python -m pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
```

Use a platform health check against `/api/health`. Do not use a one-shot job or cron-only deployment: the dashboard's SSE connection and the in-process polling loop need a running service.

## 2. Configure NVIDIA Nemotron

Dispatch sends Nemotron one system message containing the user's itinerary and normalized telemetry. The client requests JSON mode with `temperature: 0` and expects:

```json
{
  "status": "disrupted",
  "action": "trigger_ui_alert",
  "message": "Concise explanation of the detected anomaly and proactive action taken."
}
```

Configure the backend as follows:

```text
NEMOTRON_ENDPOINT=https://your-nemotron-provider.example/v1/chat/completions
NEMOTRON_API_KEY=<server-side-secret>
NEMOTRON_MODEL=nvidia/nemotron-4-mini-instruct
```

Use the exact endpoint and model name supplied by NVIDIA or your inference provider. Some NVIDIA deployments expose an OpenAI-compatible URL; others expose a deployment-specific URL. The implementation sends:

```http
POST $NEMOTRON_ENDPOINT
Authorization: Bearer $NEMOTRON_API_KEY
Content-Type: application/json
```

If Nemotron is unavailable, returns non-JSON, or violates the schema, Dispatch logs the failure and uses deterministic fallback logic. A stationary Pennsylvanian record produces an anomalous UI alert; otherwise the fallback is `on_time` with `action: "none"`.

Test the integration from the backend host, never from browser JavaScript:

```powershell
$headers = @{ Authorization = "Bearer $env:NEMOTRON_API_KEY"; "Content-Type" = "application/json" }
$body = @{
  model = $env:NEMOTRON_MODEL
  messages = @(@{ role = "user"; content = "Return {`"status`":`"on_time`",`"action`":`"none`",`"message`":`"test`"}" })
  temperature = 0
  response_format = @{ type = "json_object" }
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri $env:NEMOTRON_ENDPOINT -Method Post -Headers $headers -Body $body
```

Do not expose `NEMOTRON_API_KEY` as a `VITE_*` variable. Vite embeds `VITE_*` values into the public bundle.

## 3. Deploy the frontend to Vercel

1. In Vercel, select **Add New Project** and import the repository.
2. Set **Root Directory** to `frontend`.
3. Leave the framework preset as **Vite**.
4. Use:
   - Install command: `npm install`
   - Build command: `npm run build`
   - Output directory: `dist`
5. Add this environment variable for **Production**, **Preview**, and **Development**:

```text
VITE_API_URL=https://YOUR-BACKEND.example.com
```

6. Deploy and open the generated Vercel URL.

The frontend uses:

- `GET /api/state` for initial state.
- `GET /api/events` for live SSE state and action events.
- `POST /api/poll` for the **Run check now** button.
- `POST /api/actions/{id}/execute` for simulated email/calendar mitigation.

After the first deployment, replace the backend's `CORS_ORIGINS` value with the exact Vercel production origin. If using a custom domain, use that domain rather than the `vercel.app` preview URL.

## 4. Optional Vercel rewrite

The current frontend intentionally calls the backend URL directly. A rewrite can make the browser use the same origin, but it does not move the Python polling process into Vercel:

```json
{
  "rewrites": [
    {
      "source": "/api/:path*",
      "destination": "https://YOUR-BACKEND.example.com/api/:path*"
    }
  ]
}
```

If you add this to `frontend/vercel.json`, set `VITE_API_URL` to the deployed Vercel origin (for example `https://dispatch.example.com`). Keep CORS configured anyway for direct API health checks and future clients. Do not create a Vercel rewrite that points back to the same Vercel project.

## 5. Provider integration

### PRT GTFS-RT

`PRTProvider` downloads the VehiclePositions protobuf feed and converts vehicle ID, route, position, speed, and status into `TelemetryRecord` objects. The backend logs a provider error instead of crashing if the feed is unavailable or returns invalid data.

Confirm the provider is working with:

```powershell
Invoke-RestMethod https://YOUR-BACKEND.example.com/api/state |
  Select-Object -ExpandProperty telemetry
```

### OpenSky Network

`OpenSkyProvider` requests aircraft state vectors in the PIT bounding box. OpenSky may enforce rate limits or require authentication for higher usage. Keep the polling interval at 60 seconds or greater and configure OpenSky credentials at the provider/network layer if your account requires them.

### Amtrak

Amtrak does not provide a stable public API. Supply a maintained JSON adapter URL through `AMTRAK_TELEMETRY_URL`. The adapter accepts either a list or an object with a `trains` array and filters train numbers `42` and `43`. Validate the endpoint's terms of use and response shape before enabling it in production.

## 6. Production verification checklist

Run these checks after every deployment:

```powershell
$base = "https://YOUR-BACKEND.example.com"
Invoke-RestMethod "$base/api/health"
Invoke-RestMethod "$base/api/state"
Invoke-RestMethod "$base/api/poll" -Method Post
```

Then verify in the browser:

1. The dashboard loads without CORS errors.
2. The system status shows **Monitoring live telemetry**.
3. The network panel contains an open `/api/events` connection.
4. **Run check now** completes and refreshes the last-check time.
5. A valid Nemotron disruption produces a visible proactive alert.
6. The email and calendar buttons create mitigation entries.
7. Refreshing the page preserves only in-memory backend state; this implementation does not persist history across restarts.

## Operational notes and limitations

- The service currently stores telemetry and alerts in memory. Add a database or Redis before requiring durable history or multiple backend replicas.
- Use one backend replica unless shared event/state storage is added. Multiple replicas would give each browser a different in-memory state.
- SSE connections should be terminated by the client and reverse proxy when the browser closes. The async generator removes subscribers during cleanup.
- Protect `/api/poll` and mitigation endpoints with authentication before exposing them to untrusted users.
- Configure HTTPS on both Vercel and the backend host. Browsers block an HTTPS frontend from opening an insecure HTTP SSE connection.
- Provider failures are non-fatal, but inspect `provider_errors` and backend logs rather than treating an empty telemetry list as proof that Pittsburgh transit is on time.
