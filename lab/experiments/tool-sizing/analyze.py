#!/usr/bin/env python3
"""Summarize lab/out/tool-sizing/*.jsonl into markdown tables (printed, and written to lab/out/tool-sizing/summary.md).

CPU-seconds have the step wrapper's own sampling cost removed (measured by the overhead steps, ~1% of a core).
"Process memory" = anon + kernel + shmem (what cannot be dropped); "peak" = memory.peak, which also counts page cache.
"""
import collections, json, pathlib, statistics as st, sys

OUT = pathlib.Path(__file__).resolve().parents[1].parent / "out" / "tool-sizing"
EXPECTED_FAIL = {"cart-test", "cart-npm-test", "cart-test-with-dev"}  # the cart project has two deliberate bugs


def load(phase):
    """Rows of one phase, minus harness failures (the step wrapper produced no output, e.g. the first next-dev floor run)."""
    p = OUT / f"{phase}.jsonl"
    rows = [json.loads(l) for l in open(p)] if p.exists() else []
    return [r for r in rows if r.get("error") != "no step output"]


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def overhead_rate(rows):
    o = [r["cpu_s"] / r["wall_s"] for r in rows if r.get("group", "").startswith("overhead") and r.get("wall_s")]
    return med(o) or 0.0


def ok(r):
    return r.get("rc") == 0 or (r.get("group") in EXPECTED_FAIL and r.get("rc") == 1)


def f(x, nd=1):
    return "-" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(f(c) if not isinstance(c, str) else c for c in r) + " |" for r in rows]
    return "\n".join(out)


def by_group(rows):
    g = collections.OrderedDict()
    for r in rows:
        g.setdefault(r.get("group") or r["name"], []).append(r)
    return g


def profile_section(rows):
    if not rows:
        return ""
    ov = overhead_rate(rows)
    lines = [f"## Profile: 2 CPUs, local disk (median of repeats; wrapper overhead {ov * 1000:.0f} ms CPU per s removed)\n"]
    body = []
    for g, rs in by_group([r for r in rows if r.get("variant") != "cold-cache"]).items():
        if g.startswith("overhead"):
            continue
        good = [r for r in rs if ok(r)]
        bad = len(rs) - len(good)
        cpu = med([r["cpu_s"] - ov * r["wall_s"] for r in good])
        body.append([g, len(good), med([r["wall_s"] for r in good]), cpu, med([r["cores_avg"] for r in good]),
                     med([r["cores_peak_0_5s"] for r in good]), med([r["nonfile_peak_mb"] for r in good]),
                     med([r["anon_peak_mb"] for r in good]), med([r["mem_peak_mb"] for r in good]),
                     med([r["io_wmb"] for r in good]), f"{bad} failed" if bad else ""])
    lines.append(table(["step", "n", "wall s", "CPU s", "avg cores", "peak cores (0.5 s)", "process MiB", "anon MiB",
                        "peak MiB (incl. cache)", "disk write MiB", ""], body))
    cold = [r for r in rows if r.get("variant") == "cold-cache" and r["group"] == "next-install-cold"]
    if cold:
        r = cold[0]
        lines.append(f"\nCold npm cache (registry download) for the Next app: {r['wall_s']:.1f} s wall, {r['cpu_s']:.1f} CPU-s, "
                     f"peak {r['mem_peak_mb']} MiB, process {r['nonfile_peak_mb']} MiB.")
    return "\n".join(lines)


def sweep_section(rows, key, title):
    if not rows:
        return ""
    ov = overhead_rate(rows)
    vals = sorted({r[key] for r in rows})
    groups = by_group(rows)
    body = []
    for g, rs in groups.items():
        if g.startswith("overhead") or g.endswith("checkout"):
            continue
        row = [g]
        base = None
        for v in vals:
            good = [r for r in rs if r[key] == v and ok(r)]
            w = med([r["wall_s"] for r in good])
            if v == max(vals):
                base = w
            row.append(w if good else ("FAIL" if [r for r in rs if r[key] == v] else None))
        cpu = med([r["cpu_s"] - ov * r["wall_s"] for r in rs if ok(r)])
        row.append(cpu)
        thr = [r for r in rs if r[key] == min(vals) and ok(r)]
        row.append(med([r["throttled_s"] for r in thr]))
        body.append(row)
    return f"## {title}\n\n" + table(["step"] + [f"{v} wall s" for v in vals] + ["CPU s (all runs)", f"throttled s at {min(vals)}"], body)


def mem_section(rows):
    """Lowest limit that passed at all, and the lowest 'comfortable' limit: passed with wall time within 25% of the 3 GiB run."""
    if not rows:
        return ""
    body = []
    for g, rs in by_group([r for r in rows if r.get("role") == "target"]).items():
        passed = sorted([r for r in rs if ok(r)], key=lambda r: r["limit_mb"])
        failed = sorted([r for r in rs if not ok(r)], key=lambda r: r["limit_mb"])
        hi = next((r for r in sorted(rs, key=lambda r: -r["limit_mb"]) if ok(r)), None)
        lo = passed[0] if passed else None
        comfy = next((r for r in passed if hi and r["wall_s"] <= 1.25 * hi["wall_s"] + 0.2), None)
        how = ""
        if failed:
            fr = failed[-1]
            how = f"OOM kill after {fr['wall_s']:.0f} s" if fr.get("oom_kills") else f"rc={fr.get('rc')} after {fr.get('wall_s', 0):.0f} s"
        body.append([g, comfy["limit_mb"] if comfy else "none", lo["limit_mb"] if lo else "none",
                     failed[-1]["limit_mb"] if failed else "-", how,
                     hi["wall_s"] if hi else None, lo["wall_s"] if lo else None,
                     hi["nonfile_peak_mb"] if hi else None, hi["mem_peak_mb"] if hi else None])
    return ("## Memory floor (2 CPUs)\n\nComfortable = lowest limit where wall time stays within 25% of the 3 GiB run. "
            "Below it the step still passes but thrashes on page cache.\n\n" + table(
        ["step", "comfortable MiB", "lowest passing MiB", "first failing MiB", "how it failed", "wall s at 3 GiB",
         "wall s at lowest pass", "process MiB at 3 GiB", "peak MiB at 3 GiB"], body))


def main():
    parts = [profile_section(load("profile")),
             sweep_section(load("calib"), "cpus", "Calibration runs (1 and 2 CPUs)"),
             sweep_section(load("cpu"), "cpus", "CPU sweep: wall time by CPU limit (local disk)"),
             sweep_section(load("period"), "period_us", "0.25 CPU: CFS period 10 ms vs 100 ms"),
             mem_section(load("mem")),
             sweep_section(load("storage"), "storage", "Shared storage: wall time by where /work lives (2 CPUs)"),
             sweep_section(load("nfsopts"), "variant", "NFS options (nfs + 1 ms latency)")]
    text = "\n\n".join(p for p in parts if p)
    (OUT / "summary.md").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
