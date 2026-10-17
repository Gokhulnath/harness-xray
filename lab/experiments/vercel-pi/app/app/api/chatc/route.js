// Variant C: this Vercel function only proxies. pi runs in a Rivet Actor on Rivet Compute; tools run in E2B.
import { createClient } from "rivetkit/client";
import { begin, snapshot } from "../../../lib/instrument.js";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 300;

const g = globalThis;
g.__rivet ??= null;
function client() {
	g.__rivet ??= createClient({ endpoint: process.env.RIVET_ENDPOINT || "https://api.rivet.dev", namespace: process.env.RIVET_NAMESPACE, token: process.env.RIVET_TOKEN });
	return g.__rivet;
}

const delta = (a, b, k) => (a?.[k] != null && b?.[k] != null ? +(b[k] - a[k]).toFixed(1) : null);

// POST {sessionId, message} -> NDJSON stream (same event shapes as /api/chat), then one "done" record
export async function POST(req) {
	const { sessionId, message, whenBusy } = await req.json();
	if (!sessionId || !message) return Response.json({ error: "sessionId and message required" }, { status: 400 });
	const finish = begin();
	const t0 = performance.now();
	const at = () => Math.round(performance.now() - t0);
	const before = snapshot();
	const enc = new TextEncoder();

	const stream = new ReadableStream({
		start(controller) {
			const send = (obj) => controller.enqueue(enc.encode(JSON.stringify(obj) + "\n"));
			(async () => {
				const timeline = [];
				const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, requests: 0 };
				const eventTypes = {};
				const toolStart = new Map();
				let firstTokenMs = null;
				// Text of the in-flight assistant message, per content block. Pi Durable sends the first text of a block in
				// message_start, text_start, "block" or "message" changes, and only later pieces as text_delta.
				let blocks = [];
				let printed = ""; // what we have streamed of the current message
				let running = false;
				let onRunEnd = null;
				let conn;
				const textOf = (content) => (content || []).map((b) => (b?.type === "text" ? b.text || "" : null));
				const flush = () => {
					const full = blocks.filter((t) => t != null).join("");
					if (full.length > printed.length && full.startsWith(printed)) {
						firstTokenMs ??= at();
						send({ t: "text", d: full.slice(printed.length) });
						printed = full;
					}
				};
				try {
					const handle = client().agent.getOrCreate([sessionId]);
					const w0 = await handle.labStats(); // first call: creates/wakes the actor (and the Rivet runner if scaled to zero)
					const firstCallMs = at();
					conn = handle.connect();
					conn.on("pi.events", ({ events }) => {
						for (const e of events) {
							eventTypes[e.type] = (eventTypes[e.type] || 0) + 1;
							if (e.type === "message_start") {
								blocks = e.message?.role === "assistant" ? textOf(e.message.content) : [];
								printed = "";
								flush();
							} else if (e.type === "message_update") {
								for (const ch of e.changes || []) {
									if (ch.type === "text_delta") blocks[ch.contentIndex] = (blocks[ch.contentIndex] || "") + ch.delta;
									else if (ch.type === "text_start" || ch.type === "block") blocks[ch.contentIndex] = ch.block?.type === "text" ? ch.block.text || "" : null;
									else if (ch.type === "message") blocks = textOf(ch.message?.content);
									else if (ch.type === "thinking_delta") {
										firstTokenMs ??= at();
										send({ t: "thinking", d: ch.delta });
									}
								}
								flush();
							} else if (e.type === "message_end") {
								const m = e.entry?.model?.[0];
								if (m?.role === "assistant") {
									blocks = textOf(m.content);
									const full = blocks.filter((t) => t != null).join("");
									if (full.startsWith(printed)) flush();
									else send({ t: "text", d: "\n" + full }); // the stream diverged from the final message: show the final one
									blocks = [];
									printed = "";
									const u = m.usage || {};
									usage.input += u.input || 0;
									usage.output += u.output || 0;
									usage.cacheRead += u.cacheRead || 0;
									usage.cacheWrite += u.cacheWrite || 0;
									usage.requests++;
									timeline.push({ kind: "model", endMs: at() });
								}
							} else if (e.type === "run_start") {
								running = true;
							} else if (e.type === "run_end") {
								running = false;
								onRunEnd?.();
							} else if (e.type === "tool_execution_start") {
								toolStart.set(e.toolCallId, at());
								send({ t: "tool_start", id: e.toolCallId, name: e.toolName, args: e.args });
							} else if (e.type === "tool_execution_end") {
								const m = e.entry?.model?.[0];
								const text = (m?.content || []).filter((c) => c.type === "text").map((c) => c.text).join("");
								const isError = !!(m?.isError ?? e.result?.isError);
								timeline.push({ kind: "tool", name: e.toolName, startMs: toolStart.get(e.toolCallId), endMs: at(), isError });
								send({ t: "tool_end", id: e.toolCallId, name: e.toolName, isError, output: text.slice(0, 2000) });
							}
						}
					});
					const root = await conn.harness.root();
					await conn.conversation.watchEvents(root.id);
					const watchReadyMs = at();
					const result = await conn.prompt(message, { requestId: crypto.randomUUID(), ...(whenBusy ? { whenBusy } : {}) });
					const promptDoneMs = at();
					// prompt() can resolve before the last events (final text, message_end, run_end) reach us: wait for them
					if (running) await new Promise((r) => ((onRunEnd = r), setTimeout(r, 5000)));
					const w1 = await handle.labStats();
					const m = finish();
					send({
						t: "done",
						variant: "c",
						firstTokenMs,
						...m,
						usage,
						timeline,
						eventTypes,
						result: { status: result?.status, reason: result?.reason },
						phases: { firstCallMs, watchReadyMs, promptDoneMs },
						worker: {
							instanceId: w1.instanceId,
							sameInstance: w0.instanceId === w1.instanceId,
							instanceAgeS: w1.instanceAgeS,
							processCpuMs: delta(w0, w1, "cpuMs"),
							cgroupCpuMs: w0.cgroup?.cpuMs != null ? +(w1.cgroup.cpuMs - w0.cgroup.cpuMs).toFixed(1) : null,
							rssBeforeMiB: w0.rssMiB,
							rssAfterMiB: w1.rssMiB,
							peakRssMiB: w1.peakRssMiB,
							cgroupMemMiB: w1.cgroup?.memMiB,
							cgroupPeakMiB: w1.cgroup?.peakMiB,
							before: w0,
							after: w1,
							sandboxEvents: (w1.sandboxEvents || []).filter((s) => s.at >= Date.now() - (performance.now() - t0) - 1000),
						},
						before,
						after: snapshot(),
					});
				} catch (e) {
					finish();
					send({ t: "error", message: String(e?.message || e) });
				} finally {
					try {
						await conn?.dispose();
					} catch {}
					controller.close();
				}
			})();
		},
	});
	return new Response(stream, { headers: { "content-type": "application/x-ndjson", "cache-control": "no-store" } });
}
