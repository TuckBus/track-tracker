import { describe, expect, it } from "vitest";
import { buildPrompt, fallbackAction, parseAction } from "../lib/intelligence";
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

    expect(prompt).toContain("Favor an early, clearly labeled possible-delay warning");
    expect(prompt).toContain("at least 25% of its reporting vehicles are below 10 miles per hour");
    expect(prompt).toContain("multiple vehicles have missing positions");
    expect(prompt).toContain("do not claim a confirmed delay");
    expect(prompt).toContain('"slow_under_10_mph":7');
    expect(prompt).toContain('"missing_position_count":2');
  });

  it("adjusts the slow-vehicle threshold from the configured sensitivity", () => {
    expect(buildPrompt([], [], [], 0)).toContain("at least 50% of its reporting vehicles");
    expect(buildPrompt([], [], [], 100)).toContain("at least 10% of its reporting vehicles");
  });
});
