#!/usr/bin/env python3
"""Summarize lab/out/cc-offload/runs.jsonl: Claude Code container CPU and memory, split by process kind, per mode."""
import collections, json, pathlib, statistics as st

OUT = pathlib.Path(__file__).resolve().parents[1].parent / "out" / "cc-offload"
HOOK = {"jq", "curl", "base64", "sha256sum", "cp", "mkdir", "cat", "rexec", "hook.sh", "dirname", "awk"}
TOOLS = {"node", "npm", "npx", "next-server", "tsc", "eslint", "git", "rg", "find", "du", "head", "ls", "sh", "python3"}


def kind(p, main_pid):
    c = p["comm"]
    if p["pid"] == main_pid:
        return "claude (loop)"
    if c == "claude":
        return "claude helpers"
    if c.startswith("python3") and "sampler.py" in p["cmd"]:
        return "sampler"
    if c in ("bash",) and "hook.sh" in p["cmd"] or c in HOOK or "rexec" in p["cmd"]:
        return "hooks + rexec"
    if c == "bash":
        return "bash wrappers"
    return "tool processes"


def split(summary):
    procs = summary["processes"]
    claudes = [p for p in procs if p["comm"] == "claude"]
    main = max(claudes, key=lambda p: p["cpu_s"]) if claudes else None
    out = collections.Counter()
    for p in procs:
        out[kind(p, main and main["pid"])] += p["cpu_s"]
    out["unattributed (short-lived)"] = max(0.0, summary["cgroup_cpu_s"] - summary["sampled_cpu_s"])
    return out, main


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 2) if xs else None


def main():
    runs = [json.loads(l) for l in open(OUT / "runs.jsonl")]
    by = collections.defaultdict(list)
    for r in runs:
        if r.get("cc") and not r["rid"].startswith("smoke"):
            by[f"{r['mode']} {'real ' + r.get('model', '') if r.get('real') else 'mock'}"].append(r)
    lines = ["| metric | " + " | ".join(f"{m} (n={len(v)})" for m, v in sorted(by.items())) + " |",
             "|---|" + "|".join("---" for _ in by) + "|"]

    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(str(med([fn(r) for r in v])) for _, v in sorted(by.items())) + " |")

    row("session wall s", lambda r: r["wall_s"])
    row("API cost reported (USD)", lambda r: r.get("cost_usd"))
    row("tool calls", lambda r: r["tool_uses"])
    row("CC container CPU ms per tool call", lambda r: round(1000 * r["cc"]["cgroup_cpu_s"] / max(1, r["tool_uses"]), 1))
    row("tool errors", lambda r: r["tool_errors"])
    row("Claude Code container CPU s", lambda r: r["cc"]["cgroup_cpu_s"])
    row("  of which claude process (loop) CPU s", lambda r: split(r["cc"])[1]["cpu_s"])
    for k in ("claude helpers", "bash wrappers", "hooks + rexec", "tool processes", "sampler", "unattributed (short-lived)"):
        row(f"  of which {k} CPU s", lambda r, k=k: round(split(r["cc"])[0][k], 2))
    row("Claude Code container memory.peak MiB", lambda r: r["cc"]["mem_peak_mb"])
    row("Claude Code container anon peak MiB", lambda r: r["cc"]["anon_peak_mb"])
    row("claude process peak RSS MiB", lambda r: split(r["cc"])[1]["rss_peak_mb"])
    row("claude process peak threads", lambda r: split(r["cc"])[1]["threads_peak"])
    row("tools container CPU s", lambda r: (r.get("tools") or {}).get("cgroup_cpu_s"))
    row("tools container memory.peak MiB", lambda r: (r.get("tools") or {}).get("mem_peak_mb"))
    row("tools container anon peak MiB", lambda r: (r.get("tools") or {}).get("anon_peak_mb"))
    text = "## Claude Code: tools local (A) vs offloaded through hooks (C), median per mode\n\n" + "\n".join(lines)
    (OUT / "summary.md").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
