// Spread node_modules entries across N buckets of similar size (greedy, largest first) so the image is pushed as
// several smaller layers: Rivet's registry times out committing one large layer.
const fs = require("node:fs");
const path = require("node:path");
const { execSync } = require("node:child_process");
const N = Number(process.argv[2] || 8);
const nm = "node_modules";
const entries = [];
for (const e of fs.readdirSync(nm)) {
	if (e.startsWith("@")) for (const s of fs.readdirSync(`${nm}/${e}`)) entries.push(`${e}/${s}`);
	else entries.push(e);
}
const kb = (p) => +execSync(`du -sk "${nm}/${p}"`).toString().split("\t")[0];
const items = entries.map((p) => ({ p, s: kb(p) })).sort((a, b) => b.s - a.s);
const bins = Array.from({ length: N }, (_, i) => ({ i, s: 0, items: [] }));
for (const it of items) {
	bins.sort((a, b) => a.s - b.s);
	bins[0].items.push(it.p);
	bins[0].s += it.s;
}
for (const b of bins.sort((a, b) => a.i - b.i)) {
	fs.mkdirSync(`/split/b${b.i}`, { recursive: true });
	for (const p of b.items) {
		const dst = `/split/b${b.i}/${p}`;
		fs.mkdirSync(path.dirname(dst), { recursive: true });
		fs.renameSync(`${nm}/${p}`, dst);
	}
	console.log(`bucket ${b.i}: ${Math.round(b.s / 1024)} MB, ${b.items.length} entries`);
}
