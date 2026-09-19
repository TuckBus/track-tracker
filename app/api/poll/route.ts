import { poll } from "../../../lib/poll";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({})) as { sensitivity?: unknown; clear_alerts?: unknown };
  const sensitivity = typeof body.sensitivity === "number" && Number.isFinite(body.sensitivity) ? Math.max(0, Math.min(100, body.sensitivity)) : undefined;
  return Response.json(await poll({ sensitivity, clearAlerts: body.clear_alerts === true }));
}
