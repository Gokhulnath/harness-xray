// 8,000,000 chained SHA-256 hashes. Prints a checksum and the elapsed time.
import { createHash } from "node:crypto";
const N = 8_000_000;
let h = Buffer.from("harness-xray");
const t0 = performance.now();
for (let i = 0; i < N; i++) h = createHash("sha256").update(h).digest();
console.log("checksum " + h.toString("hex").slice(0, 16));
console.log("elapsed_ms " + Math.round(performance.now() - t0));
