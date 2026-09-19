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
  };
}

export function applySensitivity(action: ActionPayload, telemetry: TelemetryRecord[], sensitivity: number): ActionPayload {
  const normalizedSensitivity = Math.max(0, Math.min(100, sensitivity));
  const slowVehicleThreshold = 100 - normalizedSensitivity;
  if (normalizedSensitivity === 0) {
    return {
      ...action,
      status: "on_time",
      action: "none",
      message: "No actionable disruption detected at zero sensitivity.",
      affected_routes: [],
      reasoning: "AI warnings are disabled at zero sensitivity.",
    };
  }
  const routeSignals = new Map<string, { total: number; slow: number; missing: number }>();
  for (const item of telemetry) {
    if (!item.route) continue;
    const signal = routeSignals.get(item.route) || { total: 0, slow: 0, missing: 0 };
    signal.total += 1;
    if (item.speed_mph !== null && item.speed_mph < 10) signal.slow += 1;
    if (item.latitude === null || item.longitude === null) signal.missing += 1;
    routeSignals.set(item.route, signal);
  }
  const affectedRoutes = [...routeSignals.entries()]
    .filter(([, signal]) => (signal.slow / signal.total) * 100 >= slowVehicleThreshold || signal.missing >= (normalizedSensitivity >= 75 ? 1 : 2))
    .map(([route]) => route);
  const stationaryVehicles = telemetry.filter((item) => item.speed_mph !== null && item.speed_mph < 1).length;
  const qualifies = affectedRoutes.length > 0 || stationaryVehicles >= 2;

  if (!qualifies && action.action === "trigger_ui_alert") {
    return {
      ...action,
      status: "on_time",
      action: "none",
      message: "No actionable disruption detected at the selected sensitivity.",
      affected_routes: [],
      reasoning: "The live tracking data did not meet the selected early-warning threshold.",
    };
  }
  if (qualifies && action.action === "none") {
    return {
      ...action,
      status: "disrupted",
      action: "trigger_ui_alert",
      message: `Possible delay detected on ${affectedRoutes.join(", ") || "the monitored transit network"}.`,
      affected_routes: affectedRoutes,
      reasoning: `The selected sensitivity threshold was met: ${slowVehicleThreshold}% of reporting vehicles on an affected route are below 10 miles per hour, or multiple positions are missing.`,
    };
  }
  return action;
}

export function buildPrompt(itinerary: unknown[], telemetry: TelemetryRecord[], alerts: ServiceAlert[], sensitivity = 50): string {
  const compactTelemetry = telemetry.map((item) => ({
    source: item.source,
    vehicle_id: item.vehicle_id,
    route: item.route,
    speed_mph: item.speed_mph,
    observed_at: item.observed_at,
    position_available: item.latitude !== null && item.longitude !== null,
    metadata: item.source === "opensky" ? { callsign: item.metadata.callsign, airport: item.metadata.airport } : undefined,
  }));
  const normalizedSensitivity = Math.max(0, Math.min(100, sensitivity));
  const slowVehicleThreshold = 100 - normalizedSensitivity;
  return `You are a cautious transit early-warning assistant. Return ONLY one JSON object with status ("disrupted"|"on_time"|"anomalous"), action ("trigger_ui_alert"|"draft_email"|"reschedule_calendar"|"none"), message, affected_routes (an array of route IDs, or [] if none), and reasoning (a short plain-language explanation of the evidence and decision). Write the message and reasoning for a traveler, not a developer: use plain language, explain what happened and what they should do, and avoid acronyms, raw IDs, JSON, markdown, and internal system terms.
Sensitivity policy: the configured sensitivity is ${normalizedSensitivity}/100. Favor an early, clearly labeled possible-delay warning over an on-time result when the evidence is credible. Do not wait for a posted service alert or a confirmed missed trip. For each route, classify it as disrupted and use action "trigger_ui_alert" when at least ${slowVehicleThreshold}% of its reporting vehicles are below 10 miles per hour, when multiple vehicles have missing positions, or when several vehicles are stopped or nearly stopped. A single isolated slow vehicle or one missing position is not enough by itself. When the evidence suggests risk but is not conclusive, say "possible delay" or "early warning" in the message and explain the evidence; do not claim a confirmed delay.
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

export async function analyze(itinerary: unknown[], telemetry: TelemetryRecord[], alerts: ServiceAlert[], sensitivity = 50): Promise<{ action: ActionPayload; status: "connected" | "failed" | "not_configured"; error?: string }> {
  const configuredEndpoint = process.env.BREV_NIM_ENDPOINT?.trim() || process.env.NEMOTRON_ENDPOINT?.trim();
  if (!configuredEndpoint) {
    log.warn("Nemotron is not configured; using deterministic fallback", { telemetry_count: telemetry.length });
    return { action: applySensitivity(fallbackAction(telemetry), telemetry, sensitivity), status: "not_configured" };
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
        messages: [{ role: "system", content: buildPrompt(itinerary, telemetry, alerts, sensitivity) }],
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
    return { action: applySensitivity(parseAction(content), telemetry, sensitivity), status: "connected" };
  } catch (error) {
    lastError = errorMessage(error);
    if (lastError.includes("HTTP 403")) lastError = `${lastError}; the Brev endpoint rejected access before model inference; verify the public NIM URL and proxy credentials`;
    if (lastError.includes("HTTP 404")) lastError = `${lastError}; verify the Brev NIM endpoint and exact model ID from /v1/models`;
    if (lastError.includes("HTTP 401")) lastError = `${lastError}; verify BREV_NIM_API_KEY or the proxy authentication scheme`;
    log.warn("Brev NIM request failed", { endpoint, model: configuredModel, error: lastError });
  }
  log.error("Brev NIM request failed; using deterministic fallback", { endpoint, model: configuredModel, error: lastError });
  return { action: applySensitivity(fallbackAction(telemetry), telemetry, sensitivity), status: "failed", error: lastError };
}
