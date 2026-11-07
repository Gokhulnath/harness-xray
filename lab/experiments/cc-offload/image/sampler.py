#!/usr/bin/env python3
"""Sample this container's cgroup and every process in it every 100 ms until SIGTERM, then write a summary.

  sampler.py OUT_PREFIX     -> OUT_PREFIX.samples.jsonl (time series) and OUT_PREFIX.summary.json

Summary: cgroup CPU-seconds and memory.peak since start (memory.peak is reset through its own fd), the sampled maximum
of anon / file / memory.current, and one row per process seen: pid, ppid, comm, cmdline, CPU-seconds (utime+stime at the
last sample), peak RSS, threads. Processes shorter than one interval may be missed; their CPU still shows in the cgroup
total, so "unattributed" = cgroup CPU - sum of sampled processes.
"""
import json, os, signal, sys, time

CG = "/sys/fs/cgroup"
TICK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
INTERVAL = 0.1
stop = False


def handler(*_):
    global stop
    stop = True


def kv(name):
    out = {}
    try:
        for line in open(f"{CG}/{name}"):
            k, _, v = line.partition(" ")
            if v.strip().isdigit():
                out[k] = int(v)
    except OSError:
        pass
    return out


def one(name):
    try:
        return int(open(f"{CG}/{name}").read().split()[0])
    except (OSError, ValueError):
        return 0


def procs():
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            raw = open(f"/proc/{d}/stat").read()
            comm = raw[raw.index("(") + 1:raw.rindex(")")]
            f = raw[raw.rindex(")") + 2:].split()
            out[int(d)] = (comm, int(f[1]), int(f[11]) + int(f[12]), int(f[21]) * PAGE, int(f[17]))  # comm, ppid, ticks, rss, threads
        except (OSError, ValueError, IndexError):
            pass
    return out


def main():
    prefix = sys.argv[1]
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    peak_fd = None
    try:
        peak_fd = open(f"{CG}/memory.peak", "r+")
        peak_fd.write("reset\n"); peak_fd.flush()
    except OSError:
        pass
    me = os.getpid()
    c0, t0 = kv("cpu.stat").get("usage_usec", 0), time.time()
    seen, mx = {}, {"current": 0, "anon": 0, "file": 0, "nonfile": 0}
    with open(f"{prefix}.samples.jsonl", "w") as out:
        while not stop:
            t = time.time()
            st = kv("memory.stat")
            cur = one("memory.current")
            usage = kv("cpu.stat").get("usage_usec", 0)
            ps = procs()
            for pid, (comm, ppid, ticks, rss, thr) in ps.items():
                s = seen.get(pid)
                if s is None or s["comm"] != comm and s["ticks"] > ticks:  # pid reuse
                    try:
                        cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")[:200]
                    except OSError:
                        cmd = ""
                    s = seen[pid] = {"pid": pid, "ppid": ppid, "comm": comm, "cmd": cmd, "ticks": 0, "rss_peak": 0, "threads_peak": 0,
                                     "first": round(t - t0, 2)}
                s["comm"] = comm
                s["ticks"] = max(s["ticks"], ticks)
                s["rss_peak"] = max(s["rss_peak"], rss)
                s["threads_peak"] = max(s["threads_peak"], thr)
                s["last"] = round(t - t0, 2)
            vals = {"current": cur, "anon": st.get("anon", 0), "file": st.get("file", 0)}
            vals["nonfile"] = vals["anon"] + st.get("kernel", 0) + st.get("shmem", 0)
            for k, v in vals.items():
                mx[k] = max(mx[k], v)
            out.write(json.dumps({"t": round(t - t0, 3), "cpu_us": usage - c0, **vals,
                                  "p": {pid: [v[0], v[2], v[3]] for pid, v in ps.items()}}) + "\n")
            time.sleep(max(0.0, INTERVAL - (time.time() - t)))
    peak = None
    if peak_fd:
        peak_fd.seek(0); peak = int(peak_fd.read().split()[0])
    rows = sorted(seen.values(), key=lambda r: -r["ticks"])
    for r in rows:
        r["cpu_s"] = round(r.pop("ticks") / TICK, 3)
        r["rss_peak_mb"] = round(r.pop("rss_peak") / 1048576, 1)
    cg_cpu = (kv("cpu.stat").get("usage_usec", 0) - c0) / 1e6
    summary = {"wall_s": round(time.time() - t0, 2), "cgroup_cpu_s": round(cg_cpu, 3),
               "mem_peak_mb": round(peak / 1048576, 1) if peak else None,
               **{f"{k}_peak_mb": round(v / 1048576, 1) for k, v in mx.items()},
               "sampler_cpu_s": next((r["cpu_s"] for r in rows if r["pid"] == me), None),
               "sampled_cpu_s": round(sum(r["cpu_s"] for r in rows), 3), "processes": rows}
    json.dump(summary, open(f"{prefix}.summary.json", "w"), indent=1)


if __name__ == "__main__":
    main()
