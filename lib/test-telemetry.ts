import type { ServiceAlert, TelemetryRecord } from "./types";

export const testScenarios = ["normal", "developing-delay", "posted-delay"] as const;
export type TestScenario = (typeof testScenarios)[number];

function record(vehicle_id: string, speed_mph: number | null, index: number, positionAvailable = true): TelemetryRecord {
  return {
    source: "prt",
    vehicle_id,
    route: "61A",
    latitude: positionAvailable ? 40.44 + index * 0.001 : null,
    longitude: positionAvailable ? -79.99 - index * 0.001 : null,
    speed_mph,
    altitude_ft: null,
    observed_at: new Date().toISOString(),
    metadata: { test_fixture: true, scenario_route: "61A" },
  };
}

export function stagedTelemetry(scenario: TestScenario): { telemetry: TelemetryRecord[]; service_alerts: ServiceAlert[] } {
  if (scenario === "normal") {
    return {
      telemetry: Array.from({ length: 8 }, (_, index) => record(`TEST-61A-${index + 1}`, 18 + index, index)),
      service_alerts: [],
    };
  }

  if (scenario === "posted-delay") {
    return {
      telemetry: Array.from({ length: 8 }, (_, index) => record(`TEST-61A-${index + 1}`, 2, index)),
      service_alerts: [{
        id: "test-posted-61a",
        header: "61A service delay",
        description: "Test PRT alert: route 61A is already reporting delays.",
        effect: "DELAY",
        routes: ["61A"],
        stops: [],
        updated_at: new Date().toISOString(),
      }],
    };
  }

  return {
    telemetry: [
      ...Array.from({ length: 7 }, (_, index) => record(`TEST-61A-${index + 1}`, 1.5, index)),
      record("TEST-61A-8", null, 7, false),
      record("TEST-61A-9", null, 8, false),
    ],
    service_alerts: [],
  };
}
