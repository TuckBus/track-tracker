export type Status = "disrupted" | "on_time" | "anomalous";
export type Action = "trigger_ui_alert" | "draft_email" | "reschedule_calendar" | "none";

export type TelemetryRecord = {
  source: string;
  vehicle_id: string;
  route: string | null;
  latitude: number | null;
  longitude: number | null;
  speed_mph: number | null;
  altitude_ft: number | null;
  observed_at: string;
  metadata: Record<string, unknown>;
};

export type ActionPayload = {
  id: string;
  status: Status;
  action: Action;
  message: string;
  source: string;
  created_at: string;
  affected_routes?: string[];
  reasoning?: string;
};

export type DispatchState = {
  last_poll_at: string | null;
  telemetry: TelemetryRecord[];
  itineraries: { id: string; label: string; mode: string; status: string }[];
  alerts: ActionPayload[];
  provider_errors: string[];
  nemotron: { status: "connected" | "failed" | "not_configured"; error?: string; explanation?: string; checked_at: string | null };
  service_alerts: ServiceAlert[];
};

export type ServiceAlert = {
  id: string;
  header: string;
  description: string;
  effect: string;
  routes: string[];
  stops: string[];
  updated_at: string;
};
