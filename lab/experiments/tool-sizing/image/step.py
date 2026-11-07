#!/usr/bin/env python3
"""Run one tool call inside the sandbox and measure it from the container's own cgroup v2 files.

  step NAME [--cwd DIR] [--timeout S] [--reclaim] -- SHELL COMMAND...

--reclaim drops the container's reclaimable page cache first, so the step's memory peak is its own working set and not
cache left behind by earlier steps (memory.peak restarts from the current usage when it is reset).

Prints one JSON object on stdout. The command's output goes to /tmp/steplogs/NAME.log (the tail is included on failure).
CPU comes from cpu.stat deltas; memory from memory.peak (reset per step through its own file descriptor) plus 50 ms
samples of memory.current and memory.stat, so the peak can be split into process memory (anon) and page cache (file).
"""
import json, os, subprocess, sys, threading, time

CG = "/sys/fs/cgroup"
SAMPLE_S = 0.05


def kv(name):
    out = {}
    try:
        with open(f"{CG}/{name}") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if v.strip().lstrip("-").isdigit():
                    out[k] = int(v)
    except OSError:
        pass
    return out


def one(name):
    try:
        with open(f"{CG}/{name}") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError):
        return None


def io_totals():
    tot = {"rbytes": 0, "wbytes": 0, "rios": 0, "wios": 0}
    try:
        with open(f"{CG}/io.stat") as f:
            for line in f:
                for part in line.split()[1:]:
                    k, _, v = part.partition("=")
                    if k in tot:
                        tot[k] += int(v)
    except OSError:
        pass
    return tot


def pressure(name):
    """Total stall microseconds for some/full from a PSI file."""
    out = {}
    try:
        with open(f"{CG}/{name}") as f:
            for line in f:
                kind, *fields = line.split()
                out[kind] = int(dict(x.split("=") for x in fields)["total"])
    except (OSError, KeyError, ValueError):
        pass
    return out


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.mx = {"current": 0, "anon": 0, "file": 0, "kernel": 0, "shmem": 0, "nonfile": 0}
        self.cpu = []  # (t, usage_usec)
        self.n = 0

    def run(self):
        while not self.stop.is_set():
            t = time.monotonic()
            cur = one("memory.current") or 0
            st = kv("memory.stat")
            usage = kv("cpu.stat").get("usage_usec", 0)
            self.cpu.append((t, usage))
            vals = {"current": cur, "anon": st.get("anon", 0), "file": st.get("file", 0), "kernel": st.get("kernel", 0),
                    "shmem": st.get("shmem", 0)}
            vals["nonfile"] = vals["anon"] + vals["kernel"] + vals["shmem"]
            for k, v in vals.items():
                if v > self.mx[k]:
                    self.mx[k] = v
            self.n += 1
            self.stop.wait(SAMPLE_S)

    def peak_cores(self, window):
        best, j = 0.0, 0
        for i in range(len(self.cpu)):
            while self.cpu[i][0] - self.cpu[j][0] > window:
                j += 1
            dt = self.cpu[i][0] - self.cpu[j][0]
            if dt >= window * 0.8:
                best = max(best, (self.cpu[i][1] - self.cpu[j][1]) / 1e6 / dt)
        return round(best, 3)


def main():
    args = sys.argv[1:]
    if "--" not in args or not args:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sep = args.index("--")
    opts, cmd = args[:sep], " ".join(args[sep + 1:])
    name, cwd, timeout, reclaim = opts[0], None, None, False
    i = 1
    while i < len(opts):
        if opts[i] == "--cwd":
            cwd = opts[i + 1]; i += 2
        elif opts[i] == "--timeout":
            timeout = float(opts[i + 1]); i += 2
        elif opts[i] == "--reclaim":
            reclaim = True; i += 1
        else:
            i += 1
    os.makedirs("/tmp/steplogs", exist_ok=True)
    log_path = f"/tmp/steplogs/{name}.log"

    if reclaim:
        try:
            with open(f"{CG}/memory.reclaim", "w") as f:
                f.write("16G")
        except OSError:
            pass  # EAGAIN when not everything could be reclaimed; what was reclaimable is gone
    start_mb = (one("memory.current") or 0) / 1048576
    peak_fd = None
    try:
        peak_fd = open(f"{CG}/memory.peak", "r+")
        peak_fd.write("reset\n"); peak_fd.flush()
    except OSError:
        peak_fd = None
    c0, m0, e0, io0 = kv("cpu.stat"), kv("memory.stat"), kv("memory.events"), io_totals()
    pm0, pc0, pi0 = pressure("memory.pressure"), pressure("cpu.pressure"), pressure("io.pressure")
    s = Sampler(); s.start()
    t0 = time.monotonic()
    timed_out = False
    with open(log_path, "wb") as log:
        p = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill(); rc = p.wait(); timed_out = True
    wall = time.monotonic() - t0
    s.stop.set(); s.join()
    c1, m1, e1, io1 = kv("cpu.stat"), kv("memory.stat"), kv("memory.events"), io_totals()
    pm1, pc1, pi1 = pressure("memory.pressure"), pressure("cpu.pressure"), pressure("io.pressure")
    peak = None
    if peak_fd:
        peak_fd.seek(0); peak = int(peak_fd.read().split()[0]); peak_fd.close()

    d = lambda a, b, k: (b.get(k, 0) - a.get(k, 0))
    mb = lambda x: round(x / 1048576, 1) if x is not None else None
    cpu_s = d(c0, c1, "usage_usec") / 1e6
    out = {
        "name": name, "rc": rc, "timed_out": timed_out, "wall_s": round(wall, 3),
        "cpu_s": round(cpu_s, 3), "user_s": round(d(c0, c1, "user_usec") / 1e6, 3), "sys_s": round(d(c0, c1, "system_usec") / 1e6, 3),
        "cores_avg": round(cpu_s / wall, 3) if wall else None,
        "cores_peak_0_5s": s.peak_cores(0.5), "cores_peak_5s": s.peak_cores(5.0),
        "throttled_s": round(d(c0, c1, "throttled_usec") / 1e6, 3), "nr_throttled": d(c0, c1, "nr_throttled"),
        "mem_peak_mb": mb(peak), "current_peak_mb": mb(s.mx["current"]), "anon_peak_mb": mb(s.mx["anon"]),
        "file_peak_mb": mb(s.mx["file"]), "kernel_peak_mb": mb(s.mx["kernel"]), "shmem_peak_mb": mb(s.mx["shmem"]),
        "nonfile_peak_mb": mb(s.mx["nonfile"]), "current_end_mb": mb(one("memory.current")),
        "oom_kills": d(e0, e1, "oom_kill"), "mem_max_events": d(e0, e1, "max"), "mem_high_events": d(e0, e1, "high"),
        "refault_file": d(m0, m1, "workingset_refault_file"), "refault_anon": d(m0, m1, "workingset_refault_anon"),
        "pgmajfault": d(m0, m1, "pgmajfault"),
        "psi_mem_some_s": round(d(pm0, pm1, "some") / 1e6, 3), "psi_mem_full_s": round(d(pm0, pm1, "full") / 1e6, 3),
        "psi_cpu_some_s": round(d(pc0, pc1, "some") / 1e6, 3), "psi_io_some_s": round(d(pi0, pi1, "some") / 1e6, 3),
        "io_rmb": mb(d(io0, io1, "rbytes")), "io_wmb": mb(d(io0, io1, "wbytes")),
        "start_mb": round(start_mb, 1), "reclaimed": reclaim, "samples": s.n,
    }
    if rc != 0:
        with open(log_path, "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 1500))
            out["log_tail"] = f.read().decode(errors="replace")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
