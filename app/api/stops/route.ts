import { getPrtStops } from "../../../lib/stops";
import { errorMessage, log } from "../../../lib/logging";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    return Response.json({ stops: await getPrtStops() });
  } catch (error) {
    log.error("Failed to load PRT stop catalog", { error: errorMessage(error) });
    return Response.json({ error: "The PRT stop catalog is temporarily unavailable." }, { status: 502 });
  }
}
