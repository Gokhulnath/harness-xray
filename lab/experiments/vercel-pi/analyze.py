#!/usr/bin/env python3
"""Summarize lab/out/vercel-pi/<label>/*.json into per-variant tables (and write summary.json next to them).

  python3 analyze.py vercel            # one label
  python3 analyze.py vercel --runs     # also print every run

Cost estimates use published list prices (2026-10): Vercel Fluid compute Active CPU $0.128/CPU-hour and
provisioned memory $0.0106/GB-hour (2 GB default instance); E2B $0.000014/vCPU-s + $0.0000045/GiB-s
(base template, 2 vCPU / 512 MiB by E2B's docs). Rivet Compute pricing is not applied (not published per second)."""
import json, pathlib, statistics, sys

ROOT = pathlib.Path(__file__).resolve().parents[2] / "out" / "vercel-pi"
VERCEL_CPU_H, VERCEL_GB_H, VERCEL_GB = 0.128, 0.0106, 2.0
E2B_VCPU_S, E2B_GIB_S, E2B_VCPU, E2B_GIB = 0.000014, 0.0000045, 2, 0.5


def p50(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def rng(xs):
    xs = [x for x in xs if x is not None]
    return f"{min(xs):g}-{max(xs):g}" if xs else "-"


def per_run(r):
    d = r.get("done") or {}
    tl = d.get("timeline") or []
    tool_ms = sum((t["endMs"] - (t.get("startMs") or t["endMs"])) for t in tl if t["kind"] == "tool")
    wall = r["client"]["total_ms"]
    w = d.get("worker") or {}
    sb = d.get("sandbox") or {}
    fn_cpu = d.get("processCpuMs")
    child = d.get("toolChildCpuMs") or 0
    vercel_cost = ((fn_cpu or 0) + child) / 3.6e6 * VERCEL_CPU_H + (d.get("wallMs") or wall) / 3.6e6 * VERCEL_GB * VERCEL_GB_H
    e2b_cost = (wall / 1000) * (E2B_VCPU * E2B_VCPU_S + E2B_GIB * E2B_GIB_S) if r["variant"] in ("b", "c") else 0
    return {
        "file": r.get("_file"), "variant": r["variant"], "passed": r["passed"], "error": r.get("error"),
        "wall_s": round(wall / 1000, 1), "first_event_ms": r["client"]["first_event_ms"], "first_token_ms": d.get("firstTokenMs"),
        "cold": d.get("cold"), "instance": d.get("instanceId"), "tool_calls": len(r["tool_calls"]),
        "model_requests": (d.get("usage") or {}).get("requests"), "tokens_in": (d.get("usage") or {}).get("input"), "tokens_out": (d.get("usage") or {}).get("output"),
        "tool_time_ms": tool_ms, "fn_cpu_ms": fn_cpu, "tool_child_cpu_ms": child, "fn_peak_rss_mib": d.get("peakRssMiB"),
        "pi_load_cpu_ms": (d.get("piLoad") or {}).get("cpuMs") if d.get("cold") else None,
        "e2b_create_ms": sb.get("createMs") or next((e["ms"] for e in (w.get("sandboxEvents") or []) if e["kind"] == "create"), None),
        "sandbox_calls": sum((sb.get("calls") or {}).values()) if sb else None, "sandbox_roundtrip_ms": sb.get("roundTripMs"),
        "rivet_cpu_ms": w.get("processCpuMs"), "rivet_cgroup_cpu_ms": w.get("cgroupCpuMs"), "rivet_rss_mib": w.get("rssAfterMiB"),
        "rivet_peak_rss_mib": w.get("peakRssMiB"), "rivet_instance": w.get("instanceId"), "rivet_first_call_ms": (d.get("phases") or {}).get("firstCallMs"),
        "vercel_cost_usd": round(vercel_cost, 6), "e2b_cost_usd": round(e2b_cost, 6),
    }


def main():
    label = sys.argv[1]
    runs = []
    for f in sorted((ROOT / label).glob("[abc]-*.json")):
        r = json.loads(f.read_text())
        r["_file"] = f.name
        runs.append(per_run(r))
    if "--runs" in sys.argv:
        for x in runs:
            print(json.dumps(x))
    out = {}
    for v in sorted({x["variant"] for x in runs}):
        xs = [x for x in runs if x["variant"] == v]
        ok = [x for x in xs if not x["error"]]
        warm = [x for x in ok if not x["cold"]]
        cold = [x for x in ok if x["cold"]]
        s = {
            "runs": len(xs), "passed": sum(x["passed"] for x in xs), "errors": sum(1 for x in xs if x["error"]),
            "wall_s_p50": p50([x["wall_s"] for x in ok]), "wall_s_range": rng([x["wall_s"] for x in ok]),
            "first_token_ms_p50": p50([x["first_token_ms"] for x in ok]),
            "tool_calls_p50": p50([x["tool_calls"] for x in ok]), "model_requests_p50": p50([x["model_requests"] for x in ok]),
            "tool_time_ms_p50": p50([x["tool_time_ms"] for x in ok]),
            "fn_cpu_ms_cold_p50": p50([x["fn_cpu_ms"] for x in cold]), "fn_cpu_ms_warm_p50": p50([x["fn_cpu_ms"] for x in warm]),
            "fn_cpu_ms_all": rng([x["fn_cpu_ms"] for x in ok]), "tool_child_cpu_ms_p50": p50([x["tool_child_cpu_ms"] for x in ok]),
            "fn_peak_rss_mib_p50": p50([x["fn_peak_rss_mib"] for x in ok]), "cold_runs": len(cold), "warm_runs": len(warm),
            "pi_load_cpu_ms_p50": p50([x["pi_load_cpu_ms"] for x in cold]),
            "e2b_create_ms_p50": p50([x["e2b_create_ms"] for x in ok]), "sandbox_roundtrip_ms_p50": p50([x["sandbox_roundtrip_ms"] for x in ok]),
            "rivet_cpu_ms_p50": p50([x["rivet_cpu_ms"] for x in ok]), "rivet_cgroup_cpu_ms_p50": p50([x["rivet_cgroup_cpu_ms"] for x in ok]),
            "rivet_rss_mib_p50": p50([x["rivet_rss_mib"] for x in ok]), "rivet_first_call_ms_p50": p50([x["rivet_first_call_ms"] for x in ok]),
            "vercel_cost_usd_p50": p50([x["vercel_cost_usd"] * 1e6 for x in ok]) and round(p50([x["vercel_cost_usd"] * 1e6 for x in ok]) / 1e6, 6),
            "e2b_cost_usd_p50": p50([x["e2b_cost_usd"] * 1e6 for x in ok]) and round(p50([x["e2b_cost_usd"] * 1e6 for x in ok]) / 1e6, 6),
        }
        out[v] = s
    (ROOT / label / "summary.json").write_text(json.dumps({"runs": runs, "variants": out}, indent=1))
    keys = list(next(iter(out.values())).keys()) if out else []
    print(f"{'metric':28s}" + "".join(f"{v.upper():>16s}" for v in out))
    for k in keys:
        print(f"{k:28s}" + "".join(f"{str(out[v][k]):>16s}" for v in out))


if __name__ == "__main__":
    main()
