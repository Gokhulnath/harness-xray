"""Shared loaders for agent-lab runs (used by build_db.py, build_explorer.py, facts.py).

A run directory (out/<harness>/runs/<scenario>/) holds:
  proc-events.jsonl  eBPF records  (A X F E D M T S, per-second SY FR FW OP NT BK)
  samples.jsonl      monitor samples (meta, t, slow, mark)
  probe-<pid>.jsonl  in-process probe of Node agents
  mockllm.jsonl      model requests
  events-*.jsonl     harness output streams
  meta.json          scenario definition and step results
"""
import json, os, glob, collections, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "monitor"))
from labclass import Classifier, ROLE_INFO, label as role_label  # noqa: E402,F401


def jl(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return out


def load_meta(rd):
    p = os.path.join(rd, "meta.json")
    return json.load(open(p)) if os.path.exists(p) else {}


# ---------------------------------------------------------------- eBPF processes
EXIT_SUM = ("minflt", "majflt", "nvcsw", "nivcsw", "rqdelay", "slices", "rchar", "wchar", "syscr", "syscw", "rbytes", "wbytes")


def load_procs(rd, agent_match=None):
    """-> procs {pid: {...}}, threads {(pid,tid): {...}}, events list.
    CPU per thread = max(runtime at exit, runtime at final context switch); process = sum over threads.
    Peak RSS = max over threads of the memory map at do_exit (hiwater vs current, pages*4)."""
    ev = jl(os.path.join(rd, "proc-events.jsonl"))
    threads, execs, argv = {}, collections.defaultdict(list), {}
    first_exec, fork_parent = {}, {}
    for e in ev:
        k = e["e"]
        if k == "A":
            argv[e["pid"]] = (e.get("file", ""), " ".join(a for a in (e.get("a1"), e.get("a2"), e.get("a3"), e.get("a4")) if a))
            first_exec.setdefault(e["pid"], e.get("file"))
        elif k == "X":
            execs[e["pid"]].append(e)
        elif k == "F":
            fork_parent.setdefault(e["tid"], e["ppid"])
        elif k in ("E", "D"):
            th = threads.setdefault((e["pid"], e["tid"]), {"rt": 0})
            th["rt"] = max(th["rt"], e.get("rt") or 0)
            if k == "E":
                th.update({kk: e.get(kk) for kk in ("comm", "ppid", "uid", "nspid", "start", "maxrss") + EXIT_SUM})
                th["end"] = e["t"]
        elif k == "M":
            th = threads.setdefault((e["pid"], e["tid"]), {"rt": 0})
            th["mem_kb"] = max(th.get("mem_kb", 0), 4 * max(e.get("hiwater", 0), e.get("rfile", 0) + e.get("ranon", 0) + e.get("rshmem", 0)))
    procs = {}
    for (pid, tid), th in threads.items():
        p = procs.setdefault(pid, {"pid": pid, "cpu_ns": 0, "threads": 0, "thread_cpu": collections.Counter(), "mem_kb": 0, **{k: 0 for k in EXIT_SUM}})
        p["cpu_ns"] += th["rt"]
        p["threads"] += 1
        p["thread_cpu"][th.get("comm") or "?"] += th["rt"]
        p["mem_kb"] = max(p["mem_kb"], th.get("mem_kb", 0))
        for k in EXIT_SUM:
            p[k] += th.get(k) or 0
        if pid == tid:
            for k in ("comm", "ppid", "uid", "nspid", "start"):
                if th.get(k) is not None:
                    p[k] = th[k]
            p["main_exit_seen"] = "comm" in th
        p["end"] = max(p.get("end") or 0, th.get("end") or 0)
    # processes without a main-thread exit record: fill identity from exec (X), fork (F) and other threads
    for (pid, tid), th in threads.items():
        p = procs[pid]
        for k in ("ppid", "uid", "nspid", "start"):
            if p.get(k) is None and th.get(k) is not None and k != "ppid":
                p[k] = th[k]
    for pid, p in procs.items():
        x = execs.get(pid)
        if x:
            p["exe"] = x[-1].get("file")
            if p.get("uid") is None:
                p["uid"] = x[-1].get("uid")
            if not p.get("main_exit_seen"):
                p["comm"] = x[-1].get("comm") or p.get("comm")
                if p.get("ppid") is None:
                    p["ppid"] = x[0].get("ppid")
                if p.get("nspid") is None:
                    p["nspid"] = x[-1].get("nspid")
        if p.get("ppid") is None and pid in fork_parent:
            p["ppid"] = fork_parent[pid]
        if not p.get("comm"):
            p["comm"] = next((th.get("comm") for (pp, tt), th in threads.items() if pp == pid and th.get("comm")), None)
        p["first_exec"] = first_exec.get(pid)
        p["last_exec"] = argv[pid][0] if pid in argv else None
        if pid in argv:
            p["argv"] = (os.path.basename(argv[pid][0]) + " " + argv[pid][1]).strip()
        p["cmd"] = p.get("argv") or p.get("comm")
        p["life_ms"] = round((p.get("end") or 0) - p["start"], 2) if p.get("start") else None
    # a process that never exec'd inherits its parent's command line (forked node helpers, bash subshells)
    roles = Classifier(agent_match).classify({pid: {"pid": pid, "ppid": p.get("ppid"), "comm": p.get("comm"), "uid": p.get("uid"), "cmd": p.get("cmd") if p.get("argv") else _inherit_cmd(procs, pid), "nspid": p.get("nspid"), "first_exec": p.get("first_exec"), "last_exec": p.get("last_exec")} for pid, p in procs.items()})
    for pid, p in procs.items():
        p["role"] = roles.get(pid, "unclassified:" + str(p.get("comm")))
        p["label"] = role_label(p["role"], p.get("comm"))
        q, d = procs.get(p.get("ppid")), 0
        while q and d < 40:
            d += 1
            q = procs.get(q.get("ppid"))
        p["depth"] = d
    return procs, threads, ev


def _inherit_cmd(procs, pid):
    """Command line for a process without its own exec: walk up to the nearest exec'd ancestor
    only for forked children of the agent's own binary (node workers); otherwise just the comm."""
    p = procs[pid]
    return p.get("comm") or ""


# ---------------------------------------------------------------- probes
def load_probes(rd):
    probes = {}
    for f in glob.glob(os.path.join(rd, "probe-*.jsonl")):
        recs = jl(f)
        if not recs:
            continue
        pid = int(f.rsplit("-", 1)[1].split(".")[0])
        d = collections.defaultdict(list)
        for r in recs:
            d[r["k"]].append(r)
        start = d["start"][0] if d["start"] else {}
        probes[pid] = {"pid": pid, "argv": start.get("argv", []), "t0": start.get("t"), "recs": d}
    return probes


def turn_analysis_probe(pr):
    """Per-turn phases from in-process agent event timestamps (pi JSON events) + probe http timings."""
    d = pr["recs"]
    evs = d["ev"]
    https = sorted(d["http"], key=lambda h: h["t"])
    turns, cur, tools = [], None, {}
    for e in evs:
        ty = e["type"]
        if ty == "turn_start":
            cur = {"t0": e["t"], "tools": []}
        elif ty == "turn_end" and cur:
            cur["t1"] = e["t"]
            turns.append(cur)
            cur = None
        elif ty == "tool_execution_start":
            tools[e.get("id")] = {"tool": e.get("tool"), "t0": e["t"]}
        elif ty == "tool_execution_end":
            tt = tools.get(e.get("id"))
            if tt:
                tt["t1"] = e["t"]
                if cur:
                    cur["tools"].append(tt)
    hi = 0
    for t in turns:
        while hi < len(https) and https[hi]["t"] < t["t0"] - 1:
            hi += 1
        if hi < len(https) and https[hi]["t"] <= t.get("t1", 1e30):
            h = https[hi]
            t["http_t0"], t["http_t1"] = h["t"], h["t"] + (h.get("durMs") or 0)
            hi += 1
    rows = []
    for i, t in enumerate(turns):
        row = {"i": i, "t0": t["t0"], "t1": t.get("t1"), "turn_ms": round(t.get("t1", t["t0"]) - t["t0"], 3)}
        if "http_t0" in t:
            row["pre_request_ms"] = round(t["http_t0"] - t["t0"], 3)
            row["model_ms"] = round(t["http_t1"] - t["http_t0"], 3)
            if t["tools"]:
                row["dispatch_ms"] = round(t["tools"][0]["t0"] - t["http_t1"], 3)
                row["tool_ms"] = round(max(x.get("t1", x["t0"]) for x in t["tools"]) - min(x["t0"] for x in t["tools"]), 3)
                row["post_tool_ms"] = round(t["t1"] - max(x.get("t1", x["t0"]) for x in t["tools"]), 3) if t.get("t1") else None
                row["tools"] = [x["tool"] for x in t["tools"]]
        rows.append(row)
    return rows


def turn_analysis_mock(mock_rows):
    """Harness-agnostic turns from the model side: request i occupies [t, tEnd]; the gap until the
    next request of the same session is the harness's own time (tool execution + overhead)."""
    by_sid = collections.defaultdict(list)
    for m in mock_rows:
        if m.get("aux") or m.get("unknown") or m.get("kind") == "count_tokens":
            continue
        by_sid[m.get("sid")].append(m)
    rows = []
    for sid, ms in by_sid.items():
        ms.sort(key=lambda m: m["t"])
        for i, m in enumerate(ms):
            nxt = ms[i + 1] if i + 1 < len(ms) else None
            rows.append({"sid": sid, "i": i, "t": m["t"], "t_end": m.get("tEnd"), "model_ms": round((m.get("tEnd") or m["t"]) - m["t"], 3),
                         "harness_ms": round(nxt["t"] - (m.get("tEnd") or m["t"]), 3) if nxt else None, "req_bytes": m.get("reqBytes"), "n_messages": m.get("nMessages"),
                         "tools": (m.get("reply") or {}).get("toolCalls")})
    return rows
