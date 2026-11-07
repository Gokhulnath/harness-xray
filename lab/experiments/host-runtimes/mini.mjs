// The bare agent loop, no framework: send history -> parse the SSE stream -> run the read tool -> save every
// message to the session's SQLite file -> repeat until the model answers. Same work as mini-rs.
//   node mini.mjs <sessions> <turns>
import { DatabaseSync } from "node:sqlite";
import { mkdirSync, readFileSync, rmSync } from "node:fs";
const S = Number(process.argv[2] || 1), T = Number(process.argv[3] || 1);
const URL_ = `http://127.0.0.1:${process.env.PORT || 18080}/v1/chat/completions`;
const TOOLS = JSON.parse(readFileSync(new URL("./tools.json", import.meta.url), "utf8"));
const DIR = `${process.env.TMPDIR || "/tmp"}/mini-node-${process.pid}`;
mkdirSync(DIR, { recursive: true });
(await import("node:fs")).writeFileSync(`${DIR}/hello.txt`, "hello from the workspace\n");

function open(i) {
	const db = new DatabaseSync(`${DIR}/s${i}.sqlite`);
	db.exec("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; CREATE TABLE entries (seq INTEGER PRIMARY KEY, json TEXT NOT NULL)");
	return { msgs: [], db, ins: db.prepare("INSERT INTO entries (seq, json) VALUES (?, ?)"), seq: 0 };
}
const save = (s, m) => { s.ins.run(s.seq++, JSON.stringify(m)); s.msgs.push(m); };

async function turn(s, text) {
	save(s, { role: "user", content: text });
	for (;;) {
		const res = await fetch(URL_, { method: "POST", headers: { "content-type": "application/json", authorization: "Bearer x" },
			body: JSON.stringify({ model: "mock", messages: s.msgs, stream: true, stream_options: { include_usage: true }, max_completion_tokens: 8192, tools: TOOLS }) });
		const reader = res.body.getReader(), dec = new TextDecoder();
		let buf = "", out = "";
		const calls = [];
		for (;;) {
			const { done, value } = await reader.read();
			if (done) break;
			buf += dec.decode(value, { stream: true });
			let i;
			while ((i = buf.indexOf("\n\n")) >= 0) {
				const line = buf.slice(0, i);
				buf = buf.slice(i + 2);
				if (!line.startsWith("data: ") || line === "data: [DONE]") continue;
				const d = JSON.parse(line.slice(6)).choices?.[0]?.delta;
				if (!d) continue;
				if (d.content) out += d.content;
				for (const tc of d.tool_calls || []) {
					const c = (calls[tc.index] ??= { id: "", name: "", args: "" });
					if (tc.id) c.id = tc.id;
					if (tc.function?.name) c.name = tc.function.name;
					if (tc.function?.arguments) c.args += tc.function.arguments;
				}
			}
		}
		save(s, { role: "assistant", content: out || null, ...(calls.length ? { tool_calls: calls.map((c) => ({ id: c.id, type: "function", function: { name: c.name, arguments: c.args } })) } : {}) });
		if (!calls.length) return;
		for (const c of calls) save(s, { role: "tool", tool_call_id: c.id, content: readFileSync(`${DIR}/${JSON.parse(c.args).path}`, "utf8") });
	}
}

const sessions = Array.from({ length: S }, (_, i) => open(i));
await Promise.all(sessions.map(async (s, i) => { for (let t = 0; t < T; t++) await turn(s, `turn ${t}: read hello.txt and summarise it`); }));
const rows = sessions.reduce((n, s) => n + s.seq, 0);
for (const s of sessions) s.db.close();
rmSync(DIR, { recursive: true, force: true });
console.log(JSON.stringify({ impl: "mini-node", sessions: S, turns: S * T, rows }));
