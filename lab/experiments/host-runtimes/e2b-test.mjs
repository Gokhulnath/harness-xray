import { Sandbox } from "e2b";
const t = () => Math.round(performance.now());
const t0 = t();
const sbx = await Sandbox.create({ metadata: { lab: "harness-xray", test: "bun-e2b" }, timeoutMs: 120_000 });
const tc = t();
try {
	const r = await sbx.commands.run("echo hello-from-e2b && uname -sr");
	const tr = t();
	await sbx.files.write("/home/user/x.txt", "written by " + (typeof Bun !== "undefined" ? "bun" : "node"));
	const back = await sbx.files.read("/home/user/x.txt");
	const tf = t();
	console.log(JSON.stringify({ runtime: typeof Bun !== "undefined" ? "bun " + Bun.version : "node " + process.versions.node, createMs: tc - t0, cmdMs: tr - tc, fileRoundTripMs: tf - tr, stdout: r.stdout.trim(), exit: r.exitCode, fileBack: back }));
} finally {
	await sbx.kill();
}
