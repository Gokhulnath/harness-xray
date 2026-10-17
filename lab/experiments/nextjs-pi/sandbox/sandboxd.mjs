// Stand-in for an E2B sandbox: a tiny HTTP daemon (like E2B's envd) that runs shell commands and
// file operations for a harness running somewhere else. The harness never spawns anything itself.
//
//   POST /exec   {command, cwd, timeoutMs}  -> NDJSON stream: {"o":"<base64 chunk>"} ... {"exit":N,"timedOut":bool}
//   POST /read   {path}                     -> raw bytes (404 {"code":"ENOENT"} if missing)
//   POST /write  {path, content}            -> 204 (creates parent dirs)
//   POST /access {path, mode:"r"|"rw"}      -> 204 or 404/403
//   POST /mkdir  {path}                     -> 204
//   GET  /stats                             -> counters + this process's CPU/RSS
//
// Every request must carry X-Access-Token (like envd's token). Commands get a minimal env:
// nothing from the harness side ever reaches them.
import { spawn } from "node:child_process";
import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { createServer } from "node:http";
import { dirname } from "node:path";

const PORT = Number(process.env.PORT || 49983);
const TOKEN = process.env.SANDBOX_TOKEN || "";
const CMD_ENV = { PATH: "/usr/local/bin:/usr/bin:/bin", HOME: "/home/node", LANG: "C.UTF-8" };
const stats = { exec: 0, read: 0, write: 0, access: 0, mkdir: 0, execMs: 0 };

const json = (res, code, obj) => {
	res.writeHead(code, { "content-type": "application/json" });
	res.end(JSON.stringify(obj));
};

async function body(req) {
	const chunks = [];
	for await (const c of req) chunks.push(c);
	return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
}

function runExec(req, res, { command, cwd, timeoutMs }) {
	stats.exec++;
	const t0 = Date.now();
	res.writeHead(200, { "content-type": "application/x-ndjson" });
	const child = spawn("/bin/bash", ["-c", command], { cwd: cwd || "/workspace", env: CMD_ENV, detached: true, stdio: ["ignore", "pipe", "pipe"] });
	let timedOut = false;
	const killTree = () => {
		try {
			process.kill(-child.pid, "SIGKILL");
		} catch {}
	};
	const timer = timeoutMs ? setTimeout(() => ((timedOut = true), killTree()), timeoutMs) : undefined;
	// harness hung up (abort) -> kill the command
	res.on("close", () => {
		if (child.exitCode === null && child.signalCode === null) killTree();
	});
	const send = (d) => res.write(JSON.stringify({ o: d.toString("base64") }) + "\n");
	child.stdout.on("data", send);
	child.stderr.on("data", send);
	child.on("error", (e) => send(Buffer.from(String(e))));
	child.on("close", (code, signal) => {
		if (timer) clearTimeout(timer);
		stats.execMs += Date.now() - t0;
		res.end(JSON.stringify({ exit: code ?? 128 + (signal ? 9 : 0), timedOut }) + "\n");
	});
}

const server = createServer(async (req, res) => {
	try {
		if (req.method === "GET" && req.url === "/stats") {
			const m = process.memoryUsage();
			return json(res, 200, { ...stats, rssMiB: +(m.rss / 1048576).toFixed(1), cpuMs: +(process.cpuUsage().user / 1000 + process.cpuUsage().system / 1000).toFixed(1) });
		}
		if (req.headers["x-access-token"] !== TOKEN) return json(res, 401, { error: "bad token" });
		const b = await body(req);
		switch (req.url) {
			case "/exec":
				return runExec(req, res, b);
			case "/read": {
				stats.read++;
				try {
					const data = await readFile(b.path);
					res.writeHead(200, { "content-type": "application/octet-stream" });
					return res.end(data);
				} catch (e) {
					return json(res, e.code === "ENOENT" ? 404 : 500, { code: e.code, message: e.message });
				}
			}
			case "/write":
				stats.write++;
				await mkdir(dirname(b.path), { recursive: true });
				await writeFile(b.path, b.content, "utf8");
				res.writeHead(204);
				return res.end();
			case "/access":
				stats.access++;
				try {
					await access(b.path, b.mode === "rw" ? constants.R_OK | constants.W_OK : constants.R_OK);
					res.writeHead(204);
					return res.end();
				} catch (e) {
					return json(res, e.code === "ENOENT" ? 404 : 403, { code: e.code, message: e.message });
				}
			case "/mkdir":
				stats.mkdir++;
				await mkdir(b.path, { recursive: true });
				res.writeHead(204);
				return res.end();
			default:
				return json(res, 404, { error: "no route" });
		}
	} catch (e) {
		if (!res.headersSent) json(res, 500, { error: String(e) });
		else res.end();
	}
});
server.listen(PORT, "0.0.0.0", () => console.log(`sandboxd listening on :${PORT}`));
