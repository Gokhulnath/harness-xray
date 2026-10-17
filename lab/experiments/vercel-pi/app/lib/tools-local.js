// Variant A: pi's own built-in tools, unchanged. Commands run inside this Vercel function, with this
// function's environment, in a per-session workspace under /tmp.
import { execSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { SCENARIOS, scenarioOf } from "./workspace.js";

export function localWorkspace(sessionId, scenario = "cart") {
	const dir = `/tmp/ws-${sessionId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
	const s = SCENARIOS[scenarioOf(scenario)];
	for (const [name, content] of Object.entries(s.files)) {
		mkdirSync(dirname(`${dir}/${name}`), { recursive: true });
		writeFileSync(`${dir}/${name}`, content);
	}
	if (s.setup) execSync(s.setup, { cwd: dir, stdio: "ignore" });
	return { cwd: dir, customTools: undefined };
}
