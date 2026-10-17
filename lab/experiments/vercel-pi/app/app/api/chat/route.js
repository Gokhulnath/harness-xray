import { begin, snapshot } from "../../../lib/instrument.js";
import { getSession, loadPi, MODEL_ID, piState, PROVIDER } from "../../../lib/pi-common.js";
import { e2bKill, e2bMetrics, e2bWorkspace } from "../../../lib/tools-e2b.js";
import { localWorkspace } from "../../../lib/tools-local.js";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 300; // Hobby maximum

// POST {variant: "a"|"b", sessionId, message, cleanup?} -> NDJSON stream of chat events, then one "done" record
export async function POST(req) {
	const { variant = "a", sessionId, message, cleanup = false, scenario = "cart" } = await req.json();
	if (!sessionId || !message || !["a", "b"].includes(variant)) return Response.json({ error: "variant a|b, sessionId, message required" }, { status: 400 });

	const finish = begin();
	const t0 = performance.now();
	const at = () => Math.round(performance.now() - t0);
	const before = snapshot();
	let setup;
	let entry;
	try {
		const pi = await loadPi();
		const key = `${variant}:${sessionId}`;
		const tSetup = performance.now();
		setup = variant === "a" ? (piState.sessions.has(key) ? { cwd: null } : localWorkspace(sessionId, scenario)) : await e2bWorkspace(sessionId, pi, scenario);
		const setupMs = Math.round(performance.now() - tSetup);
		entry = await getSession(key, setup.cwd ? setup : { cwd: `/tmp/ws-${sessionId}` });
		entry.setupMs = setupMs;
	} catch (e) {
		finish();
		return Response.json({ error: String(e?.message || e) }, { status: 500 });
	}
	if (entry.busy) {
		finish();
		return Response.json({ error: "session busy" }, { status: 409 });
	}
	const stored = piState.sessions.get(`${variant}:${sessionId}`);
	stored.busy = true;

	const enc = new TextEncoder();
	const timeline = [];
	const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, requests: 0 };
	let firstTokenMs = null;

	const stream = new ReadableStream({
		start(controller) {
			const send = (obj) => controller.enqueue(enc.encode(JSON.stringify(obj) + "\n"));
			const tools = new Map();
			const unsubscribe = stored.session.subscribe((ev) => {
				if (ev.type === "message_update") {
					const a = ev.assistantMessageEvent;
					if (a.type === "text_delta" || a.type === "thinking_delta") {
						firstTokenMs ??= at();
						send({ t: a.type === "text_delta" ? "text" : "thinking", d: a.delta });
					}
				} else if (ev.type === "message_end" && ev.message?.role === "assistant") {
					const u = ev.message.usage || {};
					usage.input += u.input || 0;
					usage.output += u.output || 0;
					usage.cacheRead += u.cacheRead || 0;
					usage.cacheWrite += u.cacheWrite || 0;
					usage.requests++;
					timeline.push({ kind: "model", endMs: at() });
				} else if (ev.type === "tool_execution_start") {
					tools.set(ev.toolCallId, at());
					send({ t: "tool_start", id: ev.toolCallId, name: ev.toolName, args: ev.args });
				} else if (ev.type === "tool_execution_end") {
					const text = (ev.result?.content || []).filter((c) => c.type === "text").map((c) => c.text).join("");
					timeline.push({ kind: "tool", name: ev.toolName, startMs: tools.get(ev.toolCallId), endMs: at(), isError: ev.isError });
					send({ t: "tool_end", id: ev.toolCallId, name: ev.toolName, isError: ev.isError, output: text.slice(0, 2000) });
				}
			});
			stored.session
				.prompt(message)
				.then(async () => {
					const sandboxMetrics = variant === "b" ? await e2bMetrics(sessionId) : null;
					if (cleanup && variant === "b") await e2bKill(sessionId);
					const m = finish();
					send({
						t: "done",
						variant,
						scenario,
						model: `${PROVIDER}/${MODEL_ID}`,
						firstTokenMs,
						...m,
						piLoad: piState.load,
						sessionCreated: entry.created,
						sessionCreateMs: entry.createMs,
						setupMs: entry.setupMs,
						usage,
						timeline,
						sandbox: variant === "b" ? { ...setup.stats, roundTripMs: Math.round(setup.stats.roundTripMs), metrics: sandboxMetrics } : null,
						before,
						after: snapshot(),
					});
				})
				.catch((e) => {
					finish();
					send({ t: "error", message: String(e?.message || e) });
				})
				.finally(() => {
					unsubscribe();
					stored.busy = false;
					if (cleanup) piState.sessions.delete(`${variant}:${sessionId}`);
					controller.close();
				});
		},
	});
	return new Response(stream, { headers: { "content-type": "application/x-ndjson", "cache-control": "no-store" } });
}
