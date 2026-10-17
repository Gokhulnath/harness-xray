// Workspaces for every scenario. Each variant seeds the same files (and runs the same setup command) into its workspace:
// a /tmp folder in the Vercel function (A), an E2B sandbox (B), or the Rivet actor's E2B sandbox (C).

const CART = {
	"package.json": JSON.stringify({ name: "cart", version: "1.0.0", type: "module", scripts: { test: "node --test" } }, null, 2) + "\n",
	"cart.js": `// Shopping cart helpers.
export function subtotal(items) {
	return items.reduce((sum, item) => sum + item.price, 0);
}

export function applyDiscount(total, percent) {
	return total - total * percent;
}

export function total(items, discountPercent = 0) {
	return Math.round(applyDiscount(subtotal(items), discountPercent) * 100) / 100;
}
`,
	"cart.test.js": `import assert from "node:assert/strict";
import { test } from "node:test";
import { applyDiscount, subtotal, total } from "./cart.js";

test("subtotal multiplies price by quantity", () => {
	assert.equal(subtotal([{ price: 2.5, qty: 4 }, { price: 10, qty: 1 }]), 20);
});

test("applyDiscount takes a whole-number percent", () => {
	assert.equal(applyDiscount(200, 10), 180);
});

test("total combines both", () => {
	assert.equal(total([{ price: 5, qty: 2 }], 50), 5);
});
`,
};

// H1: fixed CPU work (8M hashes). Same iteration count everywhere, so elapsed time shows each machine's CPU speed.
const CPU = {
	"bench/cpu.js": `// 8,000,000 chained SHA-256 hashes. Prints a checksum and the elapsed time.
import { createHash } from "node:crypto";
const N = 8_000_000;
let h = Buffer.from("harness-xray");
const t0 = performance.now();
for (let i = 0; i < N; i++) h = createHash("sha256").update(h).digest();
console.log("checksum " + h.toString("hex").slice(0, 16));
console.log("elapsed_ms " + Math.round(performance.now() - t0));
`,
	"package.json": JSON.stringify({ name: "cpu-bench", version: "1.0.0", type: "module" }, null, 2) + "\n",
};

// H2: real dependencies from the npm registry, two bugs in src.
const NPM = {
	"package.json": JSON.stringify({ name: "report", version: "1.0.0", type: "module", scripts: { test: "node --test" }, dependencies: { dayjs: "1.11.13", lodash: "4.17.21" } }, null, 2) + "\n",
	"src/report.js": `import dayjs from "dayjs";
import groupBy from "lodash/groupBy.js";

/** ISO timestamp -> calendar day as YYYY-MM-DD (UTC input). */
export function formatDay(iso) {
	return dayjs(iso).format("DD-MM-YYYY");
}

/** Count items per status, e.g. { open: 2, done: 1 }. */
export function countByStatus(items) {
	const groups = groupBy(items, "state");
	return Object.fromEntries(Object.entries(groups).map(([k, v]) => [k, v.length]));
}
`,
	"test/report.test.js": `import assert from "node:assert/strict";
import { test } from "node:test";
import { countByStatus, formatDay } from "../src/report.js";

test("formatDay returns YYYY-MM-DD", () => {
	assert.equal(formatDay("2026-10-15T10:00:00Z"), "2026-10-15");
});

test("countByStatus counts by the status field", () => {
	assert.deepEqual(countByStatus([{ status: "open" }, { status: "done" }, { status: "open" }]), { open: 2, done: 1 });
});
`,
};

// H3: a generated repository (300 modules, ~15 KB each). mod_137.js defines calculateShippingCost; 23 files import it.
const GEN_REPO = `// Generates src/modules/mod_000.js .. mod_299.js deterministically.
import { mkdirSync, writeFileSync } from "node:fs";
let seed = 42;
const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
const N = 300, TARGET = 137;
const importers = new Set();
while (importers.size < 23) { const i = Math.floor(rnd() * N); if (i !== TARGET) importers.add(i); }
mkdirSync("src/modules", { recursive: true });
const words = ["inventory", "ledger", "invoice", "parcel", "carrier", "warehouse", "tariff", "manifest", "route", "batch"];
for (let i = 0; i < N; i++) {
	const name = "mod_" + String(i).padStart(3, "0");
	const lines = [];
	if (importers.has(i)) lines.push('import { calculateShippingCost } from "./mod_' + TARGET + '.js";');
	const other = Math.floor(rnd() * N);
	if (other !== i && other !== TARGET) lines.push('import { helper' + other + ' } from "./mod_' + String(other).padStart(3, "0") + '.js";');
	lines.push("");
	for (let k = 0; k < 120; k++) lines.push("// " + Array.from({ length: 14 }, () => words[Math.floor(rnd() * words.length)]).join(" "));
	if (i === TARGET) {
		lines.push("/** Shipping cost in cents: base rate per zone plus a per-kg charge; free over 100 kg for zone A. */");
		lines.push("export function calculateShippingCost(weightKg, zone) {");
		lines.push("\\tconst base = { A: 500, B: 900, C: 1500 }[zone] ?? 2500;");
		lines.push("\\tif (zone === 'A' && weightKg > 100) return 0;");
		lines.push("\\treturn base + Math.ceil(weightKg) * 120;");
		lines.push("}");
	}
	lines.push("export function helper" + i + "(x) { return x * " + (i + 1) + "; }");
	if (importers.has(i)) lines.push("export const shipping" + i + " = (w) => calculateShippingCost(w, 'B');");
	writeFileSync("src/modules/" + name + ".js", lines.join("\\n") + "\\n");
}
console.log("generated " + N + " modules");
`;
const REPO = { "gen-repo.js": GEN_REPO, "package.json": JSON.stringify({ name: "big-repo", version: "1.0.0", type: "module" }, null, 2) + "\n" };

// H4: a command that runs longer than Vercel's 300 s limit (prints progress every 15 s).
const SLOW = {
	"bench/slow.js": `// Prints progress every 15 s for TOTAL seconds (default 330), then a DONE line.
const TOTAL = Number(process.argv[2] || 330);
let s = 0;
const t = setInterval(() => {
	s += 15;
	console.log("progress " + s + "s");
	if (s >= TOTAL) {
		clearInterval(t);
		console.log("DONE checksum-" + TOTAL * 7);
	}
}, 15000);
`,
	"package.json": JSON.stringify({ name: "slow", version: "1.0.0", type: "module" }, null, 2) + "\n",
};


// Crash tests: push the machine the tools run on past its memory and disk limits.
const OOM = {
	"bench/mem.js": `// Allocates 100 MB at a time (every page written) up to LIMIT MB (default 3000), printing progress.
const LIMIT = Number(process.argv[2] || 3000);
const keep = [];
for (let mb = 100; mb <= LIMIT; mb += 100) {
	keep.push(Buffer.alloc(100 * 1024 * 1024, 1));
	console.log("allocated " + mb + " MB");
}
console.log("DONE allocated " + LIMIT + " MB");
`,
	"package.json": JSON.stringify({ name: "oom", version: "1.0.0", type: "module" }, null, 2) + "\n",
};
const DISK = {
	"bench/disk.js": `// Writes 50 MB files into ./fill until LIMIT MB (default 800) or the disk is full, printing progress.
import { mkdirSync, writeFileSync } from "node:fs";
const LIMIT = Number(process.argv[2] || 800);
mkdirSync("fill", { recursive: true });
const chunk = Buffer.alloc(50 * 1024 * 1024, 7);
let mb = 0;
try {
	for (; mb < LIMIT; mb += 50) {
		writeFileSync("fill/f" + mb + ".bin", chunk);
		console.log("wrote " + (mb + 50) + " MB");
	}
	console.log("DONE wrote " + mb + " MB");
} catch (e) {
	console.log("FAILED after " + mb + " MB: " + e.code);
}
`,
	"package.json": JSON.stringify({ name: "disk", version: "1.0.0", type: "module" }, null, 2) + "\n",
};

export const SCENARIOS = {
	cart: { files: CART, setup: null },
	cpu: { files: CPU, setup: null },
	npm: { files: NPM, setup: null },
	repo: { files: REPO, setup: "node gen-repo.js" },
	slow: { files: SLOW, setup: null },
	memory: { files: CART, setup: null },
	oom: { files: OOM, setup: null },
	disk: { files: DISK, setup: null },
};

export function scenarioOf(name) {
	return SCENARIOS[name] ? name : "cart";
}

// kept for the existing callers
export const WORKSPACE_FILES = CART;
