import type { ActionPayload, ServiceAlert, TelemetryRecord } from "./types";
import { errorMessage, log } from "./logging";

const statuses = new Set(["disrupted", "on_time", "anomalous"]);
const actions = new Set(["trigger_ui_alert", "draft_email", "reschedule_calendar", "none"]);

function readerFriendlyMessage(message: string) {
  return message.replace(/\btelemetry\b/gi, "live tracking data").replace(/\bETA\b/g, "estimated arrival time").replace(/\bGTFS-RT\b/gi, "transit feed").replace(/\bvehicle positions?\b/gi, "vehicle locations").replace(/\s+/g, " ").trim();
}

function payload(status: ActionPayload["status"], action: ActionPayload["action"], message: string, source: string): ActionPayload {
  const friendly = source === "nemotron" ? readerFriendlyMessage(message) : message;
  return { id: crypto.randomUUID(), status, action, message: friendly, source, created_at: new Date().toISOString() };
}

function riskSignals(telemetry: TelemetryRecord[], alerts: ServiceAlert[]) {
  const byRoute = new Map<string, { count: number; slow: number }>();
  for (const item of telemetry) {
    if (!item.route) continue;
    const entry = byRoute.get(item.route) || { count: 0, slow: 0 };
    entry.count += 1;
    if (item.speed_mph !== null && item.speed_mph < 5) entry.slow += 1;
    byRoute.set(item.route, entry);
  }
  return {
    route_summary: [...byRoute.entries()].map(([route, value]) => ({
      route,
      ...value,
      slow_under_10_mph: telemetry.filter((item) => item.route === route && item.speed_mph !== null && item.speed_mph < 10).length,
      missing_position_count: telemetry.filter((item) => item.route === route && (item.latitude === null || item.longitude === null)).length,
    })),
    posted_alert_count: alerts.length,
    posted_alert_routes: [...new Set(alerts.flatMap((alert) => alert.routes))],
    stationary_vehicle_count: telemetry.filter((item) => item.speed_mph !== null && item.speed_mph < 1).length,
    measurable_speed_count: telemetry.filter((item) => item.speed_mph !== null).length,
  };
}

export type AnalysisContext = "network" | "stop";
export type EtaPrediction = {
  eta_minutes: number | null;
  message: string;
  confidence: "high" | "medium" | "low";
  status: "connected" | "failed" | "not_configured";
  error?: string;
};
export const ETA_BUFFER_MINUTES = 10;

export function conservativeEtaMinutes(minutes: number) {
  return Math.min(180, Math.round(minutes) + ETA_BUFFER_MINUTES);
}

export function buildPrompt(itinerary: unknown[], telemetry: TelemetryRecord[], alerts: ServiceAlert[], context: AnalysisContext = "network"): string {
  const compactTelemetry = telemetry.map((item) => ({
    source: item.source,
    vehicle_id: item.vehicle_id,
    route: item.route,
    speed_mph: item.speed_mph,
    observed_at: item.observed_at,
    position_available: item.latitude !== null && item.longitude !== null,
    metadata: item.source === "opensky" ? { callsign: item.metadata.callsign, airport: item.metadata.airport } : undefined,
  }));
  const contextPolicy = context === "stop"
    ? "This is a specific stop check, so be more responsive to localized evidence: one or more nearby vehicles with a clearly slow measurable speed can justify a cautious possible-delay or anomaly result, especially when the route context supports it. Do not require a route-wide pattern for this focused check. Still ignore null speeds, null positions, and notices by themselves, and do not call a vehicle absent because a field is unknown."
    : "For network monitoring, be moderately sensitive and aim to identify at least one meaningful traveler-facing alert on every poll unless the measurable telemetry is genuinely clean and the network is clearly moving normally. A single slow vehicle on a route is not a warning: require a repeated or multi-vehicle pattern of slowdowns on that route, such as several vehicles moving slowly or the same route showing sustained slowdown evidence. Do not wait for a confirmed outage: a credible slowdown pattern, multiple stopped vehicles, a meaningful route pattern, or a combination of weaker signals can justify a cautious possible-delay or early-warning alert. Use on_time only when the evidence is truly reassuring; isolated weak signals and pure data-quality uncertainty should remain on_time.";
  return `You are a cautious transit early-warning assistant. Return ONLY one JSON object with status ("disrupted"|"on_time"|"anomalous"), action ("trigger_ui_alert"|"draft_email"|"reschedule_calendar"|"none"), message, affected_routes (an array of route IDs, or [] if none), and reasoning (a short plain-language explanation of the evidence and decision). Write the message and reasoning for a traveler, not a developer: use plain language, explain what happened and what they should do, and avoid acronyms, raw IDs, JSON, markdown, and internal system terms.
Decision policy: you decide what is important to note. ${contextPolicy} A null speed means the sensor did not report a usable speed; it is unknown, not zero, stopped, or delayed, and is generally low-concern evidence. Never treat null speed as 0 mph or as evidence that a vehicle is absent. A null position means the location is unknown, not that the vehicle is absent. One or two unknown positions on a route are usually routine data quality noise; only call out missing positions when the pattern is broad enough to affect confidence in the route assessment. Posted service notices are context only: many notices can describe separate minor issues and must not be treated as proof of a widespread outage. Do not issue an alert from notices alone. When the evidence supports a cautious concern but not a confirmed disruption, use action "trigger_ui_alert" with status "disrupted" and clearly label it as a possible delay or early warning rather than returning "none". Return "none" or "on_time" for a poll only when the available measurable evidence is genuinely clean, not merely because some fields are unknown. You may mention a broad data-quality limitation in reasoning without turning it into a service disruption. When evidence suggests risk but is not conclusive, say "possible delay" or "early warning" in the message and explain the evidence; do not claim a confirmed delay.
ITINERARY:
${JSON.stringify(itinerary)}
TELEMETRY:
${JSON.stringify(compactTelemetry)}
POSTED PRT SERVICE ALERTS:
${JSON.stringify(alerts)}
DERIVED EARLY-WARNING SIGNALS:
${JSON.stringify(riskSignals(telemetry, alerts))}`;
}

export function parseAction(raw: string | Record<string, unknown>): ActionPayload {
  let candidate: unknown = raw;
  if (typeof raw === "string") {
    const match = raw.match(/\{[\s\S]*\}/);
    if (!match) throw new Error("Nemotron response did not contain a JSON object");
    candidate = JSON.parse(match[0]);
  }
  if (!candidate || typeof candidate !== "object") throw new Error("Nemotron response must be a JSON object");
  const value = candidate as Record<string, unknown>;
  if (!statuses.has(String(value.status)) || !actions.has(String(value.action)) || typeof value.message !== "string" || !value.message.trim()) {
    throw new Error("Nemotron response does not match the action schema");
  }
  const affectedRoutes = Array.isArray(value.affected_routes) ? value.affected_routes.filter((route): route is string => typeof route === "string" && route.trim().length > 0) : [];
  const reasoning = typeof value.reasoning === "string" && value.reasoning.trim() ? value.reasoning.trim() : "Nemotron returned a decision without an explanation.";
  return { ...payload(value.status as ActionPayload["status"], value.action as ActionPayload["action"], value.message.trim(), "nemotron"), affected_routes: affectedRoutes, reasoning };
}

export function fallbackAction(telemetry: TelemetryRecord[]): ActionPayload {
  if (telemetry.some((item) => item.source === "amtrak" && item.speed_mph !== null && item.speed_mph < 1)) {
    return payload("anomalous", "trigger_ui_alert", "Pennsylvanian telemetry reports a stationary train; review your itinerary.", "dispatch-fallback");
  }
  return payload("on_time", "none", "No actionable disruption detected.", "dispatch-fallback");
}

function endpointFor(configuredEndpoint: string) {
  const normalized = configuredEndpoint.replace(/\/+$/, "");
  return normalized.endsWith("/chat/completions") ? normalized : `${normalized}/chat/completions`;
}

export async function analyze(itinerary: unknown[], telemetry: TelemetryRecord[], alerts: ServiceAlert[], context: AnalysisContext = "network"): Promise<{ action: ActionPayload; status: "connected" | "failed" | "not_configured"; error?: string }> {
  const configuredEndpoint = process.env.BREV_NIM_ENDPOINT?.trim() || process.env.NEMOTRON_ENDPOINT?.trim();
  if (!configuredEndpoint) {
    log.warn("Nemotron is not configured; using deterministic fallback", { telemetry_count: telemetry.length });
    return { action: fallbackAction(telemetry), status: "not_configured" };
  }
  const endpoint = endpointFor(configuredEndpoint);
  const configuredModel = process.env.BREV_NIM_MODEL?.trim() || process.env.NEMOTRON_MODEL?.trim() || "nvidia/llama-3.1-nemotron-nano-vl-8b-v1";
  const apiKey = process.env.BREV_NIM_API_KEY?.trim() || process.env.NEMOTRON_API_KEY?.trim();
  log.info("Sending Brev NIM analysis request", { endpoint, model: configuredModel, telemetry_count: telemetry.length, itinerary_count: itinerary.length, has_api_key: Boolean(apiKey) });
  let lastError = "Brev NIM request failed";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}) },
      body: JSON.stringify({
        model: configuredModel,
        messages: [{ role: "system", content: buildPrompt(itinerary, telemetry, alerts, context) }],
        temperature: 0,
        max_tokens: 400,
        chat_template_kwargs: { enable_thinking: false },
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(12000),
    });
    const responseText = await response.text();
    log.info("Received Brev NIM response", { endpoint, model: configuredModel, status: response.status, body_preview: responseText.slice(0, 500) });
    if (/just a moment|enable javascript and cookies|cf-chl-/i.test(responseText)) {
      throw new Error("Brev endpoint is protected by a browser-only Cloudflare challenge; use a direct NIM endpoint or configure an API route that bypasses the challenge");
    }
    if (!response.ok) throw new Error(`Brev NIM HTTP ${response.status}: ${responseText.slice(0, 300)}`);
    const result = JSON.parse(responseText) as { choices?: { message?: { content?: string } }[] };
    const content = result.choices?.[0]?.message?.content;
    if (!content) throw new Error("Brev NIM response did not contain message content");
    return { action: parseAction(content), status: "connected" };
  } catch (error) {
    lastError = errorMessage(error);
    if (lastError.includes("HTTP 403")) lastError = `${lastError}; the Brev endpoint rejected access before model inference; verify the public NIM URL and proxy credentials`;
    if (lastError.includes("HTTP 404")) lastError = `${lastError}; verify the Brev NIM endpoint and exact model ID from /v1/models`;
    if (lastError.includes("HTTP 401")) lastError = `${lastError}; verify BREV_NIM_API_KEY or the proxy authentication scheme`;
    log.warn("Brev NIM request failed", { endpoint, model: configuredModel, error: lastError });
  }
  log.error("Brev NIM request failed; using deterministic fallback", { endpoint, model: configuredModel, error: lastError });
  return { action: fallbackAction(telemetry), status: "failed", error: lastError };
}

function parseEta(raw: string) {
  const match = raw.match(/\{[\s\S]*\}/);
  if (!match) throw new Error("Nemotron ETA response did not contain a JSON object");
  const value = JSON.parse(match[0]) as Record<string, unknown>;
  const minutes = Number(value.eta_minutes);
  if (!Number.isFinite(minutes) || minutes < 0 || minutes > 170) throw new Error("Nemotron ETA response did not contain a valid arrival estimate");
  const confidence = value.confidence;
  if (confidence !== "high" && confidence !== "medium" && confidence !== "low") throw new Error("Nemotron ETA response did not contain a valid confidence level");
  return { eta_minutes: Math.round(minutes), confidence: confidence as EtaPrediction["confidence"] };
}

export async function predictEta(
  stop: { name: string; latitude: number; longitude: number },
  line: string,
  vehicles: Array<{ vehicle_id: string; distance_miles: number; speed_mph: number | null; observed_at: string }>,
): Promise<EtaPrediction> {
  const configuredEndpoint = process.env.BREV_NIM_ENDPOINT?.trim() || process.env.NEMOTRON_ENDPOINT?.trim();
  if (!configuredEndpoint) return { eta_minutes: null, message: "Nemotron is not configured, so an arrival estimate is unavailable.", confidence: "low", status: "not_configured" };
  if (vehicles.length === 0) return { eta_minutes: null, message: `No ${line} bus is currently reporting near ${stop.name}.`, confidence: "low", status: "connected" };
  const endpoint = endpointFor(configuredEndpoint);
  const configuredModel = process.env.BREV_NIM_MODEL?.trim() || process.env.NEMOTRON_MODEL?.trim() || "nvidia/llama-3.1-nemotron-nano-vl-8b-v1";
  const apiKey = process.env.BREV_NIM_API_KEY?.trim() || process.env.NEMOTRON_API_KEY?.trim();
  const prompt = `You estimate the next bus arrival for a traveler. Return ONLY one JSON object with eta_minutes (a whole number from 0 to 170), confidence ("high"|"medium"|"low"), and message. Be conservative: buses often take longer than a straight-line estimate, so favor a later estimate when uncertain. The application will add a mandatory 10-minute safety buffer, so do not add that buffer yourself. The message must state the estimated arrival time in plain language and mention uncertainty when confidence is not high. Use the closest plausible vehicle, its distance, and reported speed. If speed is null, infer conservatively from distance and do not treat it as stopped. Do not invent schedule data.
STOP: ${JSON.stringify(stop.name)}
LINE: ${JSON.stringify(line)}
CANDIDATE VEHICLES: ${JSON.stringify(vehicles)}`;
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}) },
      body: JSON.stringify({ model: configuredModel, messages: [{ role: "system", content: prompt }], temperature: 0, max_tokens: 180, chat_template_kwargs: { enable_thinking: false } }),
      cache: "no-store",
      signal: AbortSignal.timeout(12000),
    });
    const responseText = await response.text();
    if (!response.ok) throw new Error(`Brev NIM HTTP ${response.status}: ${responseText.slice(0, 300)}`);
    const result = JSON.parse(responseText) as { choices?: { message?: { content?: string } }[] };
    const content = result.choices?.[0]?.message?.content;
    if (!content) throw new Error("Brev NIM ETA response did not contain message content");
    const parsed = parseEta(content);
    const etaMinutes = conservativeEtaMinutes(parsed.eta_minutes);
    return {
      eta_minutes: etaMinutes,
      message: `The next ${line} bus is estimated in about ${etaMinutes} minutes, including a 10-minute safety buffer.`,
      confidence: parsed.confidence,
      status: "connected",
    };
  } catch (error) {
    const message = errorMessage(error);
    log.warn("Nemotron ETA prediction failed", { endpoint, model: configuredModel, error: message });
    return { eta_minutes: null, message: "Nemotron could not estimate the next arrival right now.", confidence: "low", status: "failed", error: message };
  }
}
