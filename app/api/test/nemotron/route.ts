import { poll } from "../../../../lib/poll";
import { testScenarios, type TestScenario } from "../../../../lib/test-telemetry";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function localOnly() {
  return process.env.NODE_ENV !== "production" && !process.env.VERCEL;
}

export async function GET() {
  if (!localOnly()) return Response.json({ error: "Test mode is available only in local development." }, { status: 404 });
  return Response.json({ scenarios: testScenarios });
}

export async function POST(request: Request) {
  if (!localOnly()) return Response.json({ error: "Test mode is available only in local development." }, { status: 404 });
  const body = await request.json().catch(() => ({})) as { scenario?: string };
  if (!body.scenario || !testScenarios.includes(body.scenario as TestScenario)) {
    return Response.json({ error: `scenario must be one of: ${testScenarios.join(", ")}` }, { status: 400 });
  }
  return Response.json(await poll({ testScenario: body.scenario as TestScenario }));
}
