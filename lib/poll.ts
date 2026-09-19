import { analyze } from "./intelligence";
import { fetchAmtrak, fetchOpenSky, fetchPrt } from "./providers";
import { appendAlert, getState, saveState } from "./store";
import type { ActionPayload, DispatchState, TelemetryRecord } from "./types";

export async function poll(): Promise<{ action: ActionPayload; state: DispatchState }> {
  const providers = [
    ["prt", fetchPrt],
    ["amtrak", fetchAmtrak],
    ["opensky", fetchOpenSky],
  ] as const;
  const telemetry: TelemetryRecord[] = [];
  const errors: string[] = [];
  for (const [name, provider] of providers) {
    try {
      telemetry.push(...await provider());
    } catch (error) {
      errors.push(`${name}: ${error instanceof Error ? error.message : "provider failed"}`);
    }
  }
  const current = await getState();
  const action = await analyze(current.itineraries, telemetry);
  let state: DispatchState = { ...current, telemetry, provider_errors: errors, last_poll_at: new Date().toISOString() };
  if (action.action !== "none") state = appendAlert(state, action);
  await saveState(state);
  return { action, state };
}
