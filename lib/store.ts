import { Redis } from "@upstash/redis";
import type { ActionPayload, DispatchState } from "./types";

const key = "dispatch:state";
const initial: DispatchState = {
  last_poll_at: null,
  telemetry: [],
  itineraries: [
    { id: "pennsylvanian", label: "Pennsylvanian 42", mode: "rail", status: "Monitoring" },
    { id: "pit-flight", label: "PIT departure", mode: "air", status: "Monitoring" },
  ],
  alerts: [],
  provider_errors: [],
};

function redis(): Redis | null {
  if (!process.env.KV_REST_API_URL || !process.env.KV_REST_API_TOKEN) return null;
  return Redis.fromEnv();
}

export async function getState(): Promise<DispatchState> {
  const client = redis();
  if (!client) return initial;
  return (await client.get<DispatchState>(key)) || initial;
}

export async function saveState(state: DispatchState): Promise<DispatchState> {
  const client = redis();
  if (client) await client.set(key, state);
  return state;
}

export function appendAlert(state: DispatchState, alert: ActionPayload): DispatchState {
  return { ...state, alerts: [alert, ...state.alerts].slice(0, 50) };
}
