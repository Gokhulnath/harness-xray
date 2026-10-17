// Variant B: pi's tools with pluggable operations that run in an E2B sandbox (Firecracker microVM).
// This function never spawns a process; the E2B API key stays here and never enters the sandbox.
import { SCENARIOS, scenarioOf } from "./workspace.js";

const CWD = "/home/user/workspace";
const g = globalThis;
g.__e2b ??= new Map(); // sessionId -> { sandbox, stats }

function newStats() {
	return { createMs: null, calls: { exec: 0, read: 0, write: 0, access: 0, mkdir: 0 }, roundTripMs: 0 };
}

async function timed(stats, kind, fn) {
	const t0 = performance.now();
	try {
		return await fn();
	} finally {
		stats.calls[kind]++;
		stats.roundTripMs += performance.now() - t0;
	}
}

function enoent(path) {
	return Object.assign(new Error(`ENOENT: no such file or directory, '${path}'`), { code: "ENOENT" });
}

export async function e2bWorkspace(sessionId, pi, scenario = "cart") {
	let entry = g.__e2b.get(sessionId);
	if (!entry) {
		// dynamic import -> e2b's ESM build; its CJS build require()s ESM-only chalk, which Vercel's runtime rejects
		const { Sandbox } = await import("e2b");
		const stats = newStats();
		const t0 = performance.now();
		const sandbox = await Sandbox.create("base", { timeoutMs: 10 * 60_000, lifecycle: { onTimeout: "pause" }, metadata: { lab: "harness-xray", session: sessionId } });
		stats.createMs = Math.round(performance.now() - t0);
		const sc = SCENARIOS[scenarioOf(scenario)];
		await sandbox.files.write(Object.entries(sc.files).map(([name, data]) => ({ path: `${CWD}/${name}`, data })));
		if (sc.setup) await sandbox.commands.run(sc.setup, { cwd: CWD, timeoutMs: 120_000 });
		stats.seedMs = Math.round(performance.now() - t0) - stats.createMs;
		entry = { sandbox, stats };
		g.__e2b.set(sessionId, entry);
	}
	const { sandbox, stats } = entry;

	const bashOps = {
		async exec(command, cwd, { onData, signal, timeout }) {
			return timed(stats, "exec", async () => {
				const handle = await sandbox.commands.run(command, {
					cwd,
					background: true,
					timeoutMs: timeout ? timeout * 1000 : 0, // 0 = no limit, as in pi's local bash and Rivet's adapter
					onStdout: (d) => onData(Buffer.from(d)),
					onStderr: (d) => onData(Buffer.from(d)),
				});
				const onAbort = () => handle.kill().catch(() => {});
				signal?.addEventListener("abort", onAbort, { once: true });
				try {
					const r = await handle.wait();
					return { exitCode: r.exitCode };
				} catch (e) {
					if (signal?.aborted) throw new Error("aborted");
					if (typeof e?.exitCode === "number") return { exitCode: e.exitCode }; // CommandExitError
					if (/timeout|deadline/i.test(String(e?.message))) throw new Error(`timeout:${timeout}`);
					throw e;
				} finally {
					signal?.removeEventListener("abort", onAbort);
				}
			});
		},
	};
	const readFile = (path) =>
		timed(stats, "read", async () => {
			if (!(await sandbox.files.exists(path))) throw enoent(path);
			return Buffer.from(await sandbox.files.read(path, { format: "bytes" }));
		});
	const writeFile = (path, content) => timed(stats, "write", () => sandbox.files.write(path, content));
	const access = (path) =>
		timed(stats, "access", async () => {
			if (!(await sandbox.files.exists(path))) throw enoent(path);
		});
	const mkdir = (path) => timed(stats, "mkdir", () => sandbox.files.makeDir(path));

	const customTools = [
		pi.createBashToolDefinition(CWD, { operations: bashOps }),
		pi.createReadToolDefinition(CWD, { operations: { readFile, access, detectImageMimeType: async () => null } }),
		pi.createWriteToolDefinition(CWD, { operations: { writeFile, mkdir } }),
		pi.createEditToolDefinition(CWD, { operations: { readFile, writeFile, access } }),
	];
	return { cwd: CWD, customTools, stats, sandbox };
}

export async function e2bMetrics(sessionId) {
	const entry = g.__e2b.get(sessionId);
	if (!entry) return null;
	try {
		return await entry.sandbox.getMetrics();
	} catch (e) {
		return { error: String(e?.message || e) };
	}
}

export async function e2bKill(sessionId) {
	const entry = g.__e2b.get(sessionId);
	if (!entry) return false;
	g.__e2b.delete(sessionId);
	await entry.sandbox.kill().catch(() => {});
	return true;
}
