import { getState } from "../../../lib/store";
import { log } from "../../../lib/logging";
import { getPrtStops, getPrtStopRoutes } from "../../../lib/stops";
import { fetchPrt } from "../../../lib/providers";
import { analyze } from "../../../lib/intelligence";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function normalizeLine(value: string) {
  return value.toLowerCase().replace(/[\s-]/g, "");
}

export async function POST(request: Request) {
  const body = await request.json() as { stop?: string; stop_id?: string; stop_routes?: string[]; line?: string };
  const stop = body.stop?.trim();
  const stopId = body.stop_id?.trim();
  const line = body.line?.trim();
  if (!stop || !line) return Response.json({ error: "Both stop and line are required." }, { status: 400 });

  const state = await getState();
  const suppliedRoutes = Array.isArray(body.stop_routes) && body.stop_routes.length > 0 ? body.stop_routes : undefined;
  const stops = await getPrtStops();
  const selectedStop = stops.find((candidate) => candidate.id === stopId || candidate.name.toLowerCase() === stop.toLowerCase());
  const servedRoutes = suppliedRoutes || (selectedStop ? await getPrtStopRoutes(selectedStop.id) : []);
  const lineIsServed = servedRoutes.some((route) => normalizeLine(route) === normalizeLine(line));
  let liveTelemetry = state.telemetry;
  try {
    liveTelemetry = await fetchPrt();
    log.info("Fetched fresh PRT telemetry for anomaly lookup", { records: liveTelemetry.length });
  } catch (error) {
    log.warn("Fresh PRT telemetry unavailable during anomaly lookup", { error: error instanceof Error ? error.message : String(error) });
  }
  const distanceMiles = (latitude: number, longitude: number) => {
    if (!selectedStop) return Number.POSITIVE_INFINITY;
    const latitudeDelta = (latitude - selectedStop.latitude) * 69;
    const longitudeDelta = (longitude - selectedStop.longitude) * 53;
    return Math.sqrt(latitudeDelta ** 2 + longitudeDelta ** 2);
  };
  const matching = liveTelemetry.filter((item) =>
    item.source === "prt"
    && item.route !== null
    && normalizeLine(item.route) === normalizeLine(line)
    && item.latitude !== null
    && item.longitude !== null
    && distanceMiles(item.latitude, item.longitude) <= 1.5,
  );
  const analysis = selectedStop && matching.length > 0
    ? await analyze(
      [{ stop, stop_id: selectedStop.id, line, radius_miles: 1.5, purpose: "Decide whether this route is anomalous near this selected stop." }],
      matching,
      [],
      "stop",
    )
    : null;
  const nemotronUnavailable = analysis !== null && analysis.status !== "connected";
  const anomaly = analysis?.action.status === "anomalous";
  const result = {
    stop,
    line,
    status: anomaly ? "anomalous" : nemotronUnavailable || !selectedStop || !lineIsServed ? "unknown" : "on_time",
    message: anomaly
      ? analysis?.action.message || "Nemotron identified an anomaly near the selected stop."
      : nemotronUnavailable
        ? `Nemotron could not evaluate line ${line} near ${stop}: ${analysis?.error || "AI analysis is unavailable"}.`
        : !selectedStop
          ? "The selected stop could not be matched to the PRT stop catalog."
          : !lineIsServed
        ? `The PRT schedule does not list line ${line} at ${stop}. Check the line name or choose another stop.`
        : matching.length === 0
          ? `Line ${line} serves ${stop}, but no vehicle position is currently reporting within 1.5 miles.`
          : "Nemotron did not identify an anomaly in the nearby route telemetry.",
    vehicle_count: matching.length,
    observed_at: matching[0]?.observed_at || liveTelemetry.find((item) => item.source === "prt")?.observed_at || state.last_poll_at,
  };
  log.info("Bus stop Nemotron anomaly lookup completed", { stop, stop_id: selectedStop?.id, line, served_routes: servedRoutes, status: result.status, nemotron_status: analysis?.status || "not_run", vehicle_count: matching.length });
  return Response.json(result);
}
