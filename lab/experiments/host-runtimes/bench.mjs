// Same agent workload under Node or Bun: pi-durable Harness per session (like one Rivet actor per session), SQLite
// file per session, CodingTools with a local env, model = the streaming mock. Prints one JSON line of measurements.
//   node bench.mjs <sessions> <turns>     |     bun bench.mjs <sessions> <turns>
const T0 = performance.now(), C0 = process.cpuUsage();
const isBun = typeof Bun !== "undefined";
const { BACKGROUND_CONTEXT: ctx } = await import("@earendil-works/chord/context");
const { createModels, createProvider, envApiKeyAuth } = await import("@earendil-works/pi-ai");
const { openAICompletionsApi } = await import("@earendil-works/pi-ai/api/openai-completions.lazy");
const { createRegistry, Harness } = await import("@earendil-works/pi-durable");
const { CodingTools } = await import("@earendil-works/pi-durable/tools");
const { NodeExecutionEnv } = await import("@earendil-works/pi-durable/env/node");
const openStorage = isBun ? (await import("./bun-sqlite.mjs")).openBunSqliteStorage : (await import("@earendil-works/pi-durable/storage/sqlite/node")).openNodeSqliteStorage;
const { mkdirSync, writeFileSync, rmSync } = await import("node:fs");

const cpu = () => { const u = process.cpuUsage(C0); return (u.user + u.system) / 1000; };
const rss = () => process.memoryUsage().rss / 1048576;
const importMs = performance.now() - T0, importCpu = cpu(), importRss = rss();

const SESSIONS = Number(process.argv[2] || 1), TURNS = Number(process.argv[3] || 3);
const DIR = `${process.env.TMPDIR || "/tmp"}/bunbench-${isBun ? "bun" : "node"}-${process.pid}`;
mkdirSync(DIR, { recursive: true });
writeFileSync(`${DIR}/hello.txt`, "hello from the workspace\n");
process.env.MOCK_API_KEY = "x";

const models = createModels();
models.setProvider(createProvider({
	id: "mock", baseUrl: `http://127.0.0.1:${process.env.PORT || 18080}/v1`,
	auth: { apiKey: envApiKeyAuth("mock", ["MOCK_API_KEY"]) },
	models: [{ type: "chat", id: "mock", name: "mock", api: "openai-completions", baseUrl: `http://127.0.0.1:${process.env.PORT || 18080}/v1`, provider: "mock", reasoning: false, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 200000, maxTokens: 8192, compat: { supportsDeveloperRole: false } }],
	api: { "openai-completions": openAICompletionsApi() },
}));
const registry = createRegistry();
registry.install(CodingTools);

// open one harness (session) per "actor"
const tOpen = performance.now(), cOpen = cpu();
const sessions = [];
for (let i = 0; i < SESSIONS; i++) {
	const storage = await openStorage(`${DIR}/s${i}.sqlite`);
	const harness = await Harness.open(storage, { models, registry, env: () => new NodeExecutionEnv({ cwd: DIR }) }, ctx);
	const root = await harness.root(ctx, { agent: { model: { provider: "mock", modelId: "mock" }, cwd: DIR } });
	sessions.push({ harness, root });
}
const openMs = performance.now() - tOpen, openCpu = cpu() - cOpen, rssOpen = rss();

// run TURNS turns in every session at the same time
let peak = rss();
const sampler = setInterval(() => (peak = Math.max(peak, rss())), 20);
const tRun = performance.now(), cRun = cpu();
let failed = 0;
await Promise.all(sessions.map(async ({ root }, i) => {
	for (let t = 0; t < TURNS; t++) {
		const sub = await root.submit({ type: "input", content: `turn ${t}: read hello.txt and summarise it`, requestId: `s${i}-t${t}` }, ctx);
		const settled = await sub.wait(ctx);
		if (settled.status !== "done") { failed++; if (failed === 1) console.error(JSON.stringify(settled).slice(0, 400)); }
	}
}));
const runMs = performance.now() - tRun, runCpu = cpu() - cRun;
clearInterval(sampler);
globalThis.gc?.(); if (isBun) Bun.gc(true);
const rssAfter = rss();
for (const s of sessions) await s.harness.close(ctx);
rmSync(DIR, { recursive: true, force: true });
const turns = SESSIONS * TURNS;
console.log(JSON.stringify({
	runtime: isBun ? `bun ${Bun.version}` : `node ${process.versions.node}`, sessions: SESSIONS, turns, failed,
	importMs: +importMs.toFixed(0), importCpuMs: +importCpu.toFixed(0), importRssMiB: +importRss.toFixed(1),
	openCpuMsPerSession: +(openCpu / SESSIONS).toFixed(2), rssPerOpenSessionMiB: +((rssOpen - importRss) / SESSIONS).toFixed(3),
	runWallMs: +runMs.toFixed(0), cpuMsPerTurn: +(runCpu / turns).toFixed(2), peakRssMiB: +peak.toFixed(1), rssAfterMiB: +rssAfter.toFixed(1),
}));
process.exit(0);
