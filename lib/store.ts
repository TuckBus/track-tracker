import { Redis } from "@upstash/redis";
import type { ActionPayload, DispatchState } from "./types";

const key = "dispatch:state";
const initial: DispatchState = {
  last_poll_at: null,
  telemetry: [],
  itineraries: [
    { id: "prt-buses", label: "Pittsburgh Regional Transit buses", mode: "bus", status: "Monitoring" },
  ],
  alerts: [],
  provider_errors: [],
  nemotron: { status: "not_configured", checked_at: null },
  service_alerts: [],
};

function redis(): Redis | null {
  if (!process.env.KV_REST_API_URL || !process.env.KV_REST_API_TOKEN) return null;
  return Redis.fromEnv();
}

export async function getState(): Promise<DispatchState> {
  const client = redis();
  if (!client) return initial;
  const state = (await client.get<DispatchState>(key)) || initial;
  return {
    ...initial,
    ...state,
    telemetry: (state.telemetry || []).filter((item) => item.source === "prt"),
    itineraries: initial.itineraries,
    nemotron: state.nemotron || initial.nemotron,
    service_alerts: state.service_alerts || [],
  };
}

export async function saveState(state: DispatchState): Promise<DispatchState> {
  const client = redis();
  if (client) await client.set(key, state);
  return state;
}

export function appendAlert(state: DispatchState, alert: ActionPayload): DispatchState {
  return { ...state, alerts: [alert, ...state.alerts].slice(0, 50) };
}
