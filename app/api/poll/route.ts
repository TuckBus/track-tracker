import { poll } from "../../../lib/poll";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function POST() {
  return Response.json(await poll());
}
