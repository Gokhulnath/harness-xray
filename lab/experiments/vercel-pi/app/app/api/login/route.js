import { timingSafeEqual } from "node:crypto";

export const runtime = "nodejs";

// POST {token} -> sets the httpOnly lab cookie when it matches LAB_TOKEN
export async function POST(req) {
	const { token } = await req.json().catch(() => ({}));
	const want = Buffer.from(process.env.LAB_TOKEN || "");
	const got = Buffer.from(String(token || ""));
	if (!want.length || got.length !== want.length || !timingSafeEqual(got, want)) return Response.json({ ok: false }, { status: 401 });
	const res = Response.json({ ok: true });
	res.headers.append("set-cookie", `lab_token=${process.env.LAB_TOKEN}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=604800`);
	return res;
}
