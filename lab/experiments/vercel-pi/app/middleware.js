// Every page and API route needs the lab token: header x-lab-token (scripts) or the cookie set by /login.
// /login and /api/login are open; /api/rivet/* is left to Rivet's own auth. The token never goes in a URL.
import { NextResponse } from "next/server";

export function middleware(req) {
	const token = process.env.LAB_TOKEN;
	if (!token) return new NextResponse("LAB_TOKEN not configured", { status: 503 });
	if (req.headers.get("x-lab-token") === token || req.cookies.get("lab_token")?.value === token) return NextResponse.next();
	if (req.nextUrl.pathname.startsWith("/api/")) return new NextResponse("unauthorized", { status: 401 });
	return NextResponse.redirect(new URL("/login", req.url));
}

export const config = { matcher: ["/((?!login|api/login|api/rivet|_next/static|_next/image|favicon.ico).*)"] };
