import type { ActionPayload, TelemetryRecord } from "./types";

const statuses = new Set(["disrupted", "on_time", "anomalous"]);
const actions = new Set(["trigger_ui_alert", "draft_email", "reschedule_calendar", "none"]);

function payload(status: ActionPayload["status"], action: ActionPayload["action"], message: string, source: string): ActionPayload {
  return { id: crypto.randomUUID(), status, action, message, source, created_at: new Date().toISOString() };
}

export function buildPrompt(itinerary: unknown[], telemetry: TelemetryRecord[]): string {
  return `You are a silent transit anomaly compiler. Return ONLY one JSON object with status ("disrupted"|"on_time"|"anomalous"), action ("trigger_ui_alert"|"draft_email"|"reschedule_calendar"|"none"), and message. Do not provide markdown or conversational text.
ITINERARY:
${JSON.stringify(itinerary)}
TELEMETRY:
${JSON.stringify(telemetry)}`;
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
  return payload(value.status as ActionPayload["status"], value.action as ActionPayload["action"], value.message.trim(), "nemotron");
}

export function fallbackAction(telemetry: TelemetryRecord[]): ActionPayload {
  if (telemetry.some((item) => item.source === "amtrak" && item.speed_mph !== null && item.speed_mph < 1)) {
    return payload("anomalous", "trigger_ui_alert", "Pennsylvanian telemetry reports a stationary train; review your itinerary.", "dispatch-fallback");
  }
  return payload("on_time", "none", "No actionable disruption detected.", "dispatch-fallback");
}

export async function analyze(itinerary: unknown[], telemetry: TelemetryRecord[]): Promise<ActionPayload> {
  const endpoint = process.env.NEMOTRON_ENDPOINT;
  if (!endpoint) return fallbackAction(telemetry);
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(process.env.NEMOTRON_API_KEY ? { Authorization: `Bearer ${process.env.NEMOTRON_API_KEY}` } : {}) },
      body: JSON.stringify({
        model: process.env.NEMOTRON_MODEL || "nvidia/nemotron",
        messages: [{ role: "system", content: buildPrompt(itinerary, telemetry) }],
        temperature: 0,
        response_format: { type: "json_object" },
      }),
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`Nemotron HTTP ${response.status}`);
    const result = await response.json() as { choices?: { message?: { content?: string } }[] };
    const content = result.choices?.[0]?.message?.content;
    if (!content) throw new Error("Nemotron response did not contain message content");
    return parseAction(content);
  } catch (error) {
    console.error("Nemotron request failed; using deterministic fallback", error);
    return fallbackAction(telemetry);
  }
}
