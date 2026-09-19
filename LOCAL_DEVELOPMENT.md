# Local development

Project Dispatch runs locally as a single Next.js application. The browser UI and backend API routes are served from the same development server.

## Prerequisites

- Node.js 18.18 or newer
- npm
- Optional: a Brev GPU instance running NVIDIA NIM
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

## Run the test suite

Run the unit and route tests without contacting live providers or Brev:

```powershell
npm test
```

Use watch mode while changing the fixtures or Nemotron parser:

```powershell
npm run test:watch
```

The suite covers staged telemetry shape, posted and developing delay scenarios,
Nemotron response validation and fallback behavior, prompt inputs, and local-only
test API gating.

Nemotron is intentionally tuned as an early-warning classifier: multiple slow
vehicles, missing positions, or at least 25% of a route's vehicles below 10
miles per hour can produce a clearly labeled possible-delay alert before PRT
publishes an official service alert.

## Configure environment variables

Create a file named `.env.local` in the repository root. This file is ignored by Git and should never be committed.

Minimal local configuration:

```text
CRON_SECRET=local-development-secret
```

With Nemotron on a Brev-hosted NIM:

```text
BREV_NIM_ENDPOINT=http://your-brev-host:8000/v1
# Only set this when your Brev reverse proxy requires authentication.
BREV_NIM_API_KEY=
BREV_NIM_MODEL=nvidia/llama-3.1-nemotron-nano-vl-8b-v1
```

### Connecting to a Brev VM from local development

For a local-only setup, keep the NIM private and forward its port through Brev:

```bash
brev login
brev shell dispatch-nim
# In a second WSL/Linux terminal:
brev port-forward dispatch-nim --port 8000:8000
```

Use this local environment configuration while the port-forward is running:

```text
BREV_NIM_ENDPOINT=http://127.0.0.1:8000/v1
BREV_NIM_API_KEY=
BREV_NIM_MODEL=<exact-model-from-http://127.0.0.1:8000/v1/models>
```

Verify the NIM before starting Next.js:

```bash
curl http://127.0.0.1:8000/v1/health/ready
curl http://127.0.0.1:8000/v1/models
```

Stop the Brev instance after local testing. Leaving a GPU VM running is the primary source of Brev cost; token volume and the number of short requests are secondary for a self-hosted NIM.

With Upstash Redis persistence enabled:

```text
KV_REST_API_URL=https://your-upstash-database.upstash.io
KV_REST_API_TOKEN=your-upstash-token
```

Optional provider overrides:

```text
PRT_VEHICLE_POSITIONS_URL=https://truetime.portauthority.org/gtfsrt-bus/vehicles
OPENSKY_STATES_URL=https://opensky-network.org/api/states/all
AMTRAK_TELEMETRY_URL=https://your-authorized-amtrak-adapter.example/trains
```

The PRT URL must not include `?debug`; that response is diagnostic content, not GTFS-Realtime protobuf.

If Nemotron is not configured, Dispatch uses deterministic fallback logic. If Redis is not configured, the app still runs, but state persistence is best-effort and should not be expected across serverless-style requests.

Backend logs are written as JSON lines to `logs/dispatch.log`. Set `DISPATCH_LOG_FILE` to change the path. The `logs` directory is local runtime output and should not be committed.

## Start the development server

```powershell
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

The dashboard automatically:

- Loads its initial state from `/api/state`.
- Polls `/api/poll` every five minutes.
- Runs an immediate poll when **Run check now** is clicked.
- Displays provider failures without crashing the UI.

Nemotron decides which telemetry signals are important enough to report. Null
speeds are treated as unknown sensor data rather than zero miles per hour, and
isolated missing positions are treated as routine data-quality noise.

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

If the PRT error mentions an invalid wire type, remove `?debug` from `PRT_VEHICLE_POSITIONS_URL` and restart the server.

Provider errors are isolated so one unavailable source does not stop the other integrations.

### Brev Nemotron is not producing alerts

The server normalizes `BREV_NIM_ENDPOINT` to `/chat/completions`. Confirm that `http://your-brev-host:8000/v1/models` responds and that the model ID exactly matches the NIM response. Brev NIM is OpenAI-compatible and does not require the NVIDIA hosted API gateway.

If the response is 404, the endpoint is probably pointing at the Brev dashboard or an incorrect port. Use the NIM service URL ending in `/v1`, not the Brev web console URL. Dispatch keeps deterministic fallback analysis active while this is unresolved.

If the log shows HTTP 200 followed by `Nemotron response did not contain a JSON object`, the NIM is healthy but the reasoning model used its output budget on a visible thinking trace. Dispatch sends `chat_template_kwargs: { enable_thinking: false }` and allows 400 output tokens so the final JSON can be emitted. Confirm that the Brev NIM supports this NVIDIA parameter; Nemotron 3.5 Lightning does.

If `/v1/models` returns `403`, inspect the response body. The current `nemotron-*.gobrev.dev` endpoint returns a Cloudflare browser challenge (`Just a moment...`, `Enable JavaScript and cookies`) rather than an OpenAI-compatible NIM response. A server-side `fetch` cannot complete that challenge, so changing the model or bearer key will not fix it. This is a proxy/endpoint product issue, not a Dispatch prompt issue. Test the URL from the same machine running Next.js:

```powershell
Invoke-WebRequest "$env:BREV_NIM_ENDPOINT/models" -Headers @{ Authorization = "Bearer $env:BREV_NIM_API_KEY" }
```

Do not use a Brev console URL or a Cloudflare browser-protected `gobrev.dev` URL as `BREV_NIM_ENDPOINT`. For local development, use `brev port-forward` and `http://127.0.0.1:8000/v1`. For Vercel, use a direct NIM endpoint or an authenticated TLS reverse proxy whose API path bypasses Cloudflare browser challenges. Configure that proxy to forward `/v1/*` to the Brev VM and require a machine-to-machine bearer key. If local port-forwarding works but the public URL returns a challenge, the NIM is healthy and only public ingress needs to be replaced.

Verify all three variables are set:

```powershell
$env:BREV_NIM_ENDPOINT
$env:BREV_NIM_API_KEY
$env:BREV_NIM_MODEL
```

Then restart `npm run dev`. API keys are read by server-side route handlers and are not exposed to the browser.

### Complete Brev/Nemotron recovery runbook

Use these steps in order. Do not skip the endpoint tests: a model change cannot fix a URL or authentication failure.

1. **Rotate exposed credentials first.** Revoke and recreate any Brev proxy key, NGC key, or hosted NVIDIA key that has been pasted into chat, committed, or shared. Update `.env.local` with the replacement values. `.env.local` is ignored by Git, but still treat it as a secret.
2. **Confirm the VM is running.** From WSL/Linux run `brev list`, then `brev shell dispatch-nim`. On the VM run:

   ```bash
   nvidia-smi
   docker ps
   docker logs --tail 100 nemotron-nim
   ```

   If the container is stopped or repeatedly restarting, fix the NIM image, NGC login, GPU memory, or container logs before testing the application.
3. **Test NIM locally on the VM.** The following must succeed without the public proxy:

   ```bash
   curl -i http://127.0.0.1:8000/v1/health/ready
   curl -s http://127.0.0.1:8000/v1/models
   curl -i http://127.0.0.1:8000/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"<exact-id-from-v1-models>","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":4}'
   ```

   Expected results are HTTP 200 for readiness, models, and completion. If this fails, the issue is inside NIM and not in Dispatch.
4. **Test through Brev port forwarding.** From a second WSL/Linux terminal run:

   ```bash
   brev port-forward dispatch-nim --port 8000:8000
   ```

   In another terminal, test `http://127.0.0.1:8000/v1/models`. If direct VM access works but port forwarding fails, repair Brev connectivity or use the correct instance name.
5. **Verify the public proxy separately.** From the same machine running Next.js:

   ```powershell
   $headers = @{ Authorization = "Bearer $env:BREV_NIM_API_KEY" }
   Invoke-WebRequest "$env:BREV_NIM_ENDPOINT/models" -Headers $headers
   ```

   HTTP 200 means the proxy can reach NIM. HTTP 401/403 means proxy credentials or authorization policy are wrong. HTTP 404 means the URL is wrong, points to the Brev console, uses the wrong port, or is missing `/v1`. A timeout means the proxy is not reachable from that network.
6. **Verify the model ID.** Copy the model ID exactly from `/v1/models`; do not use a catalog name, display name, or an old hosted NVIDIA model name. Set `BREV_NIM_MODEL` to that exact value.
7. **Verify the application endpoint.** Use only the NIM base URL ending in `/v1`, for example `https://proxy.example/v1`. Do not include `/chat/completions`; Dispatch appends it. Do not use `gobrev.dev` dashboard URLs.
8. **Restart and run one request.** Stop and restart `npm run dev`, click **Run check now**, then inspect `/api/state`. The Nemotron status should become `connected`; otherwise the status error identifies the HTTP class or response parsing problem.
9. **For Vercel, repeat the public test from outside the Brev VM.** A URL that works only as `localhost` or through local port forwarding cannot work from Vercel. Use an authenticated HTTPS reverse proxy or Brev public endpoint, configure the same URL and proxy key in Vercel server environment variables, redeploy, and run a manual check.

Dispatch does not expose the NGC key or Brev key to the browser. It sends one authenticated OpenAI-compatible request, logs only a redacted response preview, and uses deterministic fallback analysis when Brev is unavailable.

### Verifying Nemotron delay detection

#### Repeatable staged test mode

Dispatch includes a local-only test endpoint that runs the same polling, Nemotron, response parsing, and state persistence path with deterministic fixtures. It is disabled when `NODE_ENV=production` or `VERCEL` is set and cannot be used by a deployed Vercel function.

Start the local server with valid Brev variables, then list scenarios:

```powershell
Invoke-RestMethod http://localhost:3000/api/test/nemotron
```

Run the developing-delay scenario:

```powershell
$result = Invoke-RestMethod `
  http://localhost:3000/api/test/nemotron `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"scenario":"developing-delay"}'

$result.action
$result.state.nemotron
```

Available scenarios:

- `normal`: eight 61A test vehicles moving normally. Expected result is usually `on_time`/`none`.
- `developing-delay`: seven 61A vehicles moving at approximately 1.5 mph and two without positions, with no posted alert. Expected result is `disrupted` or `anomalous`, affected route `61A`, and reasoning about the developing pattern.
- `posted-delay`: the same slow vehicles plus a synthetic PRT alert for 61A. This checks how Nemotron responds when the condition is already represented in posted service data.

Inspect the returned `state.telemetry`, `state.service_alerts`, `state.nemotron`, and `action`. The UI can be refreshed after each request to inspect the proactive action center, map coloring, navigation decision, and **Why?** explanation. Staged vehicle IDs begin with `TEST-` and are never fetched from PRT.

Reset to live providers by running a normal poll:

```powershell
Invoke-RestMethod http://localhost:3000/api/poll -Method Post
```

The staged endpoint intentionally does not accept arbitrary telemetry from a request body; scenarios are fixed in source so tests cannot inject secrets or accidentally imitate production data.

Use a deliberately controlled prompt before relying on live traffic:

```powershell
$body = @{
  model = $env:BREV_NIM_MODEL
  messages = @(
    @{
      role = "system"
      content = 'Return only JSON with status, action, message, affected_routes, and reasoning. Treat this as a test.'
    },
    @{
      role = "user"
      content = 'Test scenario: route 61A has 12 buses. 8 are moving below 2 mph for 15 minutes, 3 have stopped reporting positions, and no PRT service alert mentions 61A. Identify whether a delay is developing.'
    }
  )
  temperature = 0
  max_tokens = 400
  chat_template_kwargs = @{ enable_thinking = $false }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  "$env:BREV_NIM_ENDPOINT/chat/completions" `
  -Method Post `
  -Headers @{ Authorization = "Bearer $env:BREV_NIM_API_KEY" } `
  -ContentType "application/json" `
  -Body $body
```

The response should be HTTP 200, contain valid JSON in `choices[0].message.content`, identify a disrupted or anomalous condition, include `61A` in `affected_routes`, and explain the slow/stopped-vehicle evidence in `reasoning`. This verifies the Brev model and output contract, but it does not prove that the live PRT feed will produce the same signal.

For an end-to-end check, run the local app with valid Brev variables, click **Run check now**, and inspect `/api/state`. Confirm `nemotron.status` is `connected`, the proactive action center shows a Nemotron alert when live evidence is strong, and the navigation **Why?** modal contains the latest explanation. The server log should contain both `Received Brev NIM response` with status `200` and `Telemetry poll completed`.

To verify a real-world detection without waiting for an incident, temporarily use a staging-only telemetry fixture or mock provider that produces the controlled slow/stopped pattern. Do not alter production PRT data or weaken the prompt solely to force an alert. Restore the real provider after the test and confirm a normal poll returns `on_time`/`none` when no abnormal pattern exists.

### Redis state is not persisting

Confirm both `KV_REST_API_URL` and `KV_REST_API_TOKEN` are present in `.env.local`, then restart the development server. The Upstash REST endpoint must be reachable from your network.

## Stop the server

Press `Ctrl+C` in the terminal running Next.js.
