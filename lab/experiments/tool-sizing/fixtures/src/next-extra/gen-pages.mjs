// Adds weight to a fresh create-next-app: 12 shared components, 40 routes (server page + client widget each),
// and a distDir taken from DIST_DIR so repeated builds can write to fresh folders.
import fs from "node:fs";
import path from "node:path";

const root = process.argv[2] || ".";
const w = (p, s) => { fs.mkdirSync(path.dirname(path.join(root, p)), { recursive: true }); fs.writeFileSync(path.join(root, p), s); };

for (let c = 0; c < 12; c++) {
  w(`components/Card${c}.tsx`, `type Props = { title: string; items: { id: number; label: string; value: number }[] };
export default function Card${c}({ title, items }: Props) {
  const total = items.reduce((s, i) => s + i.value, 0);
  return (
    <section className="rounded-xl border p-4 shadow-sm">
      <h2 className="text-lg font-semibold">{title}</h2>
      <ul className="mt-2 space-y-1">{items.map((i) => <li key={i.id} className="flex justify-between"><span>{i.label}</span><span>{i.value.toFixed(2)}</span></li>)}</ul>
      <p className="mt-2 text-sm text-gray-500">Total {total.toFixed(2)}</p>
    </section>
  );
}
`);
}
w("components/Counter.tsx", `"use client";
import { useMemo, useState } from "react";
export default function Counter({ start, step }: { start: number; step: number }) {
  const [n, setN] = useState(start);
  const squares = useMemo(() => Array.from({ length: 20 }, (_, i) => (n + i) ** 2), [n]);
  return (
    <div className="flex items-center gap-3">
      <button className="rounded bg-black px-3 py-1 text-white" onClick={() => setN(n + step)}>+{step}</button>
      <span>{n}</span><span className="text-xs text-gray-400">{squares.slice(0, 5).join(", ")}</span>
    </div>
  );
}
`);
w("lib/data.ts", `export function rows(seed: number, count = 25) {
  let x = seed;
  return Array.from({ length: count }, (_, i) => { x = (x * 1103515245 + 12345) % 2147483648; return { id: i, label: "Row " + i, value: (x % 10000) / 100 }; });
}
`);
for (let p = 0; p < 40; p++) {
  const id = String(p).padStart(2, "0");
  const a = p % 12, b = (p + 5) % 12;
  w(`app/gen/p${id}/page.tsx`, `import Card${a} from "@/components/Card${a}";
import Card${b} from "@/components/Card${b}";
import Counter from "@/components/Counter";
import { rows } from "@/lib/data";

export const metadata = { title: "Page ${id}" };

export default function Page() {
  const data = rows(${p + 1});
  return (
    <main className="mx-auto max-w-3xl space-y-6 p-8">
      <h1 className="text-2xl font-bold">Generated page ${id}</h1>
      <Counter start={${p}} step={${(p % 5) + 1}} />
      <Card${a} title="Section A" items={data.slice(0, 12)} />
      <Card${b} title="Section B" items={data.slice(12)} />
    </main>
  );
}
`);
}
const cfgPath = ["next.config.ts", "next.config.mjs", "next.config.js"].map((f) => path.join(root, f)).find(fs.existsSync);
fs.writeFileSync(cfgPath || path.join(root, "next.config.ts"), `import type { NextConfig } from "next";

// DIST_DIR: fresh output folder per repeated build. NEXT_CPUS: build workers as a real N-vCPU VM would get them
// (Docker's --cpus does not change os.cpus(), which Next uses by default).
const nextConfig: NextConfig = {
  distDir: process.env.DIST_DIR || ".next",
  experimental: process.env.NEXT_CPUS ? { cpus: Number(process.env.NEXT_CPUS) } : {},
};

export default nextConfig;
`);
console.log("generated 40 pages, 13 components");
