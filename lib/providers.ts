import GtfsRealtimeBindings from "gtfs-realtime-bindings";
import type { ServiceAlert, TelemetryRecord } from "./types";
import { log } from "./logging";

const userAgent = "ProjectDispatch/1.0";
const providerTimeoutMs = 15000;
const airports = [
  { code: "PIT", latitude: 40.4915, longitude: -80.2329 },
  { code: "AGC", latitude: 40.3544, longitude: -79.9302 },
  { code: "LBE", latitude: 40.2759, longitude: -79.4048 },
];

async function responseBytes(url: string): Promise<Uint8Array> {
  log.info("Fetching telemetry provider", { provider: "prt", url });
  const response = await fetch(url, { headers: { "User-Agent": userAgent }, cache: "no-store", signal: AbortSignal.timeout(providerTimeoutMs) });
  if (!response.ok) {
    log.warn("Telemetry provider returned an error", { provider: "prt", url, status: response.status });
    throw new Error(`HTTP ${response.status}`);
  }
  return new Uint8Array(await response.arrayBuffer());
}

export async function fetchPrt(url = process.env.PRT_VEHICLE_POSITIONS_URL || "https://truetime.portauthority.org/gtfsrt-bus/vehicles"): Promise<TelemetryRecord[]> {
  let feed;
  try {
    feed = GtfsRealtimeBindings.transit_realtime.FeedMessage.decode(await responseBytes(url));
  } catch (error) {
    throw new Error(`PRT feed was not valid GTFS-RT protobuf: ${error instanceof Error ? error.message : "unknown error"}`);
  }

  const observed = new Date().toISOString();
  return feed.entity.flatMap((entity) => {
    const vehicle = entity.vehicle;
    if (!vehicle) return [];
    const position = vehicle.position;
    return [{
      source: "prt",
      vehicle_id: vehicle.vehicle?.id || entity.id,
      route: vehicle.trip?.routeId || null,
      latitude: position?.latitude ?? null,
      longitude: position?.longitude ?? null,
      speed_mph: position?.speed ? position.speed * 2.23694 : null,
      altitude_ft: null,
      observed_at: observed,
      metadata: {
        current_status: vehicle.currentStatus,
        trip_id: vehicle.trip?.tripId,
        direction_id: vehicle.trip?.directionId,
        current_stop_sequence: vehicle.currentStopSequence,
        current_stop_id: vehicle.stopId,
      },
    }];
  });
}

export async function fetchPrtAlerts(url = process.env.PRT_SERVICE_ALERTS_URL || "https://truetime.portauthority.org/gtfsrt-bus/alerts"): Promise<ServiceAlert[]> {
  log.info("Fetching PRT service alerts", { provider: "prt-alerts", url });
  const response = await fetch(url, { headers: { "User-Agent": userAgent }, cache: "no-store", signal: AbortSignal.timeout(providerTimeoutMs) });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const feed = GtfsRealtimeBindings.transit_realtime.FeedMessage.decode(new Uint8Array(await response.arrayBuffer()));
  return feed.entity.flatMap((entity) => {
    const alert = entity.alert;
    if (!alert) return [];
    const text = (value: unknown) => {
      const translations = (value as { translation?: { text?: string }[] } | undefined)?.translation || [];
      return translations[0]?.text || "";
    };
    const informed = alert.informedEntity || [];
    return [{
      id: entity.id,
      header: text(alert.headerText) || "PRT service alert",
      description: text(alert.descriptionText),
      effect: String(alert.effect || "UNKNOWN"),
      routes: informed.flatMap((item) => item.routeId ? [String(item.routeId)] : []),
      stops: informed.flatMap((item) => item.stopId ? [String(item.stopId)] : []),
      updated_at: new Date().toISOString(),
    }];
  });
}

export async function fetchOpenSky(url = process.env.OPENSKY_STATES_URL || "https://opensky-network.org/api/states/all"): Promise<TelemetryRecord[]> {
  const query = new URLSearchParams({ lamin: "40.2", lamax: "40.7", lomin: "-80.5", lomax: "-79.6" });
  log.info("Fetching telemetry provider", { provider: "opensky", url });
  let response: Response;
  try {
    response = await fetch(`${url}${url.includes("?") ? "&" : "?"}${query}`, { headers: { "User-Agent": userAgent }, cache: "no-store", signal: AbortSignal.timeout(providerTimeoutMs) });
  } catch (error) {
    const cause = error instanceof Error && error.cause instanceof Error ? `; cause: ${error.cause.message}` : "";
    throw new Error(`OpenSky request failed for ${url}: ${error instanceof Error ? error.message : String(error)}${cause}`);
  }
  if (!response.ok) {
    const body = (await response.text()).slice(0, 300);
    log.warn("Telemetry provider returned an error", { provider: "opensky", url, status: response.status });
    throw new Error(`OpenSky HTTP ${response.status}${body ? `: ${body}` : ""}`);
  }
  const payload = await response.json() as { states?: unknown[][] };
  const observed = new Date().toISOString();
  return (payload.states || []).flatMap((state) => {
    if (state.length < 11 || state[5] == null || state[6] == null) return [];
    const latitude = Number(state[6]);
    const longitude = Number(state[5]);
    const airport = airports.find((candidate) => Math.hypot((latitude - candidate.latitude) * 69, (longitude - candidate.longitude) * 53) <= 25);
    if (!airport) return [];
    return [{
      source: "opensky",
      vehicle_id: String(state[1] || state[0]),
      route: null,
      latitude,
      longitude,
      speed_mph: state[9] == null ? null : Number(state[9]) * 2.23694,
      altitude_ft: state[7] == null ? null : Number(state[7]) * 3.28084,
      observed_at: observed,
      metadata: { callsign: String(state[1] || "").trim(), on_ground: state[8], airport: airport.code },
    }];
  });
}

export async function fetchAmtrak(): Promise<TelemetryRecord[]> {
  const url = process.env.AMTRAK_TELEMETRY_URL;
  if (!url) {
    log.warn("Amtrak telemetry is not configured; no train positions will be shown");
    return [];
  }
  log.info("Fetching telemetry provider", { provider: "amtrak", url });
  const response = await fetch(url, { headers: { "User-Agent": userAgent }, cache: "no-store", signal: AbortSignal.timeout(providerTimeoutMs) });
  if (!response.ok) {
    log.warn("Telemetry provider returned an error", { provider: "amtrak", url, status: response.status });
    throw new Error(`HTTP ${response.status}`);
  }
  const payload = await response.json() as unknown;
  const items = Array.isArray(payload) ? payload : ((payload as { trains?: unknown[] }).trains || []);
  return items.flatMap((value) => {
    const item = value as Record<string, unknown>;
    const number = String(item.trainNumber ?? item.number ?? "");
    if (!["42", "43"].includes(number)) return [];
    return [{
      source: "amtrak",
      vehicle_id: number,
      route: "Pennsylvanian",
      latitude: typeof item.latitude === "number" ? item.latitude : null,
      longitude: typeof item.longitude === "number" ? item.longitude : null,
      speed_mph: typeof item.speed === "number" ? item.speed : null,
      altitude_ft: null,
      observed_at: new Date().toISOString(),
      metadata: item,
    }];
  });
}
