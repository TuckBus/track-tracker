import { poll } from "../../../lib/poll";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({})) as { clear_alerts?: unknown };
  return Response.json(await poll({ clearAlerts: body.clear_alerts === true }));
}
