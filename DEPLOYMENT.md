# Vercel deployment guide

Project Dispatch is a single Next.js application. The UI and all backend routes deploy to Vercel together; there is no Python service to run separately.

## Architecture

```text
Vercel Next.js deployment
  |-- React dashboard
  |-- /api/state
  |-- /api/poll
  |-- /api/cron/poll (Vercel Cron, every five minutes)
  |-- /api/actions/[id]/execute
  |-- PRT GTFS-RT + OpenSky + Amtrak fetchers
  `-- Nemotron API
             |
             `-- optional Upstash Redis state persistence
```

The app is request-driven because Vercel Functions are not persistent processes. Vercel Cron invokes `/api/cron/poll`; the browser also refreshes through `/api/poll` every five minutes and through the **Run check now** button. Upstash Redis is recommended for state shared across function invocations and deployments.

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

### Brev-hosted Nemotron

These are server-only. Never prefix them with `NEXT_PUBLIC_`.

| Variable | Required | Example |
| --- | --- | --- |
| `BREV_NIM_ENDPOINT` | Yes for live AI | `https://your-brev-proxy.example/v1` |
| `BREV_NIM_API_KEY` | Only if your proxy requires it | Proxy secret |
| `BREV_NIM_MODEL` | No | Exact model returned by `/v1/models` |

The endpoint is the OpenAI-compatible base URL exposed by the NIM running on Brev. Dispatch adds `/chat/completions`. The default model is the small `nvidia/llama-3.1-nemotron-nano-vl-8b-v1`; override it only with the exact ID returned by the NIM's `/v1/models`. Dispatch sends one compact request per poll, uses a 12-second timeout, and caps output at 220 tokens. It does not retry a second model, which avoids duplicate GPU work. The model must return:

```json
{
  "status": "disrupted",
  "action": "trigger_ui_alert",
  "message": "Concise explanation of the detected anomaly and proactive action taken."
}
```

The parser also extracts a JSON object if the provider wraps it in incidental text. Invalid or unavailable Nemotron responses use deterministic fallback logic instead of failing the request.

#### Brev setup

1. Install and authenticate the Brev CLI in Linux or WSL:

   ```bash
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/brevdev/brev-cli/main/bin/install-latest.sh)"
   brev login
   brev search
   ```

2. Create a GPU instance sized for the selected NIM. Start with an L40S-class GPU and use the model's published memory requirements; do not select an expensive multi-GPU instance by default:

   ```bash
   brev create dispatch-nim --gpu "nebius.l40sx1.pcie"
   brev shell dispatch-nim
   ```

3. On the instance, authenticate Docker to NGC with an NGC API key:

   ```bash
   export NGC_API_KEY='replace-with-ngc-key'
   echo "$NGC_API_KEY" | docker login nvcr.io --username '$oauthtoken' --password-stdin
   nvidia-smi
   ```

4. Run the exact Nemotron NIM image and tag supported by the NVIDIA NIM catalog for your account:

   ```bash
   export NIM_IMAGE='nvcr.io/nvidia/<supported-nemotron-nim>:<tag>'
   docker run -d --name nemotron-nim --restart unless-stopped \
     --gpus all -p 8000:8000 \
     -e NGC_API_KEY="$NGC_API_KEY" "$NIM_IMAGE"
   ```

5. Wait for readiness and verify the model:

   ```bash
   curl http://localhost:8000/v1/health/ready
   curl http://localhost:8000/v1/models
   curl http://localhost:8000/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"<model-from-v1-models>","messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":4}'
   ```

6. Expose port 8000 through a TLS reverse proxy or authenticated public tunnel. Never expose an unauthenticated NIM directly to the internet. Set `BREV_NIM_ENDPOINT` to that proxy URL ending in `/v1`. For local development, prefer `brev port-forward dispatch-nim --port 8000:8000`.

7. Stop the GPU instance outside demos or scheduled polling windows. Brev bills while the GPU is running; a small model, one request per poll, compact prompts, and no model retry are intentional cost controls.

#### Brev operating checklist

Use this checklist for a production deployment:

1. **Create credentials.** Create a Brev account and an NVIDIA NGC API key. Store the NGC key only on the Brev VM; store `BREV_NIM_API_KEY` only in Vercel if an authenticated reverse proxy is used. Never put either secret in Git or a `NEXT_PUBLIC_` variable.
2. **Choose capacity.** Run `brev search`, compare GPU memory and hourly price, and select the smallest single-GPU instance that satisfies the selected NIM's published requirements. Do not assume the example L40S SKU is available in every region.
3. **Create and connect.** Run `brev create dispatch-nim --gpu "<sku>"`, then `brev shell dispatch-nim`. Confirm `nvidia-smi`, Docker, and NVIDIA Container Toolkit before pulling the image.
4. **Persist configuration.** Keep Docker commands, the NIM image tag, and reverse-proxy configuration in `/home/ubuntu/workspace` or another Brev persistent workspace. Do not rely on a deleted instance's local disk.
5. **Start NIM.** Log in to `nvcr.io`, start the container on port 8000, and wait for `/v1/health/ready` before testing `/v1/models` and `/v1/chat/completions`.
6. **Secure ingress.** Put TLS and authentication in front of port 8000. Restrict ingress to Vercel's egress strategy where practical, set a strong proxy key, and do not expose the raw NIM port.
7. **Connect Vercel.** Set `BREV_NIM_ENDPOINT` to the secure URL ending in `/v1`, set the exact model ID from `/v1/models`, then redeploy. Run **Run check now** and inspect the dashboard status and server log.
8. **Control lifecycle.** Stop the Brev VM when the service is not needed. Before stopping, save the NIM configuration and any proxy configuration. Deleting the VM removes its data unless it is stored separately.

Before configuring Vercel, verify the endpoint from a machine outside the Brev VM. `GET <BREV_NIM_ENDPOINT>/models` must return `200` and list the deployed model. A `403` with a Cloudflare `Just a moment...` or `Enable JavaScript and cookies` page means the `gobrev.dev` URL is browser-protected; serverless `fetch` cannot pass that challenge. It cannot be fixed by changing the model or API key. Use `brev port-forward` locally or replace the public ingress with a direct NIM endpoint/authenticated API reverse proxy that bypasses browser challenges. A plain `403` means proxy policy or credentials. A `404` usually means the URL is a Brev console URL, an incorrect port, or is missing `/v1`.

If `/v1/chat/completions` returns HTTP 200 but the dashboard reports `Nemotron response did not contain a JSON object`, the model is spending the output budget on its visible reasoning trace. The application sends `chat_template_kwargs: { "enable_thinking": false }` and allows 400 output tokens, which produces the required compact JSON response for Nemotron 3.5 Lightning.

#### Diagnosing an existing Brev deployment

Use this order when Vercel reports Nemotron unavailable:

1. Rotate any Brev, NGC, or NVIDIA key that has been exposed. Update the secret on the Brev VM, proxy, and Vercel; do not reuse the leaked value.
2. On the Brev VM, confirm `nvidia-smi`, `docker ps`, and `docker logs --tail 100 nemotron-nim`. A container that is restarting, out of memory, or unable to pull from NGC must be fixed before network debugging.
3. Test `http://127.0.0.1:8000/v1/health/ready`, `/v1/models`, and a four-token `/v1/chat/completions` request on the VM. The model must be copied exactly from `/v1/models`.
4. Test the same paths through `brev port-forward`. This separates a NIM problem from a Brev networking problem.
5. Test the public HTTPS proxy from a machine outside the VM. `/v1/models` must return `200`. `401` or `403` is proxy authentication/policy; `404` is an incorrect public URL or path; a timeout is routing/firewall/TLS.
6. Configure Vercel only after the public test succeeds. Set `BREV_NIM_ENDPOINT` to the URL ending in `/v1`, `BREV_NIM_MODEL` to the exact `/v1/models` ID, and `BREV_NIM_API_KEY` to the proxy key. Redeploy after changing server variables.
7. Trigger **Run check now** and inspect the Nemotron status plus the Vercel function log. A connected status requires a successful completion response and a parseable JSON action.

Vercel cannot reach `127.0.0.1`, a Brev private IP, or a URL that is reachable only through a developer's port-forward. Use a public HTTPS endpoint with authentication for production, and keep the raw NIM port private.

#### Brev cost estimate

Brev pricing is provider-, region-, GPU-, and availability-dependent. The Brev console is the source of truth; the figures below are planning ranges, not a quote. Public L40S marketplace comparisons commonly show roughly **$1.09-$3.50 per GPU-hour**, while cheaper marketplace capacity can sometimes be below $1/hour. Confirm the exact SKU price in `brev search` or the Brev console before provisioning.

The app's default five-minute schedule makes:

- `12 polls/hour`
- `288 polls/day`
- `8,640 polls/30-day month`

The number of requests does not directly determine Brev's bill when the GPU VM remains running. GPU runtime is the main cost:

| Operating pattern | GPU hours / 30 days | At $0.50/hr | At $1.50/hr | At $3.50/hr |
| --- | ---: | ---: | ---: | ---: |
| VM left running 24/7 | 720 | $360 | $1,080 | $2,520 |
| VM running 8 hours/day | 240 | $120 | $360 | $840 |
| VM running 1 hour/day | 30 | $15 | $45 | $105 |
| One 10-minute manual check/day | 5 | $2.50 | $7.50 | $17.50 |

These totals exclude persistent disk, public egress, proxy/tunnel fees, taxes, and any NGC/NIM licensing terms shown for the selected image. If the VM must be continuously ready for Vercel Cron, budget for the 24/7 row. If low cost matters more than continuous readiness, stop the VM overnight and use **Run check now** only while it is running; Vercel cannot wake a stopped Brev GPU by itself.

To calculate a custom estimate:

```text
monthly GPU cost = hourly GPU price × running hours per month
running hours per month = days × hours running per day
```

For example, an L40S priced at `$1.50/hour` and left on for `8 hours/day` is approximately `1.50 × 30 × 8 = $360/month`, before storage and network charges. Reducing polling from five minutes to fifteen minutes lowers request count but does **not** lower VM cost if the VM stays on; stopping the VM is the meaningful cost control.

For the lowest practical spend, use the smallest compatible single GPU, keep the five-minute schedule, avoid duplicate retries, compact prompts, stop the VM when idle, and use `brev port-forward` for local development instead of a public endpoint.

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
| `PRT_VEHICLE_POSITIONS_URL` | No | `https://truetime.portauthority.org/gtfsrt-bus/vehicles` |
| `OPENSKY_STATES_URL` | No | `https://opensky-network.org/api/states/all` |
| `AMTRAK_TELEMETRY_URL` | No | Disabled when unset |
| `PRT_GTFS_STATIC_URL` | No | `https://www.rideprt.org/developerresources/GTFS.zip` |
| `PRT_SERVICE_ALERTS_URL` | No | `https://truetime.portauthority.org/gtfsrt-bus/alerts` |

PRT is parsed as GTFS-Realtime protobuf. Use the vehicle feed without the `?debug` query parameter; that debug response is not protobuf and will be rejected. OpenSky is queried using a Pittsburgh bounding box. Amtrak accepts either a JSON array or `{ "trains": [...] }` and filters train numbers 42 and 43. Confirm that any Amtrak endpoint is authorized and stable before using it.

The searchable stop picker loads the PRT static GTFS ZIP and parses `stops.txt` through `/api/stops`. The catalog is cached for the lifetime of a serverless instance.
Polling also ingests posted PRT GTFS-Realtime service alerts and includes them in the risk analysis. Routes with posted alerts are highlighted on the map.

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

Vercel Cron availability and minimum schedule frequency depend on the Vercel plan. The default five-minute schedule avoids unnecessary GPU requests; use **Run check now** for an immediate analysis.

## Local development

```powershell
npm install
npm run dev
```

Create `.env.local`:

```text
BREV_NIM_ENDPOINT=https://your-brev-proxy.example/v1
BREV_NIM_API_KEY=replace-me
BREV_NIM_MODEL=nvidia/llama-3.1-nemotron-nano-vl-8b-v1
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
- Backend logs are structured JSON and are written to `DISPATCH_LOG_FILE`; locally the default is `logs/dispatch.log`, while Vercel uses `/tmp/dispatch.log` because deployed filesystems are ephemeral. For durable production logs, point `DISPATCH_LOG_FILE` at a mounted/shared writable volume or use the platform's log drain. Filter entries for `Nemotron`, `Telemetry provider`, or `poll completed` when diagnosing a request.
- The dashboard status bar reports the latest Nemotron request as connected, unavailable with fallback active, or not configured.
- The current mitigation actions create review-ready entries only. Connect an email/calendar provider before allowing external side effects.
