import { describe, expect, it } from "vitest";
import { stagedTelemetry, testScenarios } from "../lib/test-telemetry";

describe("staged telemetry fixtures", () => {
  it("exposes the supported scenarios", () => {
    expect(testScenarios).toEqual(["normal", "developing-delay", "posted-delay"]);
  });

  it("generates normal telemetry without a service alert", () => {
    const fixture = stagedTelemetry("normal");

    expect(fixture.telemetry).toHaveLength(8);
    expect(fixture.service_alerts).toHaveLength(0);
    expect(fixture.telemetry.every((item) => item.metadata.test_fixture === true)).toBe(true);
    expect(fixture.telemetry.every((item) => item.speed_mph !== null && item.speed_mph >= 18)).toBe(true);
  });

  it("generates a developing delay with slow and missing vehicle positions", () => {
    const fixture = stagedTelemetry("developing-delay");

    expect(fixture.telemetry).toHaveLength(9);
    expect(fixture.service_alerts).toHaveLength(0);
    expect(fixture.telemetry.filter((item) => item.speed_mph === null)).toHaveLength(2);
    expect(fixture.telemetry.filter((item) => item.speed_mph !== null && item.speed_mph < 5)).toHaveLength(7);
    expect(new Set(fixture.telemetry.map((item) => item.route))).toEqual(new Set(["61A"]));
  });

  it("includes a posted PRT alert in the posted-delay scenario", () => {
    const fixture = stagedTelemetry("posted-delay");

    expect(fixture.telemetry).toHaveLength(8);
    expect(fixture.service_alerts).toHaveLength(1);
    expect(fixture.service_alerts[0].routes).toEqual(["61A"]);
    expect(fixture.service_alerts[0].effect).toBe("DELAY");
  });
});
