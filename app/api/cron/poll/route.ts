import { poll } from "../../../../lib/poll";

export const runtime = "nodejs";
export const maxDuration = 60;

export async function GET(request: Request) {
  const secret = process.env.CRON_SECRET;
  const authorization = request.headers.get("authorization");
  if (secret && authorization !== `Bearer ${secret}`) return Response.json({ error: "Unauthorized" }, { status: 401 });
  return Response.json(await poll());
}
