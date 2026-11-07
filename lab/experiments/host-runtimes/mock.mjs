// OpenAI-compatible streaming mock. Turn shape: user message -> one `read` tool call; tool result -> a text answer
// of TOKENS chunks. Runs as its own process so the agent process's CPU is only the agent's.
import http from "node:http";
const PORT = Number(process.env.PORT || 18080), TOKENS = Number(process.env.TOKENS || 300);
let n = 0;
const seen = { calls: 0, toolResults: 0, lastToolText: "" };
http.createServer((req, res) => {
	if (req.method === "GET") return res.end(JSON.stringify(seen));
	let body = "";
	req.on("data", (c) => (body += c));
	req.on("end", async () => {
		const msgs = JSON.parse(body || "{}").messages || [];
		const last = msgs[msgs.length - 1] || {};
		seen.calls++;
		if (process.env.DUMP && seen.calls <= 2) (await import("node:fs")).writeFileSync(`${process.env.DUMP}-${seen.calls}.json`, body);
		if (last.role === "tool") { seen.toolResults++; seen.lastToolText = String(last.content).slice(0, 80); }
		res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
		const chunk = (delta, finish = null) => res.write(`data: ${JSON.stringify({ id: "m" + n, object: "chat.completion.chunk", created: 0, model: "mock", choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`);
		n++;
		if (last.role !== "tool") {
			chunk({ role: "assistant", content: "" });
			chunk({ tool_calls: [{ index: 0, id: "call_" + n, type: "function", function: { name: "read", arguments: "" } }] });
			for (const piece of ['{"pa', 'th":"he', 'llo.txt"}']) chunk({ tool_calls: [{ index: 0, function: { arguments: piece } }] });
			chunk({}, "tool_calls");
		} else {
			chunk({ role: "assistant", content: "" });
			for (let i = 0; i < TOKENS; i++) chunk({ content: "tok" + (i % 10) + " " });
			chunk({}, "stop");
		}
		res.write(`data: ${JSON.stringify({ id: "m" + n, object: "chat.completion.chunk", created: 0, model: "mock", choices: [], usage: { prompt_tokens: 1200, completion_tokens: TOKENS, total_tokens: 1200 + TOKENS } })}\n\n`);
		res.end("data: [DONE]\n\n");
	});
}).listen(PORT, "127.0.0.1", () => console.log("mock on", PORT));
