import { stats } from "../../../lib/pi.js";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
	return Response.json(stats());
}
