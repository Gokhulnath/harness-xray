// pi loaded as a library inside the Next.js server process.
// One process, many chat sessions: pi's code is loaded once and shared, each session is an
// in-memory AgentSession. Model calls go out from here; tools go to the sandbox.
import { mkdirSync, writeFileSync } from "node:fs";
import { remoteTools, sandboxCalls } from "./sandbox.js";

const AGENT_DIR = "/tmp/pi-agent"; // only /tmp is writable on Vercel
const CWD = "/workspace"; // a path inside the sandbox, not on this machine
const PROVIDER = process.env.PI_PROVIDER || "openrouter";
const MODEL_ID = process.env.PI_MODEL || "nvidia/nemotron-3-super-120b-a12b:free";

const g = globalThis;
g.__pi ??= { mod: null, runtime: null, model: null, load: null, sessions: new Map(), prompts: 0 };
const state = g.__pi;

const cpuMs = () => {
	const u = process.cpuUsage();
	return (u.user + u.system) / 1000;
};

async function loadPi() {
	if (state.mod) return state.mod;
	const t0 = performance.now();
	const c0 = cpuMs();
	const rss0 = process.memoryUsage().rss;
	state.mod = await import("@earendil-works/pi-coding-agent");
	state.load = { wallMs: performance.now() - t0, cpuMs: cpuMs() - c0, rssMiB: (process.memoryUsage().rss - rss0) / 1048576 };
	return state.mod;
}

async function getModel(pi) {
	if (state.model) return state.model;
	mkdirSync(AGENT_DIR, { recursive: true });
	if (PROVIDER === "mock") {
		const m = { id: "mock-1", name: "Mock", contextWindow: 200000, maxTokens: 16384, reasoning: false, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
		writeFileSync(`${AGENT_DIR}/models.json`, JSON.stringify({ providers: { mock: { baseUrl: process.env.MOCK_BASE_URL, api: "openai-completions", apiKey: "mock", models: [m] } } }));
	}
	state.runtime = await pi.ModelRuntime.create({ authPath: `${AGENT_DIR}/auth.json`, modelsPath: `${AGENT_DIR}/models.json` });
	let model = state.runtime.getModel(PROVIDER, MODEL_ID);
	if (!model) model = (await state.runtime.getAvailable()).find((m) => m.provider === PROVIDER && m.id === MODEL_ID);
	if (!model) throw new Error(`model ${PROVIDER}/${MODEL_ID} not found (is the API key set?)`);
	state.model = model;
	return model;
}

function resourceLoader(pi) {
	// No disk discovery (no ~/.pi, no AGENTS.md scan); pi's default coding system prompt.
	return {
		getExtensions: () => ({ extensions: [], errors: [], runtime: pi.createExtensionRuntime() }),
		getSkills: () => ({ skills: [], diagnostics: [] }),
		getPrompts: () => ({ prompts: [], diagnostics: [] }),
		getThemes: () => ({ themes: [], diagnostics: [] }),
		getAgentsFiles: () => ({ agentsFiles: [] }),
		getSystemPrompt: () => undefined,
		getSystemPromptSource: () => undefined,
		getAppendSystemPrompt: () => [],
		getAppendSystemPromptSources: () => [],
		extendResources: () => {},
		reload: async () => {},
	};
}

export async function getSession(id) {
	const existing = state.sessions.get(id);
	if (existing) return existing;
	const pi = await loadPi();
	const model = await getModel(pi);
	const { session } = await pi.createAgentSession({
		cwd: CWD,
		agentDir: AGENT_DIR,
		model,
		modelRuntime: state.runtime,
		resourceLoader: resourceLoader(pi),
		tools: ["read", "bash", "edit", "write"],
		customTools: remoteTools(pi, CWD),
		sessionManager: pi.SessionManager.inMemory(CWD),
		settingsManager: pi.SettingsManager.inMemory({}),
	});
	const entry = { session, busy: false, turns: 0, created: Date.now() };
	state.sessions.set(id, entry);
	return entry;
}

export function stats() {
	const m = process.memoryUsage();
	return {
		pid: process.pid,
		uptimeS: +process.uptime().toFixed(1),
		cpuMs: +cpuMs().toFixed(1),
		rssMiB: +(m.rss / 1048576).toFixed(1),
		heapUsedMiB: +(m.heapUsed / 1048576).toFixed(1),
		externalMiB: +(m.external / 1048576).toFixed(1),
		piLoaded: !!state.mod,
		piLoad: state.load,
		model: state.model ? `${state.model.provider}/${state.model.id}` : null,
		sessions: state.sessions.size,
		prompts: state.prompts,
		sandboxCalls,
	};
}

export function countPrompt() {
	state.prompts++;
}
