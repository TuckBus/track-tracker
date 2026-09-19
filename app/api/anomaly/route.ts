import { getState } from "../../../lib/store";
import { log } from "../../../lib/logging";
import { getPrtStops, getPrtStopRoutes } from "../../../lib/stops";
import { fetchPrt } from "../../../lib/providers";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const body = await request.json() as { stop?: string; stop_id?: string; stop_routes?: string[]; line?: string };
  const stop = body.stop?.trim();
  const stopId = body.stop_id?.trim();
  const line = body.line?.trim();
  if (!stop || !line) return Response.json({ error: "Both stop and line are required." }, { status: 400 });

  const state = await getState();
  const stops = body.stop_routes ? [] : await getPrtStops();
  const selectedStop = stops.find((candidate) => candidate.id === stopId || candidate.name.toLowerCase() === stop.toLowerCase());
  const servedRoutes = body.stop_routes || (selectedStop ? await getPrtStopRoutes(selectedStop.id) : []);
  const normalizeLine = (value: string) => value.toLowerCase().replace(/[\s-]/g, "");
  const lineIsServed = servedRoutes.some((route) => normalizeLine(route) === normalizeLine(line));
  let liveTelemetry = state.telemetry;
  try {
    liveTelemetry = await fetchPrt();
    log.info("Fetched fresh PRT telemetry for anomaly lookup", { records: liveTelemetry.length });
  } catch (error) {
    log.warn("Fresh PRT telemetry unavailable during anomaly lookup", { error: error instanceof Error ? error.message : String(error) });
  }
  const matching = liveTelemetry.filter((item) => item.source === "prt" && item.route !== null && normalizeLine(item.route) === normalizeLine(line));
  const slow = matching.filter((item) => item.speed_mph !== null && item.speed_mph < 2);
  const anomaly = slow.length > 0 && lineIsServed;
  const result = {
    stop,
    line,
    status: anomaly ? "anomalous" : !lineIsServed ? "unknown" : "on_time",
    message: anomaly
      ? `${slow.length} vehicle${slow.length === 1 ? "" : "s"} on line ${line} ${slow.length === 1 ? "is" : "are"} moving unusually slowly in the latest feed for ${stop}.`
      : !lineIsServed
        ? `The PRT schedule does not list line ${line} at ${stop}. Check the line name or choose another stop.`
        : matching.length > 0
          ? `No active speed anomaly is visible for line ${line} at ${stop}.`
          : `Line ${line} serves ${stop}, but no vehicle position is currently reporting. This is not evidence that the bus is absent.`,
    vehicle_count: matching.length,
    observed_at: liveTelemetry.find((item) => item.source === "prt")?.observed_at || state.last_poll_at,
  };
  log.info("Bus stop anomaly lookup completed", { stop, stop_id: selectedStop?.id, line, served_routes: servedRoutes, status: result.status, vehicle_count: matching.length });
  return Response.json(result);
}
