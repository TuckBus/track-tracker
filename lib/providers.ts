import GtfsRealtimeBindings from "gtfs-realtime-bindings";
import type { ServiceAlert, TelemetryRecord } from "./types";
import { log } from "./logging";

const userAgent = "ProjectDispatch/1.0";
const providerTimeoutMs = 15000;
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
