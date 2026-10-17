// Variant C: pi (Pi Durable, via @rivet-dev/pi) inside a Rivet Actor on Rivet Compute. Tools run in E2B.
// Vercel only serves the Next.js app and proxies to this actor; pi's loop, model calls and session state live here.
import { readFileSync } from "node:fs";
import os from "node:os";
import { createRegistry, defineExtension, section } from "@earendil-works/pi-durable";
import { CodingTools } from "@earendil-works/pi-durable/tools";
import { pi } from "@rivet-dev/pi";
import { e2bProvider } from "@rivet-dev/sandbox-adapter/e2b";
import { Sandbox } from "e2b";
import { setup } from "rivetkit";
import { SCENARIOS, scenarioOf } from "./workspace.mjs";

const MODEL = `openrouter/${process.env.PI_MODEL || "nvidia/nemotron-3-super-120b-a12b:free"}`;
const CWD = "/home/user/workspace";
const instance = { id: Math.random().toString(36).slice(2, 10), bootedAt: Date.now(), bootCpuMs: cpuMs() };
const sandboxEvents = []; // create/connect/suspend/destroy timings, newest last (kept small)

function cpuMs() {
	const u = process.cpuUsage();
	return (u.user + u.system) / 1000;
}
const read = (p) => {
	try {
		return readFileSync(p, "utf8");
	} catch {
		return null;
	}
};
function stats() {
	const st = read("/proc/self/status") || "";
	const kb = (k) => {
		const m = st.match(new RegExp(`^${k}:\\s+(\\d+) kB`, "m"));
		return m ? +(+m[1] / 1024).toFixed(1) : null;
	};
	const cpuStat = read("/sys/fs/cgroup/cpu.stat");
	const usageUsec = cpuStat ? +(cpuStat.match(/usage_usec (\d+)/)?.[1] ?? NaN) : null;
	const m = process.memoryUsage();
	return {
		instanceId: instance.id,
		instanceAgeS: +((Date.now() - instance.bootedAt) / 1000).toFixed(1),
		bootCpuMs: +instance.bootCpuMs.toFixed(1),
		cpuMs: +cpuMs().toFixed(1),
		rssMiB: kb("VmRSS") ?? +(m.rss / 1048576).toFixed(1),
		peakRssMiB: kb("VmHWM"),
		heapUsedMiB: +(m.heapUsed / 1048576).toFixed(1),
		threads: +(st.match(/^Threads:\s+(\d+)/m)?.[1] ?? 0),
		cgroup: {
			cpuMs: usageUsec ? usageUsec / 1000 : null,
			memMiB: read("/sys/fs/cgroup/memory.current") ? +(+read("/sys/fs/cgroup/memory.current") / 1048576).toFixed(1) : null,
			peakMiB: read("/sys/fs/cgroup/memory.peak") ? +(+read("/sys/fs/cgroup/memory.peak") / 1048576).toFixed(1) : null,
			memMax: read("/sys/fs/cgroup/memory.max")?.trim() ?? null,
			cpuMax: read("/sys/fs/cgroup/cpu.max")?.trim() ?? null,
		},
		host: { cpus: os.cpus().length, cpuModel: os.cpus()[0]?.model, memTotalMiB: Math.round(os.totalmem() / 1048576), kernel: os.release(), loadavg: os.loadavg() },
		region: process.env.RIVET_REGION || process.env.FLY_REGION || null,
		node: process.version,
		sandboxEvents: sandboxEvents.slice(-20),
	};
}

// E2B provider from Rivet's adapter, wrapped to seed the workspace into each new sandbox and time every lifecycle step.
const base = e2bProvider({ template: "base", cwd: CWD, create: { timeoutMs: 10 * 60_000, metadata: { lab: "harness-xray", variant: "c" } } });
const timed = (kind, fn) => async (...args) => {
	const t0 = performance.now();
	try {
		return await fn(...args);
	} finally {
		sandboxEvents.push({ kind, ms: Math.round(performance.now() - t0), at: Date.now() });
		if (sandboxEvents.length > 200) sandboxEvents.shift();
	}
};
const sandbox = {
	...base,
	create: timed("create", async (c) => {
		const id = await base.create(c);
		const sbx = await Sandbox.connect(id);
		// scenario comes from the actor key "<scenario>-<id>" (the Vercel proxy uses the session id as the key)
		const key = Array.isArray(c?.key) ? String(c.key[0]) : String(c?.key ?? "");
		const sc = SCENARIOS[scenarioOf(key.split("-")[0])];
		await sbx.files.write(Object.entries(sc.files).map(([name, data]) => ({ path: `${CWD}/${name}`, data })));
		if (sc.setup) await sbx.commands.run(sc.setup, { cwd: CWD, timeoutMs: 120_000 });
		return id;
	}),
	connect: timed("connect", base.connect),
	suspend: base.suspend && timed("suspend", base.suspend),
	destroy: base.destroy && timed("destroy", base.destroy),
};

// CodingTools installs only the four tools; Pi Durable sends no system prompt unless an extension adds sections. Variants
// A and B run pi-coding-agent, which always sends its coding prompt, so C gets the same text (pi-coding-agent 1.2.0's
// buildSystemPrompt for read/bash/edit/write; the pi-docs section is left out: those files are not in the sandbox).
const CodingPrompt = defineExtension({
	name: "coding-prompt",
	sections: [
		section("preamble", () => "You are an expert coding assistant operating inside pi, a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files.", { tag: false }),
		section("tools", () =>
			[
				"- read: Read file contents",
				"- bash: Execute bash commands (ls, grep, find, etc.)",
				"- edit: Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
				"- write: Create or overwrite files",
				"",
				"In addition to the tools above, you may have access to other custom tools depending on the project.",
			].join("\n"),
		),
		section("rules", () =>
			[
				"Use bash for file operations like ls, rg, find",
				"Use read to examine files instead of cat or sed.",
				"Use edit for precise changes (edits[].oldText must match exactly)",
				"When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls",
				"Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
				"Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.",
				"Use write only for new files or complete rewrites.",
				"Be concise in your responses",
				"Show file paths clearly when working with files",
			]
				.map((r) => "- " + r)
				.join("\n"),
		),
		section("cwd", () => CWD),
	],
});

const extensions = createRegistry();
extensions.install(CodingTools);
if (process.env.PI_PROMPT !== "none") extensions.install(CodingPrompt);

const agent = pi({
	model: MODEL,
	registry: extensions,
	sandbox,
	actions: {
		labStats: () => stats(),
	},
});

export const registry = setup({ use: { agent } });
registry.start();
