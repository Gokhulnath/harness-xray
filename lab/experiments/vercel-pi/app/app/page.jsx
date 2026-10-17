"use client";
import { useEffect, useRef, useState } from "react";

const newId = () => "s-" + Math.random().toString(36).slice(2, 10);

export default function Chat() {
	const [sessionId, setSessionId] = useState("");
	const [items, setItems] = useState([]); // {role:"user"|"assistant", text, thinking, tools:[], meta}
	const [input, setInput] = useState("Run the tests in the workspace with npm test, find the bugs in cart.js, fix them, and re-run the tests until they pass. Keep the final answer short.");
	const [busy, setBusy] = useState(false);
	const [variant, setVariant] = useState("a");
	const [stats, setStats] = useState(null);
	const endRef = useRef(null);

	useEffect(() => {
		setSessionId(newId());
	}, []);
	useEffect(() => {
		const tick = () => fetch("/api/stats").then((r) => r.json()).then(setStats).catch(() => {});
		tick();
		const t = setInterval(tick, 1500);
		return () => clearInterval(t);
	}, []);
	useEffect(() => {
		endRef.current?.scrollIntoView({ block: "end" }); // braces: newer Chrome returns a Promise here, which React would treat as a cleanup
	}, [items]);

	const update = (fn) =>
		setItems((prev) => {
			const next = prev.slice();
			const last = { ...next[next.length - 1] };
			last.tools = last.tools.slice();
			fn(last);
			next[next.length - 1] = last;
			return next;
		});

	async function send(e) {
		e.preventDefault();
		const message = input.trim();
		if (!message || busy) return;
		setInput("");
		setBusy(true);
		setItems((p) => [...p, { role: "user", text: message, tools: [] }, { role: "assistant", text: "", thinking: "", tools: [], meta: null }]);
		try {
			const res = await fetch(variant === "c" ? "/api/chatc" : "/api/chat", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ variant, sessionId, message }) });
			if (!res.ok) throw new Error((await res.json()).error || res.statusText);
			const reader = res.body.getReader();
			const dec = new TextDecoder();
			let buf = "";
			for (;;) {
				const { done, value } = await reader.read();
				if (done) break;
				buf += dec.decode(value, { stream: true });
				let i;
				while ((i = buf.indexOf("\n")) >= 0) {
					const ev = JSON.parse(buf.slice(0, i));
					buf = buf.slice(i + 1);
					update((m) => {
						if (ev.t === "text") m.text += ev.d;
						else if (ev.t === "thinking") m.thinking += ev.d;
						else if (ev.t === "tool_start") m.tools.push({ id: ev.id, name: ev.name, args: ev.args, output: null });
						else if (ev.t === "tool_end") {
							const k = m.tools.findIndex((t) => t.id === ev.id);
							if (k >= 0) m.tools[k] = { ...m.tools[k], output: ev.output, isError: ev.isError };
						} else if (ev.t === "done") m.meta = ev;
						else if (ev.t === "error") m.text += `\n\n[error] ${ev.message}`;
					});
				}
			}
		} catch (err) {
			update((m) => (m.text += `\n\n[error] ${err.message}`));
		} finally {
			setBusy(false);
		}
	}

	return (
		<main className="wrap">
			<section className="chat">
				<header>
					<h1>pi on Vercel</h1>
					<p>
						<select value={variant} onChange={(e) => (setVariant(e.target.value), setSessionId(newId()), setItems([]))} disabled={busy}>
							<option value="a">A: pi + tools inside Vercel</option>
							<option value="b">B: pi in Vercel, tools in E2B</option>
							<option value="c">C: pi on Rivet, tools in E2B</option>
						</select>{" "}
						Session <code>{sessionId}</code>{" "}
						<button type="button" onClick={() => (setSessionId(newId()), setItems([]))} disabled={busy}>
							New session
						</button>
					</p>
				</header>
				<div className="log">
					{items.map((m, i) => (
						<div key={i} className={`msg ${m.role}`}>
							{m.thinking ? (
								<details className="thinking">
									<summary>thinking ({m.thinking.length} chars)</summary>
									<pre>{m.thinking}</pre>
								</details>
							) : null}
							{m.tools.map((t) => (
								<details key={t.id} className={`tool ${t.isError ? "err" : ""}`}>
									<summary>
										<b>{t.name}</b> <code>{summarize(t.args)}</code> {t.output === null ? "…" : t.isError ? "✗" : "✓"}
									</summary>
									{t.output !== null ? <pre>{t.output}</pre> : null}
								</details>
							))}
							{m.text ? <div className="text">{m.text}</div> : m.role === "assistant" && busy && i === items.length - 1 ? <div className="text dim">…</div> : null}
							{m.meta ? (
								<div className="meta">
									{(m.meta.wallMs / 1000).toFixed(1)} s · first token {m.meta.firstTokenMs ?? "–"} ms · function CPU {m.meta.processCpuMs} ms · tool processes {m.meta.toolChildCpuMs ?? 0} ms · peak RSS {m.meta.peakRssMiB} MiB · {m.meta.cold ? "cold" : "warm"} instance {m.meta.instanceId}{m.meta.sandbox ? ` · E2B create ${m.meta.sandbox.createMs} ms, ${Object.values(m.meta.sandbox.calls).reduce((a, b) => a + b, 0)} calls` : ""}{m.meta.worker ? ` · Rivet worker CPU ${m.meta.worker.processCpuMs} ms, RSS ${m.meta.worker.rssAfterMiB} MiB` : ""}
								</div>
							) : null}
						</div>
					))}
					<div ref={endRef} />
				</div>
				<form onSubmit={send}>
					<textarea value={input} onChange={(e) => setInput(e.target.value)} rows={2} onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send(e)} />
					<button disabled={busy || !input.trim()}>{busy ? "Working…" : "Send"}</button>
				</form>
			</section>
			<aside className="stats">
				<h2>Vercel function instance</h2>
				{stats ? (
					<dl>
						<dt>RSS</dt>
						<dd>{stats.rssMiB} MiB</dd>
						<dt>Heap used</dt>
						<dd>{stats.heapUsedMiB} MiB</dd>
						<dt>CPU since start</dt>
						<dd>{stats.cpuMs} ms</dd>
						<dt>pi loaded</dt>
						<dd>{stats.piLoaded ? `yes (${stats.piLoad.cpuMs} ms CPU, +${stats.piLoad.rssMiB} MiB)` : "not yet"}</dd>
						<dt>Instance</dt>
						<dd>{stats.instanceId} ({stats.region || "local"}, {stats.instanceAgeS} s old)</dd>
						<dt>Live sessions</dt>
						<dd>{stats.sessions}</dd>
						<dt>Invocations</dt>
						<dd>{stats.invocations} (in flight {stats.inFlight})</dd>
					</dl>
				) : (
					<p>…</p>
				)}
			</aside>
		</main>
	);
}

function summarize(args) {
	if (!args) return "";
	const s = args.command || args.path || JSON.stringify(args);
	return s.length > 70 ? s.slice(0, 70) + "…" : s;
}
