import { countPrompt, getSession } from "../../../lib/pi.js";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 300; // Vercel: Hobby max 300 s, Pro up to 800 s

// POST {sessionId, message} -> NDJSON stream of chat events
export async function POST(req) {
	const { sessionId, message } = await req.json();
	if (!sessionId || !message) return Response.json({ error: "sessionId and message required" }, { status: 400 });

	const entry = await getSession(sessionId);
	if (entry.busy) return Response.json({ error: "session busy" }, { status: 409 });
	entry.busy = true;
	countPrompt();

	const enc = new TextEncoder();
	const t0 = performance.now();
	const c0 = process.cpuUsage();
	let firstTokenMs = null;
	let toolCalls = 0;

	const stream = new ReadableStream({
		start(controller) {
			const send = (obj) => controller.enqueue(enc.encode(JSON.stringify(obj) + "\n"));
			const unsubscribe = entry.session.subscribe((ev) => {
				if (ev.type === "message_update") {
					const a = ev.assistantMessageEvent;
					if (a.type === "text_delta") {
						firstTokenMs ??= performance.now() - t0;
						send({ t: "text", d: a.delta });
					} else if (a.type === "thinking_delta") {
						firstTokenMs ??= performance.now() - t0;
						send({ t: "thinking", d: a.delta });
					}
				} else if (ev.type === "tool_execution_start") {
					toolCalls++;
					send({ t: "tool_start", id: ev.toolCallId, name: ev.toolName, args: ev.args });
				} else if (ev.type === "tool_execution_end") {
					const text = (ev.result?.content || []).filter((c) => c.type === "text").map((c) => c.text).join("");
					send({ t: "tool_end", id: ev.toolCallId, name: ev.toolName, isError: ev.isError, output: text.slice(0, 2000) });
				}
			});
			entry.session
				.prompt(message)
				.then(() => {
					const c = process.cpuUsage(c0);
					entry.turns++;
					send({
						t: "done",
						wallMs: Math.round(performance.now() - t0),
						firstTokenMs: firstTokenMs && Math.round(firstTokenMs),
						serverCpuMs: +((c.user + c.system) / 1000).toFixed(1),
						toolCalls,
					});
				})
				.catch((e) => send({ t: "error", message: String(e?.message || e) }))
				.finally(() => {
					unsubscribe();
					entry.busy = false;
					controller.close();
				});
		},
	});
	return new Response(stream, { headers: { "content-type": "application/x-ndjson", "cache-control": "no-store" } });
}
