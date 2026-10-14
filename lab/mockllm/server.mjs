// Harness-agnostic mock LLM for load-testing coding agents.
//
// Protocols: OpenAI Chat Completions (/v1/chat/completions), Anthropic Messages (/v1/messages,
// /v1/messages/count_tokens), OpenAI Responses (/v1/responses). Streaming and non-streaming.
//
// Scenario: a line in the user prompt   SCENARIO: <name> key=value key="quoted value" ...
// Each step of a scenario is an INTENT (shell, read, write, edit, grep, find, ls, text, subagent).
// The mock resolves each intent against the tools the harness actually advertised in the request
// (names + JSON schemas), so the same scenario drives pi, Claude Code, Codex, opencode, ... unchanged.
// If a harness has no tool for an intent, the step falls back to an equivalent shell command.
//
// Every request is logged as JSONL (size, messages, tools, chosen tool + mapping, timing).

import http from "node:http";
import fs from "node:fs";
import crypto from "node:crypto";
import { performance } from "node:perf_hooks";

const PORT = Number(process.env.PORT || 8080);
const DEFAULT_TPS = Number(process.env.MOCK_TPS || 0); // chunks per second; 0 = as fast as possible
const CHUNK_CHARS = Number(process.env.MOCK_CHUNK_CHARS || 4);
const WS = process.env.MOCK_WORKSPACE || "/workspace";

let logFd = null;
function openLog(path) {
	try {
		fs.mkdirSync(path.replace(/\/[^/]*$/, ""), { recursive: true });
		logFd = fs.openSync(path, "a");
	} catch (e) {
		console.error("mock log open failed", e.message);
	}
}
openLog(process.env.MOCK_LOG || "/lab/out/mockllm-default.jsonl");
const now = () => performance.timeOrigin + performance.now();
const log = (rec) => {
	if (logFd !== null) try { fs.writeSync(logFd, JSON.stringify(rec) + "\n"); } catch {}
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const int = (v, d) => (v === undefined || v === "" ? d : Number(v));
const abs = (p) => (!p || p === "." ? WS : p.startsWith("/") ? p : `${WS}/${p}`);
const rid = (p) => `${p}${crypto.randomBytes(6).toString("hex")}`;

// ---------------------------------------------------------------- text helpers
function textOf(content) {
	if (typeof content === "string") return content;
	if (Array.isArray(content)) return content.map((c) => (typeof c === "string" ? c : c.text ?? c.input_text ?? (typeof c.content === "string" ? c.content : ""))).join("");
	return "";
}
function filler(n) {
	const w = ["the", "agent", "loop", "streams", "tokens", "while", "tools", "run", "in", "child", "processes", "and", "memory", "grows", "with", "context", "**bold**", "`code`", "\n\n- item", "\n\n```js\nconst x = 1;\n```\n"];
	let s = "";
	for (let i = 0; s.length < n; i++) s += w[i % w.length] + " ";
	return s.slice(0, n);
}
function parseScenarioText(txt) {
	const m = (txt || "").match(/SCENARIO:\s*([\w-]+)([^\n]*)/);
	if (!m) return null;
	const params = {};
	const re = /(\w+)=("([^"]*)"|'([^']*)'|(\S+))/g;
	let mm;
	while ((mm = re.exec(m[2]))) params[mm[1]] = mm[3] ?? mm[4] ?? mm[5];
	return { name: m[1], params };
}

// ---------------------------------------------------------------- protocol normalization
// -> { msgs: [{role, text, toolCalls, toolResults}], tools: [{name, schema, kind}], system, model, stream, maxTokens }
function normalize(proto, body) {
	const out = { model: body.model, stream: !!body.stream, msgs: [], tools: [], system: "", maxTokens: body.max_tokens ?? body.max_completion_tokens ?? body.max_output_tokens };
	if (proto === "openai-chat") {
		for (const m of body.messages || []) {
			if (m.role === "system" || m.role === "developer") out.system += textOf(m.content) + "\n";
			else if (m.role === "tool") out.msgs.push({ role: "tool", text: textOf(m.content) });
			else out.msgs.push({ role: m.role, text: textOf(m.content), toolCalls: (m.tool_calls || []).length });
		}
		for (const t of body.tools || []) if (t.function) out.tools.push({ name: t.function.name, schema: t.function.parameters || {}, kind: "function" });
	} else if (proto === "anthropic") {
		out.system = typeof body.system === "string" ? body.system : textOf(body.system || []);
		for (const m of body.messages || []) {
			const blocks = Array.isArray(m.content) ? m.content : [{ type: "text", text: m.content }];
			const tr = blocks.filter((b) => b.type === "tool_result");
			const tu = blocks.filter((b) => b.type === "tool_use");
			const txt = blocks.filter((b) => b.type === "text").map((b) => b.text).join("");
			if (m.role === "user" && tr.length) out.msgs.push({ role: "tool", text: tr.map((b) => textOf(b.content)).join("\n") + (txt ? "\n" + txt : ""), toolResults: tr.length });
			else out.msgs.push({ role: m.role, text: txt, toolCalls: tu.length });
		}
		for (const t of body.tools || []) if (t.name) out.tools.push({ name: t.name, schema: t.input_schema || {}, kind: t.type && t.type !== "custom" ? t.type : "function" });
	} else if (proto === "openai-responses") {
		out.system = (body.instructions || "") + "\n";
		const input = typeof body.input === "string" ? [{ type: "message", role: "user", content: body.input }] : body.input || [];
		let inAssistant = false;
		for (const it of input) {
			const ty = it.type || (it.role ? "message" : "");
			if (ty === "message") {
				if (it.role === "system" || it.role === "developer") { out.system += textOf(it.content) + "\n"; continue; }
				out.msgs.push({ role: it.role, text: textOf(it.content) });
				inAssistant = it.role === "assistant";
			} else if (/_call$/.test(ty) || ty === "reasoning") {
				if (!inAssistant) out.msgs.push({ role: "assistant", text: "", toolCalls: 0 });
				if (ty !== "reasoning") out.msgs[out.msgs.length - 1].toolCalls++;
				inAssistant = true;
			} else if (/_call_output$/.test(ty)) {
				out.msgs.push({ role: "tool", text: typeof it.output === "string" ? it.output : JSON.stringify(it.output) });
				inAssistant = false;
			}
		}
		for (const t of body.tools || []) {
			if (t.type === "function") out.tools.push({ name: t.name, schema: t.parameters || {}, kind: "function" });
			else if (t.type === "local_shell") out.tools.push({ name: "local_shell", schema: {}, kind: "local_shell" });
			else if (t.type === "custom") out.tools.push({ name: t.name, schema: {}, kind: "custom" });
		}
	}
	return out;
}

// ---------------------------------------------------------------- intent -> harness tool
const ALIASES = {
	shell: [/^bash$/i, /^shell$/i, /^local_shell$/i, /^exec_command$/i, /^run_shell_command$/i, /^execute_command$/i, /^run_command$/i, /^terminal$/i, /^shell_command$/i, /bash/i, /shell/i, /command/i],
	read: [/^read$/i, /^read_file$/i, /^readfile$/i, /^view$/i, /read/i, /view/i],
	write: [/^write$/i, /^write_file$/i, /^writefile$/i, /^create_file$/i, /^create$/i, /write/i],
	edit: [/^edit$/i, /^edit_file$/i, /^replace$/i, /^str_replace_based_edit_tool$/i, /^str_replace_editor$/i, /^multiedit$/i, /^apply_patch$/i, /edit/i, /replace/i, /patch/i],
	grep: [/^grep$/i, /^search_file_content$/i, /^grep_search$/i, /^search$/i, /^rg$/i, /grep/i, /search/i],
	find: [/^find$/i, /^glob$/i, /^file_search$/i, /^list_files$/i, /glob/i, /find/i],
	ls: [/^ls$/i, /^list_directory$/i, /^list_dir$/i, /^list$/i, /list_dir/i, /^ls/i],
};
const propsOf = (t) => Object.keys((t.schema && t.schema.properties) || {});
const pick = (names, cands) => cands.find((c) => names.includes(c));
// tools that must never receive a scripted file/shell intent (network, orchestration, scheduling)
const NOT_FOR_INTENTS = /web|fetch|url|browser|agent|workflow|cron|skill|notebook|worktree|task|message|wakeup|finding|todo|mcp|plan/i;
function findTool(tools, intent) {
	for (const re of ALIASES[intent] || []) {
		const t = tools.find((x) => re.test(x.name) && !NOT_FOR_INTENTS.test(x.name) && (intent === "shell" || !/shell|bash|command/i.test(x.name)));
		if (t) return t;
	}
	return null;
}
// a request is part of the scripted agent loop only if it offers at least one core tool
const isCoreToolset = (tools) => ["shell", "read", "write", "edit"].some((k) => findTool(tools, k));
function fillRequired(t, args) {
	const req = (t.schema && t.schema.required) || [];
	const p = (t.schema && t.schema.properties) || {};
	for (const r of req) {
		if (args[r] !== undefined) continue;
		const ty = p[r] && p[r].type;
		args[r] = r === "description" ? "lab scenario step" : ty === "boolean" ? false : ty === "number" || ty === "integer" ? 0 : ty === "array" ? [] : "";
	}
	return args;
}
const shq = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;
function shellFallback(it) {
	switch (it.kind) {
		case "read": return `head -n 2000 ${shq(abs(it.path))}`;
		case "write": return `cat > ${shq(abs(it.path))} <<'LAB_EOF'\n${it.content}\nLAB_EOF`;
		case "edit": return `python3 - <<'LAB_EOF'\np=${JSON.stringify(abs(it.path))}\ns=open(p).read()\nopen(p,'w').write(s.replace(${JSON.stringify(it.oldText)}, ${JSON.stringify(it.newText)}, 1))\nLAB_EOF`;
		case "grep": return `rg -n --max-count 100 ${shq(it.pattern)} ${shq(abs(it.path))} | head -n 200`;
		case "find": return `find ${shq(abs(it.path))} -name ${shq(it.pattern)} | head -n 1000`;
		case "ls": return `ls -la ${shq(abs(it.path))}`;
		default: return "true";
	}
}
// -> { name, kind, args (object, or string for custom tools), mapped, fallback }
function resolve(it, tools) {
	const t = findTool(tools, it.kind);
	if (!t) {
		if (it.kind === "shell") return null;
		const sh = resolve({ kind: "shell", command: shellFallback(it), timeout: it.timeout }, tools);
		return sh ? { ...sh, mapped: it.kind, fallback: true } : null;
	}
	const P = propsOf(t);
	const sp = (t.schema && t.schema.properties) || {};
	const args = {};
	const pathKey = () => pick(P, ["path", "file_path", "filePath", "absolute_path", "filename", "file", "target_file"]) || "path";
	const dirKey = () => pick(P, ["path", "dir_path", "directory", "dir", "cwd", "target_directory", "absolute_path"]);
	switch (it.kind) {
		case "shell": {
			if (t.kind === "local_shell") return { name: "local_shell", kind: "local_shell", args: { command: ["bash", "-lc", it.command], working_directory: WS, timeout_ms: it.timeout ? it.timeout * 1000 : undefined }, mapped: "shell" };
			const c = pick(P, ["command", "cmd", "script", "commands", "input"]) || "command";
			args[c] = sp[c]?.type === "array" ? ["bash", "-lc", it.command] : it.command;
			const wd = pick(P, ["workdir", "cwd", "working_directory", "directory", "dir_path"]);
			if (wd) args[wd] = WS;
			if (it.timeout) {
				const to = pick(P, ["timeout", "timeout_ms", "timeoutMs", "timeout_seconds"]);
				if (to) args[to] = /ms/i.test(to) || /millisecond/i.test(sp[to]?.description || "") ? it.timeout * 1000 : it.timeout;
			}
			break;
		}
		case "read": args[pathKey()] = abs(it.path); break;
		case "write":
			args[pathKey()] = abs(it.path);
			args[pick(P, ["content", "contents", "text", "file_text", "data"]) || "content"] = it.content;
			break;
		case "edit": {
			if (t.kind === "custom" || /patch/i.test(t.name)) {
				const patch = `*** Begin Patch\n*** Update File: ${abs(it.path)}\n@@\n-${it.oldText}\n+${it.newText}\n*** End Patch\n`;
				if (t.kind === "custom") return { name: t.name, kind: "custom", args: patch, mapped: "edit" };
				args[pick(P, ["input", "patch", "diff"]) || "input"] = patch;
				break;
			}
			args[pathKey()] = abs(it.path);
			if (P.includes("edits")) {
				const ip = Object.keys(sp.edits?.items?.properties || {});
				const e = {};
				e[pick(ip, ["oldText", "old_string", "oldString", "old_str", "search"]) || "oldText"] = it.oldText;
				e[pick(ip, ["newText", "new_string", "newString", "new_str", "replace"]) || "newText"] = it.newText;
				args.edits = [e];
			} else {
				if (P.includes("command") && /str_replace/i.test(t.name)) args.command = "str_replace";
				args[pick(P, ["old_string", "oldString", "old_str", "oldText", "search", "old"]) || "old_string"] = it.oldText;
				args[pick(P, ["new_string", "newString", "new_str", "newText", "replace", "new"]) || "new_string"] = it.newText;
			}
			break;
		}
		case "grep": {
			args[pick(P, ["pattern", "query", "regex", "search", "q"]) || "pattern"] = it.pattern;
			const k = dirKey();
			if (k) args[k] = abs(it.path);
			break;
		}
		case "find": {
			args[pick(P, ["pattern", "glob", "query", "name"]) || "pattern"] = it.pattern;
			const k = dirKey();
			if (k) args[k] = abs(it.path);
			break;
		}
		case "ls": args[dirKey() || "path"] = abs(it.path); break;
	}
	return { name: t.name, kind: t.kind, args: fillRequired(t, args), mapped: it.kind };
}

// ---------------------------------------------------------------- scenarios -> intents
function stepIntent(p, i) {
	const kinds = { bash: "shell", shell: "shell", read: "read", ls: "ls", grep: "grep", find: "find", write: "write", edit: "edit" };
	const tool = p.tool || "bash";
	const k = tool === "mixed" ? ["shell", "read", "ls", "grep", "write", "find"][i % 6] : kinds[tool] || tool;
	switch (k) {
		case "shell": return { kind: "shell", command: (p.cmd || "echo turn-$I").replaceAll("$I", String(i)), timeout: p.timeout ? Number(p.timeout) : undefined };
		case "read": return { kind: "read", path: p.file || (tool === "mixed" ? "/etc/os-release" : p.path) || "/etc/os-release" };
		case "ls": return { kind: "ls", path: p.path || "." };
		case "grep": return { kind: "grep", pattern: p.pattern || "root", path: p.path || "/etc" };
		case "find": return { kind: "find", pattern: p.glob || "*.ts", path: p.path || "/etc" };
		case "write": return { kind: "write", path: `w-${i % 20}.txt`, content: filler(int(p.bytes, 2000)) };
		default: return { kind: k };
	}
}
// plan = { intents: [...] } | { raw: [...] } | { text, reasoning, tps }
function decide(scn, step) {
	const p = scn.params;
	switch (scn.name) {
		case "echo":
		case "chat": return { text: filler(int(p.chars, 200)), reasoning: p.think ? filler(int(p.think, 0)) : "" };
		case "stream": return { text: filler(int(p.chars, 40000)), reasoning: p.think ? filler(int(p.think, 0)) : "", tps: int(p.tps, DEFAULT_TPS) };
		case "each-tool": {
			const seq = [
				{ kind: "ls", path: "." },
				{ kind: "write", path: "lab-note.txt", content: "hello from the mock\nline two\n" },
				{ kind: "read", path: "lab-note.txt" },
				{ kind: "edit", path: "lab-note.txt", oldText: "line two", newText: "line 2 edited" },
				{ kind: "grep", pattern: "edited", path: "." },
				{ kind: "find", pattern: "*.txt", path: "." },
				{ kind: "shell", command: "echo hi && uname -a" },
			];
			return step < seq.length ? { intents: [seq[step]] } : { text: "All tools exercised." };
		}
		case "single": return step === 0 ? { intents: [stepIntent(p, 0)] } : { text: "done" };
		case "loop": {
			const n = int(p.n, 50);
			return step < n ? { intents: [stepIntent(p, step)], text: p.say ? filler(int(p.say, 0)) : "" } : { text: `Finished ${n} turns.` };
		}
		case "parallel": {
			const k = int(p.k, 8), rounds = int(p.rounds, 1);
			return step < rounds ? { intents: Array.from({ length: k }, (_, i) => stepIntent(p, step * k + i)) } : { text: `Ran ${k}x${rounds} parallel calls.` };
		}
		case "script": {
			const cmds = (p.cmds || "echo hi").split(";;");
			return step < cmds.length ? { intents: [{ kind: "shell", command: cmds[step], timeout: p.timeout ? Number(p.timeout) : undefined }] } : { text: "script complete" };
		}
		case "subagent": {
			const k = int(p.k, 3);
			const child = p.child || "loop n=5 tool=bash";
			return step === 0 ? { raw: [{ name: p.toolname || "subagent", args: { tasks: Array.from({ length: k }, (_, i) => ({ agent: p.agent || "labworker", task: `SCENARIO: ${child.replaceAll("$I", String(i))}` })) } }] } : { text: "subagents finished" };
		}
		default: return { text: `unknown scenario ${scn.name}` };
	}
}

// ---------------------------------------------------------------- sessions
const sessions = new Map(); // sid -> { scn, steps, failed }
function sessionId(req, body, norm) {
	const h = req.headers;
	const s = h["x-session-affinity"] || h["x-session-id"] || h["session_id"] || h["session-id"] || h["conversation_id"] || h["x-claude-code-session-id"] || body.prompt_cache_key || body.metadata?.user_id;
	if (s) return String(s);
	const first = norm.msgs.find((m) => m.role === "user");
	return first ? "h" + crypto.createHash("sha1").update(first.text).digest("hex").slice(0, 12) : null;
}

// ---------------------------------------------------------------- writers per protocol
let callSeq = 0;
function materialize(plan, tools) {
	const calls = [];
	const notes = [];
	for (const it of plan.intents || []) {
		const r = resolve(it, tools);
		if (r) calls.push({ id: `call_${++callSeq}_${crypto.randomBytes(3).toString("hex")}`, ...r });
		else notes.push(`no tool for ${it.kind}`);
	}
	for (const r of plan.raw || []) {
		if (tools.find((t) => t.name === r.name)) calls.push({ id: `call_${++callSeq}_${crypto.randomBytes(3).toString("hex")}`, name: r.name, kind: "function", args: r.args, mapped: "raw" });
		else notes.push(`no tool named ${r.name}`);
	}
	let text = plan.text || "";
	if (!calls.length && (plan.intents || plan.raw)) text = text || `(mock: ${notes.join("; ")})`;
	return { text, reasoning: plan.reasoning || "", calls, tps: plan.tps, notes };
}
function pacer(tps) {
	let n = 0;
	const d = tps > 0 ? 1000 / tps : 0;
	return async () => {
		n++;
		if (d > 0) await sleep(d);
		else if (n % 64 === 0) await new Promise((r) => setImmediate(r));
	};
}
const SSE = { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" };

async function writeOpenAIChat(res, model, out, usage, stream) {
	if (!stream) {
		const message = { role: "assistant", content: out.text || null };
		if (out.calls.length) message.tool_calls = out.calls.map((c) => ({ id: c.id, type: "function", function: { name: c.name, arguments: JSON.stringify(c.args) } }));
		res.writeHead(200, { "content-type": "application/json" });
		res.end(JSON.stringify({ id: rid("chatcmpl-"), object: "chat.completion", created: Math.floor(Date.now() / 1000), model, choices: [{ index: 0, message, finish_reason: out.calls.length ? "tool_calls" : "stop" }], usage: { prompt_tokens: usage.in, completion_tokens: usage.out, total_tokens: usage.in + usage.out } }));
		return 0;
	}
	res.writeHead(200, SSE);
	const id = rid("chatcmpl-"), created = Math.floor(Date.now() / 1000);
	const send = (o) => res.write(`data: ${JSON.stringify(o)}\n\n`);
	const ch = (delta, finish = null) => ({ id, object: "chat.completion.chunk", created, model, choices: [{ index: 0, delta, finish_reason: finish }] });
	const pace = pacer(out.tps ?? DEFAULT_TPS);
	let chunks = 0;
	send(ch({ role: "assistant", content: "" }));
	for (let i = 0; i < out.reasoning.length; i += CHUNK_CHARS) { send(ch({ reasoning_content: out.reasoning.slice(i, i + CHUNK_CHARS) })); chunks++; await pace(); }
	for (let i = 0; i < out.text.length; i += CHUNK_CHARS) { send(ch({ content: out.text.slice(i, i + CHUNK_CHARS) })); chunks++; await pace(); }
	out.calls.forEach((c, idx) => {
		const a = JSON.stringify(c.args);
		send(ch({ tool_calls: [{ index: idx, id: c.id, type: "function", function: { name: c.name, arguments: "" } }] }));
		for (let i = 0; i < a.length; i += 16) { send(ch({ tool_calls: [{ index: idx, function: { arguments: a.slice(i, i + 16) } }] })); chunks++; }
	});
	send(ch({}, out.calls.length ? "tool_calls" : "stop"));
	send({ id, object: "chat.completion.chunk", created, model, choices: [], usage: { prompt_tokens: usage.in, completion_tokens: usage.out, total_tokens: usage.in + usage.out } });
	res.write("data: [DONE]\n\n");
	res.end();
	return chunks;
}

async function writeAnthropic(res, model, out, usage, stream) {
	const blocks = [];
	if (out.reasoning) blocks.push({ type: "thinking", thinking: out.reasoning, signature: "mock-signature" });
	if (out.text) blocks.push({ type: "text", text: out.text });
	for (const c of out.calls) blocks.push({ type: "tool_use", id: c.id.replace(/^call_/, "toolu_"), name: c.name, input: c.args });
	const stop = out.calls.length ? "tool_use" : "end_turn";
	const msgId = rid("msg_");
	const u = { input_tokens: usage.in, output_tokens: usage.out, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 };
	if (!stream) {
		res.writeHead(200, { "content-type": "application/json" });
		res.end(JSON.stringify({ id: msgId, type: "message", role: "assistant", model, content: blocks, stop_reason: stop, stop_sequence: null, usage: u }));
		return 0;
	}
	res.writeHead(200, SSE);
	const ev = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
	const pace = pacer(out.tps ?? DEFAULT_TPS);
	let chunks = 0;
	ev("message_start", { message: { id: msgId, type: "message", role: "assistant", model, content: [], stop_reason: null, stop_sequence: null, usage: { ...u, output_tokens: 1 } } });
	for (let idx = 0; idx < blocks.length; idx++) {
		const b = blocks[idx];
		if (b.type === "thinking") {
			ev("content_block_start", { index: idx, content_block: { type: "thinking", thinking: "" } });
			for (let i = 0; i < b.thinking.length; i += CHUNK_CHARS) { ev("content_block_delta", { index: idx, delta: { type: "thinking_delta", thinking: b.thinking.slice(i, i + CHUNK_CHARS) } }); chunks++; await pace(); }
			ev("content_block_delta", { index: idx, delta: { type: "signature_delta", signature: b.signature } });
		} else if (b.type === "text") {
			ev("content_block_start", { index: idx, content_block: { type: "text", text: "" } });
			for (let i = 0; i < b.text.length; i += CHUNK_CHARS) { ev("content_block_delta", { index: idx, delta: { type: "text_delta", text: b.text.slice(i, i + CHUNK_CHARS) } }); chunks++; await pace(); }
		} else {
			ev("content_block_start", { index: idx, content_block: { type: "tool_use", id: b.id, name: b.name, input: {} } });
			const a = JSON.stringify(b.input);
			for (let i = 0; i < a.length; i += 16) { ev("content_block_delta", { index: idx, delta: { type: "input_json_delta", partial_json: a.slice(i, i + 16) } }); chunks++; }
		}
		ev("content_block_stop", { index: idx });
	}
	ev("message_delta", { delta: { stop_reason: stop, stop_sequence: null }, usage: { output_tokens: usage.out } });
	ev("message_stop", {});
	res.end();
	return chunks;
}

async function writeResponses(res, model, out, usage, stream) {
	const items = [];
	if (out.text) items.push({ type: "message", id: rid("msg_"), role: "assistant", status: "completed", content: [{ type: "output_text", text: out.text, annotations: [] }] });
	for (const c of out.calls) {
		if (c.kind === "local_shell") items.push({ type: "local_shell_call", id: rid("lsh_"), call_id: c.id, status: "completed", action: { type: "exec", ...c.args } });
		else if (c.kind === "custom") items.push({ type: "custom_tool_call", id: rid("ctc_"), call_id: c.id, name: c.name, input: c.args, status: "completed" });
		else items.push({ type: "function_call", id: rid("fc_"), call_id: c.id, name: c.name, arguments: JSON.stringify(c.args), status: "completed" });
	}
	const usageObj = { input_tokens: usage.in, input_tokens_details: { cached_tokens: 0 }, output_tokens: usage.out, output_tokens_details: { reasoning_tokens: 0 }, total_tokens: usage.in + usage.out };
	const final = { id: rid("resp_"), object: "response", created_at: Math.floor(Date.now() / 1000), status: "completed", model, output: items, usage: usageObj };
	if (!stream) {
		res.writeHead(200, { "content-type": "application/json" });
		res.end(JSON.stringify(final));
		return 0;
	}
	res.writeHead(200, SSE);
	let seq = 0;
	const ev = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, sequence_number: seq++, ...data })}\n\n`);
	const pace = pacer(out.tps ?? DEFAULT_TPS);
	let chunks = 0;
	ev("response.created", { response: { ...final, status: "in_progress", output: [], usage: null } });
	ev("response.in_progress", { response: { ...final, status: "in_progress", output: [], usage: null } });
	for (let oi = 0; oi < items.length; oi++) {
		const it = items[oi];
		if (it.type === "message") {
			const txt = it.content[0].text;
			ev("response.output_item.added", { output_index: oi, item: { ...it, status: "in_progress", content: [] } });
			ev("response.content_part.added", { item_id: it.id, output_index: oi, content_index: 0, part: { type: "output_text", text: "", annotations: [] } });
			for (let i = 0; i < txt.length; i += CHUNK_CHARS) { ev("response.output_text.delta", { item_id: it.id, output_index: oi, content_index: 0, delta: txt.slice(i, i + CHUNK_CHARS) }); chunks++; await pace(); }
			ev("response.output_text.done", { item_id: it.id, output_index: oi, content_index: 0, text: txt });
			ev("response.content_part.done", { item_id: it.id, output_index: oi, content_index: 0, part: it.content[0] });
			ev("response.output_item.done", { output_index: oi, item: it });
		} else if (it.type === "function_call") {
			ev("response.output_item.added", { output_index: oi, item: { ...it, arguments: "", status: "in_progress" } });
			for (let i = 0; i < it.arguments.length; i += 16) { ev("response.function_call_arguments.delta", { item_id: it.id, output_index: oi, delta: it.arguments.slice(i, i + 16) }); chunks++; }
			ev("response.function_call_arguments.done", { item_id: it.id, output_index: oi, arguments: it.arguments });
			ev("response.output_item.done", { output_index: oi, item: it });
		} else {
			ev("response.output_item.added", { output_index: oi, item: it });
			ev("response.output_item.done", { output_index: oi, item: it });
		}
	}
	ev("response.completed", { response: final });
	res.end();
	return chunks;
}

// ---------------------------------------------------------------- HTTP
const MODELS = ["mock-1", "mock-small", "mock-cache"];
function errorBody(proto, status) {
	if (proto === "anthropic") return { type: "error", error: { type: status === 429 ? "rate_limit_error" : status === 529 ? "overloaded_error" : "api_error", message: `injected ${status}` } };
	return { error: { message: `injected ${status}`, type: "lab_injected", code: status } };
}

const server = http.createServer(async (req, res) => {
	const tReq = now();
	const url = new URL(req.url, "http://x");
	if (req.method === "GET" && url.pathname.endsWith("/models")) {
		res.writeHead(200, { "content-type": "application/json" });
		res.end(JSON.stringify({ object: "list", data: MODELS.map((id) => ({ id, object: "model", type: "model", display_name: id, owned_by: "lab", created_at: "2026-01-01T00:00:00Z" })) }));
		return;
	}
	if (req.method === "GET" && url.pathname === "/health") return res.end("ok");
	if (req.method === "POST" && url.pathname.startsWith("/control/log")) {
		try { if (logFd !== null) fs.closeSync(logFd); } catch {}
		logFd = null;
		const p = url.searchParams.get("path");
		if (p) openLog(p);
		sessions.clear();
		return res.end(JSON.stringify({ ok: true, path: p }));
	}
	const bufs = [];
	for await (const b of req) bufs.push(b);
	const raw = Buffer.concat(bufs);
	let body = {};
	try { body = raw.length ? JSON.parse(raw.toString("utf8")) : {}; } catch { res.writeHead(400); return res.end("bad json"); }

	if (url.pathname.endsWith("/messages/count_tokens")) {
		res.writeHead(200, { "content-type": "application/json" });
		res.end(JSON.stringify({ input_tokens: Math.ceil(raw.length / 4) }));
		log({ t: tReq, tEnd: now(), proto: "anthropic", path: url.pathname, aux: true, kind: "count_tokens", reqBytes: raw.length, ua: req.headers["user-agent"] });
		return;
	}
	const proto = url.pathname.endsWith("/chat/completions") ? "openai-chat" : url.pathname.endsWith("/messages") ? "anthropic" : url.pathname.endsWith("/responses") ? "openai-responses" : null;
	if (req.method !== "POST" || !proto) {
		log({ t: tReq, unknown: true, method: req.method, path: url.pathname, reqBytes: raw.length, ua: req.headers["user-agent"] });
		res.writeHead(404, { "content-type": "application/json" });
		return res.end(JSON.stringify({ error: { message: "not found in lab mock" } }));
	}
	const norm = normalize(proto, body);
	const lastMsg = norm.msgs.at(-1);
	const freshPrompt = lastMsg && lastMsg.role === "user" && /SCENARIO:/.test(lastMsg.text);
	// Requests without tools are auxiliary (compaction summaries, titles, topic checks, quota probes):
	// answer with text and do not advance the scenario.
	const aux = norm.tools.length === 0 || !isCoreToolset(norm.tools);
	const sid = sessionId(req, body, norm);
	let scn = null, step = 0, st = null;
	if (!aux) {
		st = sid ? sessions.get(sid) : null;
		if (!st || freshPrompt) {
			const fromLast = freshPrompt ? parseScenarioText(lastMsg.text) : null;
			const anyUser = norm.msgs.filter((m) => m.role === "user").map((m) => parseScenarioText(m.text)).find(Boolean);
			st = { scn: fromLast || anyUser || { name: "echo", params: {} }, steps: 0, failed: 0 };
			if (sid) sessions.set(sid, st);
		}
		scn = st.scn;
		step = st.steps++;
	}
	const params = scn ? scn.params : {};
	if (!aux && params.fail && step === int(params.failstep, 1) && st.failed < int(params.fails, 3)) {
		st.failed++;
		st.steps--;
		const status = Number(params.fail);
		const hdrs = { "content-type": "application/json" };
		if (params.retryafter) hdrs["retry-after"] = String(params.retryafter);
		res.writeHead(status, hdrs);
		res.end(JSON.stringify(errorBody(proto, status)));
		log({ t: tReq, tEnd: now(), proto, scenario: scn.name, step, sid: sid && sid.slice(-8), injected: status, attempt: st.failed, reqBytes: raw.length, nMessages: norm.msgs.length, ua: req.headers["user-agent"] });
		return;
	}
	let plan;
	if (aux) {
		const sysAll = norm.system + "\n" + (lastMsg?.text || "");
		if (/isNewTopic/.test(sysAll)) plan = { text: JSON.stringify({ isNewTopic: false, title: null }) };
		else if (/summar|compact/i.test(sysAll)) plan = { text: "## Summary\nThe agent ran a scripted load scenario. " + filler(1500) };
		else if (/title/i.test(sysAll)) plan = { text: "Lab scenario run" };
		else plan = { text: "ok" };
	} else plan = decide(scn, step);
	if (params.latency) await sleep(Number(params.latency));
	const out = materialize(plan, norm.tools);
	const outChars = out.text.length + out.reasoning.length + JSON.stringify(out.calls.map((c) => c.args)).length;
	const usage = { in: Math.ceil(raw.length / 4), out: Math.max(1, Math.ceil(outChars / 4)) };
	const t0 = now();
	let chunks = 0;
	try {
		if (proto === "openai-chat") chunks = await writeOpenAIChat(res, body.model, out, usage, norm.stream);
		else if (proto === "anthropic") chunks = await writeAnthropic(res, body.model, out, usage, norm.stream);
		else chunks = await writeResponses(res, body.model, out, usage, norm.stream);
	} catch (e) {
		console.error("write failed", e.message);
	}
	log({
		t: tReq, tEnd: now(), proto, path: url.pathname, model: body.model, aux, summary: aux && /^## Summary/.test(out.text),
		scenario: scn?.name, params: scn ? params : undefined, step, sid: sid ? sid.slice(-8) : undefined, stream: norm.stream,
		reqBytes: raw.length, nMessages: norm.msgs.length, nTools: norm.tools.length, toolNames: norm.tools.map((t) => t.name),
		toolsBytes: JSON.stringify(body.tools || []).length, sysBytes: norm.system.length,
		toolResultBytes: norm.msgs.filter((m) => m.role === "tool").reduce((a, m) => a + m.text.length, 0),
		maxTokens: norm.maxTokens,
		reply: out.calls.length ? { toolCalls: out.calls.map((c) => c.name), mapped: out.calls.map((c) => c.mapped + (c.fallback ? "→shell" : "")) } : { textChars: out.text.length },
		notes: out.notes.length ? out.notes : undefined,
		ua: req.headers["user-agent"], headers: Object.keys(req.headers),
		chunks, ttfbMs: t0 - tReq, streamMs: now() - t0, outChars,
	});
});
server.keepAliveTimeout = 65000;
server.listen(PORT, "0.0.0.0", () => console.log(`mockllm (multi-protocol) listening on ${PORT}`));
