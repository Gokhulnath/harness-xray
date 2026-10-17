// pi (@earendil-works/pi-coding-agent) as a library inside a Vercel function. Shared by variants A and B.
import { mkdirSync } from "node:fs";

const AGENT_DIR = "/tmp/pi-agent"; // only /tmp is writable on Vercel
export const PROVIDER = process.env.PI_PROVIDER || "openrouter";
export const MODEL_ID = process.env.PI_MODEL || "nvidia/nemotron-3-super-120b-a12b:free";

const g = globalThis;
g.__pi ??= { mod: null, runtime: null, model: null, load: null, sessions: new Map() };
export const piState = g.__pi;

export async function loadPi() {
	if (piState.mod) return piState.mod;
	const t0 = performance.now();
	const u0 = process.cpuUsage();
	const r0 = process.memoryUsage().rss;
	piState.mod = await import("@earendil-works/pi-coding-agent");
	const u = process.cpuUsage(u0);
	piState.load = { wallMs: Math.round(performance.now() - t0), cpuMs: +((u.user + u.system) / 1000).toFixed(1), rssMiB: +((process.memoryUsage().rss - r0) / 1048576).toFixed(1) };
	return piState.mod;
}

export async function getModel(pi) {
	if (piState.model) return piState.model;
	mkdirSync(AGENT_DIR, { recursive: true });
	piState.runtime = await pi.ModelRuntime.create({ authPath: `${AGENT_DIR}/auth.json`, modelsPath: `${AGENT_DIR}/models.json` });
	let model = piState.runtime.getModel(PROVIDER, MODEL_ID);
	if (!model) model = (await piState.runtime.getAvailable()).find((m) => m.provider === PROVIDER && m.id === MODEL_ID);
	if (!model) throw new Error(`model ${PROVIDER}/${MODEL_ID} not found (is the API key set?)`);
	piState.model = model;
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

/** One pi AgentSession per (variant, sessionId), kept in this instance's memory. */
export async function getSession(key, { cwd, customTools }) {
	const existing = piState.sessions.get(key);
	if (existing) return { ...existing, created: false };
	const pi = await loadPi();
	const model = await getModel(pi);
	const t0 = performance.now();
	const { session } = await pi.createAgentSession({
		cwd,
		agentDir: AGENT_DIR,
		model,
		modelRuntime: piState.runtime,
		resourceLoader: resourceLoader(pi),
		tools: ["read", "bash", "edit", "write"],
		customTools,
		sessionManager: pi.SessionManager.inMemory(cwd),
		settingsManager: pi.SettingsManager.inMemory({}),
	});
	const entry = { session, busy: false, createMs: Math.round(performance.now() - t0) };
	piState.sessions.set(key, entry);
	return { ...entry, created: true };
}
