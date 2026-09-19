import GtfsRealtimeBindings from "gtfs-realtime-bindings";
import type { TelemetryRecord } from "./types";

const userAgent = "ProjectDispatch/1.0";

async function responseBytes(url: string): Promise<Uint8Array> {
  const response = await fetch(url, { headers: { "User-Agent": userAgent }, cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return new Uint8Array(await response.arrayBuffer());
}

export async function fetchPrt(url = process.env.PRT_VEHICLE_POSITIONS_URL || "https://truetime.portauthority.org/gtfsrt/vehiclePositions"): Promise<TelemetryRecord[]> {
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
      metadata: { current_status: vehicle.currentStatus },
    }];
  });
}

export async function fetchOpenSky(url = process.env.OPENSKY_STATES_URL || "https://opensky-network.org/api/states/all"): Promise<TelemetryRecord[]> {
  const query = new URLSearchParams({ lamin: "40.2", lamax: "40.7", lomin: "-80.5", lomax: "-79.6" });
  const response = await fetch(`${url}?${query}`, { headers: { "User-Agent": userAgent }, cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json() as { states?: unknown[][] };
  const observed = new Date().toISOString();
  return (payload.states || []).flatMap((state) => {
    if (state.length < 11 || state[5] == null || state[6] == null) return [];
    return [{
      source: "opensky",
      vehicle_id: String(state[1] || state[0]),
      route: null,
      latitude: Number(state[6]),
      longitude: Number(state[5]),
      speed_mph: state[9] == null ? null : Number(state[9]) * 2.23694,
      altitude_ft: state[7] == null ? null : Number(state[7]) * 3.28084,
      observed_at: observed,
      metadata: { callsign: String(state[1] || "").trim(), on_ground: state[8] },
    }];
  });
}

export async function fetchAmtrak(): Promise<TelemetryRecord[]> {
  const url = process.env.AMTRAK_TELEMETRY_URL;
  if (!url) return [];
  const response = await fetch(url, { headers: { "User-Agent": userAgent }, cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
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
