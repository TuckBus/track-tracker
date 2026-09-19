import { appendAlert, getState, saveState } from "../../../../../lib/store";
import type { ActionPayload, Action } from "../../../../../lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request, context: { params: Promise<{ actionId: string }> }) {
  const { actionId } = await context.params;
  const body = await request.json() as { type?: string };
  if (body.type !== "draft_email" && body.type !== "reschedule_calendar") {
    return Response.json({ error: "Unsupported mitigation action" }, { status: 400 });
  }
  const state = await getState();
  const source = state.alerts.find((alert) => alert.id === actionId);
  if (!source) return Response.json({ error: "Action not found" }, { status: 404 });
  const executed: ActionPayload = {
    id: crypto.randomUUID(),
    status: source.status,
    action: body.type as Action,
    message: `${body.type === "draft_email" ? "Draft email" : "Calendar reschedule"} prepared for your review.`,
    source: "dispatch",
    created_at: new Date().toISOString(),
  };
  const next = appendAlert(state, executed);
  await saveState(next);
  return Response.json({ action: executed, state: next });
}
