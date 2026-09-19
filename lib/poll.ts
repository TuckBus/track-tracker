import { analyze } from "./intelligence";
import { fetchAmtrak, fetchOpenSky, fetchPrt, fetchPrtAlerts } from "./providers";
import { appendAlert, getState, saveState } from "./store";
import type { ActionPayload, DispatchState, TelemetryRecord } from "./types";
import { errorMessage, log } from "./logging";
import { stagedTelemetry, type TestScenario } from "./test-telemetry";

export async function poll(options: { testScenario?: TestScenario; clearAlerts?: boolean } = {}): Promise<{ action: ActionPayload; state: DispatchState }> {
  const startedAt = Date.now();
  log.info("Starting telemetry poll", options.testScenario ? { test_scenario: options.testScenario } : undefined);
  if (options.testScenario) {
    const fixture = stagedTelemetry(options.testScenario);
    const current = await getState();
    const analysis = await analyze(current.itineraries, fixture.telemetry, fixture.service_alerts);
    const state: DispatchState = {
      ...current,
      alerts: options.clearAlerts ? [] : current.alerts,
      telemetry: fixture.telemetry,
      provider_errors: [],
      nemotron: { status: analysis.status, ...(analysis.error ? { error: analysis.error } : {}), ...(analysis.action.reasoning ? { explanation: analysis.action.reasoning } : {}), checked_at: new Date().toISOString() },
      service_alerts: fixture.service_alerts,
      last_poll_at: new Date().toISOString(),
    };
    const nextState = analysis.action.action !== "none" ? appendAlert(state, analysis.action) : state;
    await saveState(nextState);
    log.info("Staged telemetry poll completed", { test_scenario: options.testScenario, telemetry_count: fixture.telemetry.length, action: analysis.action.action, duration_ms: Date.now() - startedAt });
    return { action: analysis.action, state: nextState };
  }
  const providers = [
    ["prt", fetchPrt],
    ["amtrak", fetchAmtrak],
    ["opensky", fetchOpenSky],
  ] as const;
  const telemetry: TelemetryRecord[] = [];
  const errors: string[] = [];
  const serviceAlertsRequest = fetchPrtAlerts().catch((error) => {
    errors.push(`prt-alerts: ${errorMessage(error)}`);
    log.warn("PRT service alerts failed", { error: errorMessage(error) });
    return [];
  });
  const results = await Promise.all(providers.map(async ([name, provider]) => {
    try {
      const records = await provider();
      log.info("Telemetry provider completed", { provider: name, records: records.length });
      return { name, records };
    } catch (error) {
      const message = errorMessage(error);
      log.error("Telemetry provider failed", { provider: name, error: message });
      return { name, error: message };
    }
  }));
  for (const result of results) {
    if ("records" in result) telemetry.push(...(result.records || []));
    else errors.push(`${result.name}: ${result.error || "provider failed"}`);
  }
  const service_alerts = await serviceAlertsRequest;
  const current = await getState();
  const analysis = await analyze(current.itineraries, telemetry, service_alerts);
  const action = analysis.action;
  let state: DispatchState = {
    ...current,
    alerts: options.clearAlerts ? [] : current.alerts,
    telemetry,
    provider_errors: errors,
    nemotron: { status: analysis.status, ...(analysis.error ? { error: analysis.error } : {}), ...(analysis.action.reasoning ? { explanation: analysis.action.reasoning } : {}), checked_at: new Date().toISOString() },
    service_alerts,
    last_poll_at: new Date().toISOString(),
  };
  if (action.action !== "none") state = appendAlert(state, action);
  await saveState(state);
  log.info("Telemetry poll completed", {
    telemetry_count: telemetry.length,
    provider_error_count: errors.length,
    action: action.action,
    duration_ms: Date.now() - startedAt,
  });
  return { action, state };
}
