import { describe, expect, it } from "vitest";
import { applySensitivity, buildPrompt, fallbackAction, parseAction } from "../lib/intelligence";
import { stagedTelemetry } from "../lib/test-telemetry";

describe("Nemotron intelligence", () => {
  it("parses JSON embedded in a model response and adds friendly defaults", () => {
    const action = parseAction(
      'Reasoning trace removed. {"status":"disrupted","action":"trigger_ui_alert","message":"Route 61A is delayed.","affected_routes":["61A"],"reasoning":"Vehicles are moving very slowly."}',
    );

    expect(action.source).toBe("nemotron");
    expect(action.status).toBe("disrupted");
    expect(action.action).toBe("trigger_ui_alert");
    expect(action.affected_routes).toEqual(["61A"]);
    expect(action.reasoning).toBe("Vehicles are moving very slowly.");
  });

  it("rejects malformed model responses", () => {
    expect(() => parseAction("not JSON")).toThrow("did not contain a JSON object");
    expect(() => parseAction({ status: "unknown", action: "none", message: "Bad" })).toThrow("does not match the action schema");
  });

  it("uses deterministic fallback behavior for stationary trains", () => {
    const train = {
      source: "amtrak",
      vehicle_id: "TEST-TRAIN",
      route: "Pennsylvanian",
      latitude: 40.4,
      longitude: -80,
      speed_mph: 0,
      altitude_ft: null,
      observed_at: new Date().toISOString(),
      metadata: {},
    };

    expect(fallbackAction([train])).toMatchObject({
      status: "anomalous",
      action: "trigger_ui_alert",
      source: "dispatch-fallback",
    });
  });

  it("includes posted alerts and early-warning signals in the prompt", () => {
    const fixture = stagedTelemetry("posted-delay");
    const prompt = buildPrompt([], fixture.telemetry, fixture.service_alerts);

    expect(prompt).toContain("POSTED PRT SERVICE ALERTS");
    expect(prompt).toContain("test-posted-61a");
    expect(prompt).toContain("DERIVED EARLY-WARNING SIGNALS");
    expect(prompt).not.toContain("ignore already-posted");
  });

  it("uses an early-warning policy for credible but unconfirmed delays", () => {
    const prompt = buildPrompt([], stagedTelemetry("developing-delay").telemetry, []);

    expect(prompt).toContain("Filter noise aggressively");
    expect(prompt).toContain("null speed means the sensor did not report a usable speed");
    expect(prompt).toContain("at least 50% of at least three vehicles with measurable speeds");
    expect(prompt).toContain("posted notices alone are not enough");
    expect(prompt).toContain("do not claim a confirmed delay");
    expect(prompt).toContain('"slow_under_10_mph":7');
    expect(prompt).toContain('"missing_position_count":2');
  });

  it("adjusts the slow-vehicle threshold from the configured sensitivity", () => {
    expect(buildPrompt([], [], [], 0)).toContain("at least 100% of at least three vehicles with measurable speeds");
    expect(buildPrompt([], [], [], 100)).toContain("at least 0% of at least three vehicles with measurable speeds");
  });

  it("enforces sensitivity when the model returns no warning", () => {
    const telemetry = stagedTelemetry("developing-delay").telemetry.map((item, index) => ({
      ...item,
      speed_mph: index < 4 ? 1.5 : 15,
      latitude: 40.4,
      longitude: -80,
    }));
    const onTime = parseAction({ status: "on_time", action: "none", message: "Looks good." });

    expect(applySensitivity(onTime, telemetry, 0).action).toBe("none");
    expect(applySensitivity(onTime, telemetry, 100).action).toBe("trigger_ui_alert");
  });

  it("does not treat null speeds or missing positions as a delay signal", () => {
    const telemetry = stagedTelemetry("developing-delay").telemetry.map((item) => ({
      ...item,
      speed_mph: null,
    }));
    const onTime = parseAction({ status: "on_time", action: "none", message: "Looks good." });

    expect(applySensitivity(onTime, telemetry, 100).action).toBe("none");
  });

  it("requires at least two measurable slow vehicles before warning", () => {
    const telemetry = stagedTelemetry("developing-delay").telemetry.map((item, index) => ({
      ...item,
      speed_mph: index === 0 ? 1 : index === 1 ? 2 : 25,
    }));
    const onTime = parseAction({ status: "on_time", action: "none", message: "Looks good." });

    expect(applySensitivity(onTime, telemetry, 50).action).toBe("none");
  });
});
