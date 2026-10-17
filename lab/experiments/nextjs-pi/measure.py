#!/usr/bin/env python3
"""Drive the Next.js+pi proof of concept and record where the compute goes.

  python3 measure.py chat "message" [--session ID]     one chat turn, with container CPU/memory and process sampling
  python3 measure.py sessions N "message"              N sessions one after another (memory per extra session)

Container numbers come from each container's own cgroup (cpu.stat usage_usec, memory.current/peak).
Process sampling uses `docker top` every 0.25 s to show which processes ran in which container."""
import json, subprocess, sys, threading, time, urllib.request

APP = "http://127.0.0.1:3100"
BOXES = ["xray-next", "xray-sandbox"]


def cgroup(name):
    out = subprocess.run(["docker", "exec", name, "sh", "-c", "cat /sys/fs/cgroup/cpu.stat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak"],
                         capture_output=True, text=True).stdout.split("\n")
    cpu = next(int(l.split()[1]) for l in out if l.startswith("usage_usec"))
    nums = [l for l in out if l.strip().isdigit()]
    return {"cpu_ms": cpu / 1000, "mem_mib": int(nums[0]) / 1048576, "peak_mib": int(nums[1]) / 1048576}


def app_stats():
    return json.loads(urllib.request.urlopen(APP + "/api/stats", timeout=10).read())


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = False
        self.seen = {b: {} for b in BOXES}  # comm -> max concurrent
        self.max_procs = {b: 0 for b in BOXES}

    def run(self):
        while not self.stop:
            for b in BOXES:
                r = subprocess.run(["docker", "top", b, "-eo", "pid,comm"], capture_output=True, text=True).stdout.splitlines()[1:]
                comms = [l.split(None, 1)[1].strip() for l in r if l.strip()]
                self.max_procs[b] = max(self.max_procs[b], len(comms))
                for c in set(comms):
                    self.seen[b][c] = max(self.seen[b].get(c, 0), comms.count(c))
            time.sleep(0.25)


def chat(session, message, quiet=False):
    req = urllib.request.Request(APP + "/api/chat", data=json.dumps({"sessionId": session, "message": message}).encode(),
                                 headers={"content-type": "application/json"})
    done, tools, text = None, [], []
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            ev = json.loads(line)
            if ev["t"] == "text":
                text.append(ev["d"])
                if not quiet:
                    print(ev["d"], end="", flush=True)
            elif ev["t"] == "tool_start":
                tools.append(ev)
                if not quiet:
                    arg = ev["args"].get("command") or ev["args"].get("path") or ""
                    print(f"\n  [tool] {ev['name']}: {arg[:100]}", flush=True)
            elif ev["t"] == "tool_end" and not quiet:
                print(f"  [done] {'error' if ev['isError'] else 'ok'}: {ev['output'][:160].strip()!r}", flush=True)
            elif ev["t"] == "done":
                done = ev
            elif ev["t"] == "error":
                print(f"\n[error] {ev['message']}")
    return done, tools, "".join(text)


def main():
    mode = sys.argv[1]
    if mode == "chat":
        message = sys.argv[2]
        session = sys.argv[sys.argv.index("--session") + 1] if "--session" in sys.argv else f"m-{int(time.time())}"
        before = {b: cgroup(b) for b in BOXES}
        s0 = app_stats()
        sampler = Sampler()
        sampler.start()
        t0 = time.time()
        done, tools, _ = chat(session, message)
        wall = time.time() - t0
        sampler.stop = True
        sampler.join()
        after = {b: cgroup(b) for b in BOXES}
        s1 = app_stats()
        print("\n\n=== result ===")
        print(json.dumps({"session": session, "wall_s": round(wall, 2), "done": done}, indent=1))
        for b in BOXES:
            print(f"{b:13s} CPU {after[b]['cpu_ms'] - before[b]['cpu_ms']:8.1f} ms   memory {before[b]['mem_mib']:.1f} -> {after[b]['mem_mib']:.1f} MiB (peak {after[b]['peak_mib']:.1f})"
                  f"   max processes {sampler.max_procs[b]}   seen {sampler.seen[b]}")
        print("app process:", json.dumps({k: s1[k] for k in ("rssMiB", "heapUsedMiB", "cpuMs", "piLoad", "sessions", "sandboxCalls")}))
        print(f"app process CPU during chat: {s1['cpuMs'] - s0['cpuMs']:.1f} ms, RSS {s0['rssMiB']} -> {s1['rssMiB']} MiB")
    elif mode == "sessions":
        n, message = int(sys.argv[2]), sys.argv[3]
        rows = []
        base = app_stats()
        print(f"before: rss {base['rssMiB']} MiB, sessions {base['sessions']}, piLoaded {base['piLoaded']}")
        for i in range(n):
            c0 = app_stats()["cpuMs"]
            done, _, _ = chat(f"bulk-{int(time.time())}-{i}", message, quiet=True)
            s = app_stats()
            rows.append({"i": i + 1, "rss": s["rssMiB"], "heap": s["heapUsedMiB"], "cpu": round(s["cpuMs"] - c0, 1), "wall": done and done["wallMs"]})
            if (i + 1) in (1, 2, 5, 10, 25, 50, 100, 200) or i + 1 == n:
                print(json.dumps(rows[-1]))
        first, last = rows[0], rows[-1]
        if n > 1:
            print(f"RSS after 1 session {first['rss']} MiB, after {n}: {last['rss']} MiB -> {(last['rss'] - first['rss']) / (n - 1) * 1024:.0f} KiB per extra session")
            cpus = sorted(r["cpu"] for r in rows[1:])
            print(f"server CPU per chat turn (sessions 2..{n}): median {cpus[len(cpus) // 2]} ms")
        print("container:", cgroup("xray-next"))


if __name__ == "__main__":
    main()
