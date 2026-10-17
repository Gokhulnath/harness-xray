// pi's tools, rewired to run in a remote sandbox (here sandboxd; E2B would be the same shape).
// pi ships its tools with pluggable "operations" exactly so they can run somewhere else.
// The Next.js process never spawns a child process or touches a workspace file.

const BASE = process.env.SANDBOX_URL || "http://sandbox:49983";
const TOKEN = process.env.SANDBOX_TOKEN || "";

export const sandboxCalls = { exec: 0, read: 0, write: 0, access: 0, mkdir: 0, ms: 0 };

async function call(path, payload, signal) {
	const t0 = performance.now();
	const res = await fetch(BASE + path, {
		method: "POST",
		headers: { "content-type": "application/json", "x-access-token": TOKEN },
		body: JSON.stringify(payload),
		signal,
	});
	sandboxCalls[path.slice(1)]++;
	sandboxCalls.ms += performance.now() - t0;
	return res;
}

async function fail(res, path) {
	let info = {};
	try {
		info = await res.json();
	} catch {}
	const err = new Error(info.message || `sandbox ${res.status}`);
	if (info.code) err.code = info.code;
	if (res.status === 404 && !info.code) err.code = "ENOENT";
	err.path = path;
	return err;
}

const bashOps = {
	async exec(command, cwd, { onData, signal, timeout }) {
		const res = await call("/exec", { command, cwd, timeoutMs: timeout ? timeout * 1000 : undefined }, signal);
		if (!res.ok) throw await fail(res, cwd);
		const reader = res.body.getReader();
		const dec = new TextDecoder();
		let buf = "";
		let result;
		for (;;) {
			const { done, value } = await reader.read();
			if (done) break;
			buf += dec.decode(value, { stream: true });
			let i;
			while ((i = buf.indexOf("\n")) >= 0) {
				const line = buf.slice(0, i);
				buf = buf.slice(i + 1);
				if (!line) continue;
				const msg = JSON.parse(line);
				if (msg.o !== undefined) onData(Buffer.from(msg.o, "base64"));
				else result = msg;
			}
		}
		if (signal?.aborted) throw new Error("aborted");
		if (result?.timedOut) throw new Error(`timeout:${timeout}`);
		return { exitCode: result ? result.exit : null };
	},
};

async function readFile(path) {
	const res = await call("/read", { path });
	if (!res.ok) throw await fail(res, path);
	return Buffer.from(await res.arrayBuffer());
}

async function writeFile(path, content) {
	const res = await call("/write", { path, content });
	if (!res.ok) throw await fail(res, path);
}

const access = (mode) => async (path) => {
	const res = await call("/access", { path, mode });
	if (!res.ok) throw await fail(res, path);
};

async function mkdir(path) {
	const res = await call("/mkdir", { path });
	if (!res.ok) throw await fail(res, path);
}

/** Same names as pi's built-in tools, so they replace the local versions. */
export function remoteTools(pi, cwd) {
	return [
		pi.createBashToolDefinition(cwd, { operations: bashOps }),
		pi.createReadToolDefinition(cwd, { operations: { readFile, access: access("r"), detectImageMimeType: async () => null } }),
		pi.createWriteToolDefinition(cwd, { operations: { writeFile, mkdir } }),
		pi.createEditToolDefinition(cwd, { operations: { readFile, writeFile, access: access("rw") } }),
	];
}
