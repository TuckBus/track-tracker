import { afterEach, describe, expect, it } from "vitest";
import { GET, POST } from "../app/api/test/nemotron/route";

const originalNodeEnv = process.env.NODE_ENV;
const originalVercel = process.env.VERCEL;
const originalBrevEndpoint = process.env.BREV_NIM_ENDPOINT;
const originalBrevApiKey = process.env.BREV_NIM_API_KEY;
const originalBrevModel = process.env.BREV_NIM_MODEL;

afterEach(() => {
  if (originalNodeEnv === undefined) delete process.env.NODE_ENV;
  else process.env.NODE_ENV = originalNodeEnv;
  if (originalVercel === undefined) delete process.env.VERCEL;
  else process.env.VERCEL = originalVercel;
  if (originalBrevEndpoint === undefined) delete process.env.BREV_NIM_ENDPOINT;
  else process.env.BREV_NIM_ENDPOINT = originalBrevEndpoint;
  if (originalBrevApiKey === undefined) delete process.env.BREV_NIM_API_KEY;
  else process.env.BREV_NIM_API_KEY = originalBrevApiKey;
  if (originalBrevModel === undefined) delete process.env.BREV_NIM_MODEL;
  else process.env.BREV_NIM_MODEL = originalBrevModel;
});

describe("local staged Nemotron API", () => {
  it("lists supported scenarios during local development", async () => {
    delete process.env.VERCEL;
    process.env.NODE_ENV = "test";

    const response = await GET();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      scenarios: ["normal", "developing-delay", "posted-delay"],
    });
  });

  it("rejects unsupported scenarios before polling", async () => {
    delete process.env.VERCEL;
    process.env.NODE_ENV = "test";

    const response = await POST(new Request("http://localhost/api/test/nemotron", {
      method: "POST",
      body: JSON.stringify({ scenario: "unknown" }),
      headers: { "Content-Type": "application/json" },
    }));

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({
      error: "scenario must be one of: normal, developing-delay, posted-delay",
    });
  });

  it("runs a supported staged scenario through the polling pipeline", async () => {
    delete process.env.VERCEL;
    process.env.NODE_ENV = "test";
    delete process.env.BREV_NIM_ENDPOINT;
    delete process.env.BREV_NIM_API_KEY;
    delete process.env.BREV_NIM_MODEL;

    const response = await POST(new Request("http://localhost/api/test/nemotron", {
      method: "POST",
      body: JSON.stringify({ scenario: "normal" }),
      headers: { "Content-Type": "application/json" },
    }));
    const result = await response.json();

    expect(response.status).toBe(200);
    expect(result.state.telemetry).toHaveLength(8);
    expect(result.state.telemetry.every((item: { vehicle_id: string }) => item.vehicle_id.startsWith("TEST-"))).toBe(true);
    expect(result.state.nemotron.status).toBe("not_configured");
    expect(result.action.action).toBe("none");
  });

  it("hides the test endpoint in production and on Vercel", async () => {
    process.env.NODE_ENV = "production";
    expect((await GET()).status).toBe(404);

    process.env.NODE_ENV = "test";
    process.env.VERCEL = "1";
    expect((await GET()).status).toBe(404);
  });
});
