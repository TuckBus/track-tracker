import AdmZip from "adm-zip";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { log } from "./logging";

export type PrtStop = {
  id: string;
  name: string;
  latitude: number;
  longitude: number;
  routes?: string[];
};
type StopCatalog = {
  stops: PrtStop[];
  routesByStop: Map<string, string[]>;
  stopSequencesByRouteDirection: Map<string, Map<string, { min: number; max: number }>>;
};

let cachedCatalog: StopCatalog | null = null;
let loading: Promise<StopCatalog> | null = null;

function parseCsvLine(line: string) {
  const fields: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"') quoted = !quoted;
    else if (character === "," && !quoted) {
      fields.push(current);
      current = "";
    } else current += character;
  }
  fields.push(current);
  return fields.map((value) => value.trim().replace(/^"(.*)"$/, "$1").replace(/""/g, '"'));
}

async function loadStops(): Promise<StopCatalog> {
  const feedPath = path.join(process.cwd(), "public", "GTFS.zip");
  log.info("Reading bundled PRT static GTFS stop catalog", { path: feedPath });
  const archive = new AdmZip(await readFile(feedPath));
  const entry = archive.getEntry("stops.txt");
  if (!entry) throw new Error("PRT GTFS static feed did not contain stops.txt");
  const lines = entry.getData().toString("utf8").split(/\r?\n/).filter(Boolean);
  const headers = parseCsvLine(lines.shift() || "");
  const column = (name: string) => headers.indexOf(name);
  const idIndex = column("stop_id");
  const nameIndex = column("stop_name");
  const latIndex = column("stop_lat");
  const lonIndex = column("stop_lon");
  if ([idIndex, nameIndex, latIndex, lonIndex].some((index) => index < 0)) throw new Error("PRT stops.txt is missing a required column");
  const stops = lines.flatMap((line) => {
    const values = parseCsvLine(line);
    const latitude = Number(values[latIndex]);
    const longitude = Number(values[lonIndex]);
    if (!values[idIndex] || !values[nameIndex] || !Number.isFinite(latitude) || !Number.isFinite(longitude)) return [];
    return [{ id: values[idIndex], name: values[nameIndex], latitude, longitude }];
  });
  const routeNames = new Map<string, string>();
  const routesEntry = archive.getEntry("routes.txt");
  if (routesEntry) {
    const routeLines = routesEntry.getData().toString("utf8").split(/\r?\n/).filter(Boolean);
    const routeHeaders = parseCsvLine(routeLines.shift() || "");
    const routeIdIndex = routeHeaders.indexOf("route_id");
    const shortNameIndex = routeHeaders.indexOf("route_short_name");
    if (routeIdIndex >= 0 && shortNameIndex >= 0) {
      for (const routeLine of routeLines) {
        const values = parseCsvLine(routeLine);
        if (values[routeIdIndex] && values[shortNameIndex]) routeNames.set(values[routeIdIndex], values[shortNameIndex]);
      }
    }
  }
  const tripRoutes = new Map<string, { route: string; direction: string }>();
  const tripsEntry = archive.getEntry("trips.txt");
  if (tripsEntry) {
    const tripLines = tripsEntry.getData().toString("utf8").split(/\r?\n/).filter(Boolean);
    const tripHeaders = parseCsvLine(tripLines.shift() || "");
    const tripIdIndex = tripHeaders.indexOf("trip_id");
    const tripRouteIndex = tripHeaders.indexOf("route_id");
    const tripDirectionIndex = tripHeaders.indexOf("direction_id");
    if (tripIdIndex >= 0 && tripRouteIndex >= 0 && tripDirectionIndex >= 0) {
      for (const tripLine of tripLines) {
        const values = parseCsvLine(tripLine);
        const route = routeNames.get(values[tripRouteIndex]);
        if (values[tripIdIndex] && route) tripRoutes.set(values[tripIdIndex], { route, direction: values[tripDirectionIndex] });
      }
    }
  }
  const routesByStop = new Map<string, Set<string>>();
  const stopTimesEntry = archive.getEntry("stop_times.txt");
  if (stopTimesEntry) {
    const stopTimeLines = stopTimesEntry.getData().toString("utf8").split(/\r?\n/).filter(Boolean);
    const stopTimeHeaders = parseCsvLine(stopTimeLines.shift() || "");
    const stopTimeIdIndex = stopTimeHeaders.indexOf("stop_id");
    const stopTimeTripIndex = stopTimeHeaders.indexOf("trip_id");
    const stopTimeSequenceIndex = stopTimeHeaders.indexOf("stop_sequence");
    const stopSequencesByRouteDirection = new Map<string, Map<string, { min: number; max: number }>>();
    if (stopTimeIdIndex >= 0 && stopTimeTripIndex >= 0 && stopTimeSequenceIndex >= 0) {
      for (const stopTimeLine of stopTimeLines) {
        const values = parseCsvLine(stopTimeLine);
        const trip = tripRoutes.get(values[stopTimeTripIndex]);
        const sequence = Number(values[stopTimeSequenceIndex]);
        if (values[stopTimeIdIndex] && trip && Number.isFinite(sequence)) {
          const routeName = trip.route;
          const routes = routesByStop.get(values[stopTimeIdIndex]) || new Set<string>();
          routes.add(routeName);
          routesByStop.set(values[stopTimeIdIndex], routes);
          const routeKey = `${values[stopTimeIdIndex]}|${routeName}`;
          const directions = stopSequencesByRouteDirection.get(routeKey) || new Map<string, { min: number; max: number }>();
          const current = directions.get(trip.direction) || { min: sequence, max: sequence };
          current.min = Math.min(current.min, sequence);
          current.max = Math.max(current.max, sequence);
          directions.set(trip.direction, current);
          stopSequencesByRouteDirection.set(routeKey, directions);
        }
      }
      const normalizedRoutes = new Map<string, string[]>();
      for (const [stopId, routes] of routesByStop) normalizedRoutes.set(stopId, [...routes].sort());
      log.info("Loaded PRT static GTFS stop catalog", { stop_count: stops.length, stops_with_routes: normalizedRoutes.size });
      return { stops: stops.map((stop) => ({ ...stop, routes: normalizedRoutes.get(stop.id) || [] })), routesByStop: normalizedRoutes, stopSequencesByRouteDirection };
    }
  }
  const normalizedRoutes = new Map<string, string[]>();
  for (const [stopId, routes] of routesByStop) normalizedRoutes.set(stopId, [...routes].sort());
  log.info("Loaded PRT static GTFS stop catalog", { stop_count: stops.length, stops_with_routes: normalizedRoutes.size });
  return { stops: stops.map((stop) => ({ ...stop, routes: normalizedRoutes.get(stop.id) || [] })), routesByStop: normalizedRoutes, stopSequencesByRouteDirection: new Map() };
}

export async function getPrtStops() {
  if (cachedCatalog) return cachedCatalog.stops;
  loading ??= loadStops().then((catalog) => {
    cachedCatalog = catalog;
    return catalog;
  }).finally(() => {
    loading = null;
  });
  return loading.then((catalog) => catalog.stops);
}

export async function getPrtStopRoutes(stopId: string) {
  if (!cachedCatalog) await getPrtStops();
  return cachedCatalog?.routesByStop.get(stopId) || [];
}

export async function getPrtStopSequences(stopId: string, route: string) {
  if (!cachedCatalog) await getPrtStops();
  return cachedCatalog?.stopSequencesByRouteDirection.get(`${stopId}|${route}`) || new Map<string, { min: number; max: number }>();
}
