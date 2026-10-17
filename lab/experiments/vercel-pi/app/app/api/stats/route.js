import { snapshot } from "../../../lib/instrument.js";
import { piState } from "../../../lib/pi-common.js";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
	return Response.json({ ...snapshot(), piLoaded: !!piState.mod, piLoad: piState.load, sessions: piState.sessions.size });
}
