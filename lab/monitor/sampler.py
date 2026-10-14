#!/usr/bin/env python3
"""Agent-lab monitor: /proc + cgroup + network + VM sampler, eBPF tracer, live dashboard.

Runs in the privileged `monitor` container with the VM's root PID namespace and cgroup tree,
samples only the lab containers (agent + mock), and lives in its own cgroup so its own CPU never
pollutes the agent's numbers. (v1, pi-only: sampler.v1.py)

Files written to the current run directory:
  samples.jsonl      k=meta  once per new pid (cmdline, uid, nspid, container)
                     k=t     every SAMPLE_MS: procs, threads, cgroups, container network, VM
                     k=slow  every SLOW_EVERY_S: memory maps, fd types, socket inodes, TCP tables
                     k=mark  markers from the runner
  proc-events.jsonl  eBPF records (see proctrace.bt): exec/fork/exit with exact CPU, run-queue delay,
                     I/O accounting, peak RSS; TCP state changes; signals; per-second syscalls, file I/O,
                     opens, network bytes, block I/O
  syscalls.json      syscall number -> name for this kernel/arch
Control (HTTP :8765):
  POST /control/run?dir=/lab/out/<harness>/runs/NAME&agent=<regex>&harness=<name>
  POST /control/mark?label=...    POST /control/flush
  GET  /  live dashboard     GET /stream  SSE snapshots     GET /explorer/...  offline explorer
"""
import json, os, sys, time, threading, glob, socket, http.client, subprocess, re, collections
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from labclass import Classifier, label as role_label, ROLE_INFO  # noqa: E402

SAMPLE_MS = int(os.environ.get("SAMPLE_MS", "100"))
SLOW_EVERY = float(os.environ.get("SLOW_EVERY_S", "1.0"))
PORT = int(os.environ.get("PORT", "8765"))
OUT_ROOT = os.environ.get("OUT_ROOT", "/lab/out")
AGENT_CONTAINER = os.environ.get("AGENT_CONTAINER", "lab-agent")
MOCK_CONTAINER = os.environ.get("MOCK_CONTAINER", "lab-mockllm")
LAB = (AGENT_CONTAINER, MOCK_CONTAINER)
EXPLORER_DIR = os.environ.get("EXPLORER_DIR", "/lab/explorer")
HERE = os.path.dirname(os.path.abspath(__file__))
CLK = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024

state_lock = threading.Lock()
run_dir = os.path.join(OUT_ROOT, "idle")
run_info = {"harness": os.environ.get("HARNESS", "pi"), "agent": None}
classifier = Classifier(None)
out_f = None
bpf_f = None
bpf_lock = threading.Lock()
snap_cond = threading.Condition()
latest_snapshot = None
recent_events = []


# ---------------------------------------------------------------- syscall names
def syscall_table():
    names = {}
    for path in ("/usr/include/asm-generic/unistd.h", "/usr/include/aarch64-linux-gnu/asm/unistd.h", "/usr/include/x86_64-linux-gnu/asm/unistd_64.h"):
        try:
            for line in open(path):
                m = re.match(r"#define __NR(?:3264)?_(\w+)\s+(\d+)", line)
                if m:
                    names.setdefault(int(m.group(2)), m.group(1))
        except FileNotFoundError:
            pass
    return names


SYSCALLS = syscall_table()
pid_meta = {}  # pid -> meta (incl. "st" starttime, "skip" for non-lab pids)
exec_hist = {}  # pid -> {"first": path, "last": path}
live_agg = {}  # latest per-second eBPF aggregates for the dashboard


def open_run(d, agent=None, harness=None):
    global run_dir, out_f, bpf_f, classifier
    with state_lock:
        os.makedirs(d, exist_ok=True)
        if out_f:
            out_f.close()
        run_dir = d
        out_f = open(os.path.join(d, "samples.jsonl"), "a", buffering=1 << 16)
        if agent:
            run_info["agent"] = agent
            classifier = Classifier(agent)
        if harness:
            run_info["harness"] = harness
        json.dump({str(k): v for k, v in sorted(SYSCALLS.items())}, open(os.path.join(d, "syscalls.json"), "w"))
    with bpf_lock:
        if bpf_f:
            bpf_f.close()
        bpf_f = open(os.path.join(d, "proc-events.jsonl"), "a", buffering=1 << 16)
    pid_meta.clear()  # re-emit meta for live pids into the new run's file
    live_agg.clear()


def write(rec):
    with state_lock:
        if out_f:
            out_f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def flush():
    with state_lock:
        if out_f:
            out_f.flush()
    with bpf_lock:
        if bpf_f:
            bpf_f.flush()


# ---------------------------------------------------------------- docker / cgroups
def docker_get(path):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect("/var/run/docker.sock")
        c = http.client.HTTPConnection("localhost")
        c.sock = s
        c.request("GET", path)
        return json.loads(c.getresponse().read())
    except Exception:
        return None


container_names = {}


def refresh_containers():
    cs = docker_get("/containers/json")
    if cs is None:
        return
    fresh = {c["Id"]: (c.get("Names") or ["?"])[0].lstrip("/") for c in cs}
    container_names.clear()
    container_names.update(fresh)


def cgroup_of(pid):
    try:
        for line in open(f"/proc/{pid}/cgroup"):
            if line.startswith("0::"):
                return line.strip()[3:]
    except Exception:
        return None


def container_of_cgroup(cg):
    if not cg:
        return None
    for part in re.split(r"[/.-]", cg):
        if len(part) == 64:
            return container_names.get(part) or part[:12]
    return None


CG_ROOT = "/sys/fs/cgroup"


def read_kv(path):
    d = {}
    try:
        for line in open(path):
            p = line.split()
            if len(p) >= 2:
                d[p[0]] = int(p[1])
    except Exception:
        pass
    return d


def read_int(path):
    try:
        return int(open(path).read().strip())
    except Exception:
        return None


def read_psi(path):
    r = {}
    try:
        for line in open(path):
            parts = line.split()
            kv = dict(p.split("=") for p in parts[1:])
            r[parts[0]] = {"avg10": float(kv["avg10"]), "total": int(kv["total"])}
    except Exception:
        pass
    return r


MEMSTAT_KEYS = ("anon", "file", "kernel", "kernel_stack", "pagetables", "sock", "shmem", "file_mapped", "file_dirty", "file_writeback", "slab", "pgfault", "pgmajfault", "workingset_refault_file")


def read_cgroup(cgpath):
    base = CG_ROOT + cgpath
    cpu = read_kv(base + "/cpu.stat")
    ms = read_kv(base + "/memory.stat")
    io = {}
    try:
        for line in open(base + "/io.stat"):
            for kv in line.split()[1:]:
                k, v = kv.split("=")
                io[k] = io.get(k, 0) + int(v)
    except Exception:
        pass
    return {
        "cpu_us": cpu.get("usage_usec"), "user_us": cpu.get("user_usec"), "sys_us": cpu.get("system_usec"), "thr": cpu.get("nr_throttled"),
        "mem": read_int(base + "/memory.current"), "peak": read_int(base + "/memory.peak"),
        "ms": {k: ms.get(k) for k in MEMSTAT_KEYS},
        "io": {k: io.get(k) for k in ("rbytes", "wbytes", "rios", "wios")},
        "pids": read_int(base + "/pids.current"),
        "psi": {"cpu": read_psi(base + "/cpu.pressure"), "mem": read_psi(base + "/memory.pressure"), "io": read_psi(base + "/io.pressure")},
    }


def lab_cgroups():
    res = {}
    for cid, name in container_names.items():
        if name not in LAB and not name.startswith("lab-"):
            continue
        for cand in (f"/docker/{cid}", f"/system.slice/docker-{cid}.scope"):
            if os.path.exists(CG_ROOT + cand):
                res[name] = cand
                break
    return res


# ---------------------------------------------------------------- /proc readers
def stat_fields(path):
    with open(path) as f:
        s = f.read()
    rp = s.rfind(")")
    return s[s.find("(") + 1: rp], s[rp + 2:].split()


def schedstat(path):
    try:
        a = open(path).read().split()
        return int(a[1]), int(a[2])  # run_delay_ns, timeslices
    except Exception:
        return None, None


# proc row: [pid, ppid, state, utime_s, stime_s, rss_kb, threads, vctx, nvctx, rchar, wchar, read_bytes, write_bytes,
#            pss_kb(unused), fds(unused), run_delay_ns, timeslices, last_cpu]  (+ comm, not written)
def read_proc(pid):
    try:
        comm, f = stat_fields(f"/proc/{pid}/stat")
    except Exception:
        return None
    starttime = int(f[19])
    pm = pid_meta.get(pid)
    if pm and pm.get("st") == starttime and pm.get("skip"):
        return None
    if not pm or pm.get("st") != starttime:
        cname = container_of_cgroup(cgroup_of(pid))
        if cname not in LAB:
            pid_meta[pid] = {"st": starttime, "skip": True}
            return None
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
        except Exception:
            cmd = ""
        uid = nspid = None
        try:
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("Uid:"):
                    uid = int(line.split()[2])
                elif line.startswith("NSpid:"):
                    nspid = int(line.split()[-1])
        except Exception:
            pass
        pm = {"k": "meta", "t": time.time() * 1000, "pid": pid, "nspid": nspid, "ppid": int(f[1]), "comm": comm, "cmd": cmd[:600], "uid": uid, "st": starttime, "cg": cname}
        pid_meta[pid] = pm
        write(pm)
    vctx = nvctx = None
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("voluntary_ctxt_switches"):
                    vctx = int(line.split()[1])
                elif line.startswith("nonvoluntary_ctxt_switches"):
                    nvctx = int(line.split()[1])
    except Exception:
        pass
    io = {}
    try:
        with open(f"/proc/{pid}/io") as fh:
            for line in fh:
                k, v = line.split(":")
                io[k] = int(v)
    except Exception:
        pass
    rd, sl = schedstat(f"/proc/{pid}/schedstat")
    return [pid, int(f[1]), f[0], round(int(f[11]) / CLK, 2), round(int(f[12]) / CLK, 2), int(f[21]) * PAGE_KB, int(f[17]),
            vctx, nvctx, io.get("rchar"), io.get("wchar"), io.get("read_bytes"), io.get("write_bytes"), None, None, rd, sl,
            int(f[36]) if len(f) > 36 else None, comm]


# thread row: [pid, tid, name, utime_s, stime_s, state, last_cpu, run_delay_ns, timeslices]
def read_threads(pid):
    rows = []
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except Exception:
        return rows
    for t in tids:
        try:
            comm, f = stat_fields(f"/proc/{pid}/task/{t}/stat")
        except Exception:
            continue
        rd, sl = schedstat(f"/proc/{pid}/task/{t}/schedstat")
        rows.append([pid, int(t), comm, round(int(f[11]) / CLK, 2), round(int(f[12]) / CLK, 2), f[0], int(f[36]) if len(f) > 36 else None, rd, sl])
    return rows


SMAPS_KEYS = ("Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem", "Shared_Clean", "Shared_Dirty", "Private_Clean", "Private_Dirty", "Swap")
STATUS_KEYS = ("VmPeak", "VmSize", "VmHWM", "VmRSS", "RssAnon", "RssFile", "RssShmem", "VmData", "VmStk", "VmExe", "VmLib", "VmPTE")


def read_mem_detail(pid):
    d = {}
    try:
        for line in open(f"/proc/{pid}/smaps_rollup"):
            k = line.split(":")[0]
            if k in SMAPS_KEYS:
                d[k] = int(line.split()[1])
    except Exception:
        pass
    try:
        for line in open(f"/proc/{pid}/status"):
            k = line.split(":")[0]
            if k in STATUS_KEYS:
                d[k] = int(line.split()[1])
    except Exception:
        pass
    return d


def read_fds(pid):
    types = collections.Counter()
    socks = []
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except Exception:
                continue
            if target.startswith("socket:["):
                types["socket"] += 1
                socks.append(int(target[8:-1]))
            elif target.startswith("pipe:["):
                types["pipe"] += 1
            elif target.startswith("anon_inode:"):
                types[target[11:].strip("[]")] += 1
            elif target.startswith("/dev/"):
                types["dev"] += 1
            else:
                types["file"] += 1
    except Exception:
        pass
    return dict(types), socks


TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}


def hexaddr(s):
    ip, port = s.split(":")
    if len(ip) == 8:
        b = bytes.fromhex(ip)[::-1]
        return f"{'.'.join(str(x) for x in b)}:{int(port, 16)}"
    return f"[v6]:{int(port, 16)}"


# tcp row: [local, remote, state, tx_queue, rx_queue, uid, inode]
def read_tcp(pid):
    rows = []
    for fn in ("tcp", "tcp6"):
        try:
            lines = open(f"/proc/{pid}/net/{fn}").read().splitlines()[1:]
        except Exception:
            continue
        for line in lines:
            p = line.split()
            if len(p) < 10:
                continue
            tx, rx = p[4].split(":")
            rows.append([hexaddr(p[1]), hexaddr(p[2]), TCP_STATES.get(p[3], p[3]), int(tx, 16), int(rx, 16), int(p[7]), int(p[9])])
    return rows


def read_net(pid):
    out = {"dev": {}, "tcp": {}, "udp": {}, "sock": {}}
    try:
        for line in open(f"/proc/{pid}/net/dev").read().splitlines()[2:]:
            name, rest = line.split(":", 1)
            v = [int(x) for x in rest.split()]
            out["dev"][name.strip()] = [v[0], v[1], v[2], v[3], v[8], v[9], v[10], v[11]]  # rx bytes/packets/errs/drop, tx bytes/packets/errs/drop
    except Exception:
        pass
    try:
        lines = open(f"/proc/{pid}/net/snmp").read().splitlines()
        for i in range(0, len(lines) - 1, 2):
            hk, hv = lines[i].split(), lines[i + 1].split()
            proto = hk[0].rstrip(":")
            if proto in ("Tcp", "Udp"):
                d = dict(zip(hk[1:], (int(x) for x in hv[1:])))
                keys = ("ActiveOpens", "PassiveOpens", "CurrEstab", "InSegs", "OutSegs", "RetransSegs", "InErrs", "OutRsts", "AttemptFails", "EstabResets") if proto == "Tcp" else ("InDatagrams", "OutDatagrams", "InErrors", "NoPorts", "RcvbufErrors", "SndbufErrors")
                out[proto.lower()] = {k: d.get(k) for k in keys}
    except Exception:
        pass
    try:
        for line in open(f"/proc/{pid}/net/sockstat"):
            p = line.split()
            if p[0] == "sockets:":
                out["sock"]["used"] = int(p[2])
            elif p[0] in ("TCP:", "UDP:"):
                for k, v in zip(p[1::2], p[2::2]):
                    out["sock"][p[0].rstrip(":").lower() + "_" + k] = int(v)
    except Exception:
        pass
    return out


def read_vm():
    v = {}
    try:
        p = open("/proc/stat").readline().split()
        v["cpu"] = [int(x) for x in p[1:9]]  # user nice system idle iowait irq softirq steal (jiffies)
    except Exception:
        pass
    try:
        m = {}
        for line in open("/proc/meminfo"):
            k, rest = line.split(":")
            if k in ("MemTotal", "MemFree", "MemAvailable", "Cached", "Buffers", "Dirty", "Writeback", "AnonPages", "Shmem", "Slab", "SwapTotal", "SwapFree"):
                m[k] = int(rest.split()[0])
        v["mem"] = m
    except Exception:
        pass
    try:
        la = open("/proc/loadavg").read().split()
        v["load"] = [float(la[0]), float(la[1]), float(la[2]), la[3]]
    except Exception:
        pass
    v["psi"] = {k: read_psi(f"/proc/pressure/{k}") for k in ("cpu", "memory", "io")}
    try:
        vs = {}
        for line in open("/proc/vmstat"):
            k, val = line.split()
            if k in ("pgfault", "pgmajfault", "pgpgin", "pgpgout", "pswpin", "pswpout", "nr_dirty", "nr_writeback"):
                vs[k] = int(val)
        v["vmstat"] = vs
    except Exception:
        pass
    return v


# ---------------------------------------------------------------- live snapshot
prev_cpu, prev_thr, prev_cg, prev_net = {}, {}, {}, {}
probe_pos, probe_latest = {}, {}


def tail_new(path, posmap, maxbytes=1 << 20):
    try:
        size = os.path.getsize(path)
    except Exception:
        return []
    pos = posmap.get(path, 0)
    if size < pos:
        pos = 0
    if size == pos:
        return []
    with open(path, "rb") as f:
        f.seek(pos)
        data = f.read(min(size - pos, maxbytes))
    nl = data.rfind(b"\n")
    if nl < 0:
        return []
    posmap[path] = pos + nl + 1
    out = []
    for ln in data[: nl + 1].splitlines():
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def push_event(ev):
    recent_events.append(ev)
    del recent_events[:-400]


def scan_side_files(t):
    for pf in glob.glob(os.path.join(run_dir, "probe-*.jsonl")):
        pid = int(pf.rsplit("-", 1)[1].split(".")[0])
        for r in tail_new(pf, probe_pos):
            k = r.get("k")
            if k == "s":
                probe_latest[pid] = r
            elif k in ("spawn", "exit", "spawnSync", "http", "net", "worker", "start", "end"):
                push_event({"t": r.get("t"), "src": "probe", "pid": pid, "k": k, "d": {kk: r.get(kk) for kk in ("fn", "file", "args", "code", "signal", "durMs", "origin", "path", "status", "host", "port", "maxRssKB", "pid", "ttfb") if kk in r}})
            elif k == "ev" and r.get("type") in ("turn_start", "tool_execution_start", "tool_execution_end", "agent_start", "agent_end", "compaction_start", "compaction_end", "auto_retry_start"):
                push_event({"t": r.get("t"), "src": "agent", "pid": pid, "k": r["type"], "d": {"tool": r.get("tool")}})
            elif k == "gc" and r.get("kind") == "major":
                push_event({"t": r.get("t"), "src": "probe", "pid": pid, "k": "gc", "d": {"dur": r.get("dur")}})
    for pid in list(probe_latest):
        if t * 1000 - (probe_latest[pid].get("t") or 0) > 3000:
            probe_latest.pop(pid, None)


def classify_live(rows):
    procs = {}
    for r in rows:
        m = pid_meta.get(r[0], {})
        if m.get("cg") != AGENT_CONTAINER:
            continue
        eh = exec_hist.get(r[0], {})
        procs[r[0]] = {"pid": r[0], "ppid": r[1], "comm": r[18], "uid": m.get("uid"), "cmd": m.get("cmd"), "nspid": m.get("nspid"), "first_exec": eh.get("first"), "last_exec": eh.get("last")}
    return classifier.classify(procs)


def build_snapshot(t, rows, thr_rows, cgs, nets, vm):
    roles = classify_live(rows)
    out_procs = []
    by_role = collections.Counter()
    for r in rows:
        pid = r[0]
        cpu_s = r[3] + r[4]
        pc = prev_cpu.get(pid)
        pct = 100.0 * (cpu_s - pc[1]) / (t - pc[0]) if pc and t > pc[0] else 0.0
        prev_cpu[pid] = (t, cpu_s)
        m = pid_meta.get(pid, {})
        role = roles.get(pid, "mock" if m.get("cg") == MOCK_CONTAINER else "?")
        if m.get("cg") == AGENT_CONTAINER:
            by_role[role] += pct
        out_procs.append({"pid": pid, "nspid": m.get("nspid"), "ppid": r[1], "st": r[2], "cpu": round(pct, 1), "rss": r[5], "thr": r[6], "uid": m.get("uid"), "cg": m.get("cg"),
                          "cmd": m.get("cmd") or r[18], "comm": r[18], "role": role, "label": role_label(role, r[18]) if role not in ("mock", "?") else "Mock LLM server", "rq_ms": round((r[15] or 0) / 1e6, 1)})
    for pid in list(prev_cpu):
        if t - prev_cpu[pid][0] > 5:
            prev_cpu.pop(pid)
    threads = []
    for tr in thr_rows:
        key = (tr[0], tr[1])
        cpu_s = tr[3] + tr[4]
        pt = prev_thr.get(key)
        pct = 100.0 * (cpu_s - pt[1]) / (t - pt[0]) if pt and t > pt[0] else 0.0
        rqd = (tr[7] - pt[2]) / 1e6 / (t - pt[0]) if pt and tr[7] is not None and pt[2] is not None and t > pt[0] else 0.0
        prev_thr[key] = (t, cpu_s, tr[7])
        if roles.get(tr[0]) in ("agent", "agent:nested"):
            threads.append({"pid": tr[0], "tid": tr[1], "name": tr[2], "cpu": round(pct, 1), "st": tr[5], "core": tr[6], "rq_ms_per_s": round(rqd, 1)})
    for k in list(prev_thr):
        if t - prev_thr[k][0] > 5:
            prev_thr.pop(k)
    cgsnap = {}
    for name, c in cgs.items():
        p = prev_cg.get(name)
        pct = (c["cpu_us"] - p[1]) / 1e4 / (t - p[0]) if p and c.get("cpu_us") is not None and t > p[0] else None
        if c.get("cpu_us") is not None:
            prev_cg[name] = (t, c["cpu_us"])
        ms = c.get("ms") or {}
        cgsnap[name] = {"cpu": round(pct, 1) if pct is not None else None, "mem": c.get("mem"), "peak": c.get("peak"), "pids": c.get("pids"), "anon": ms.get("anon"), "file": ms.get("file"),
                        "psi_cpu": ((c.get("psi") or {}).get("cpu") or {}).get("some", {}).get("avg10"), "io_w": (c.get("io") or {}).get("wbytes")}
    netsnap = {}
    for name, n in nets.items():
        rx = sum(v[0] for k, v in n["dev"].items() if k != "lo")
        tx = sum(v[4] for k, v in n["dev"].items() if k != "lo")
        lo = n["dev"].get("lo", [0] * 8)[0]
        p = prev_net.get(name)
        if p and t > p[0]:
            netsnap[name] = {"rx_Bps": (rx - p[1]) / (t - p[0]), "tx_Bps": (tx - p[2]) / (t - p[0]), "lo_Bps": (lo - p[3]) / (t - p[0]), "estab": (n.get("tcp") or {}).get("CurrEstab"), "sockets": (n.get("sock") or {}).get("used")}
        prev_net[name] = (t, rx, tx, lo)
    probes = {str(pid): {k: r.get(k) for k in ("elu", "lag99", "lagMax", "rss", "heapUsed", "heapTotal", "ext", "u", "sy", "dt", "thr", "h")} for pid, r in probe_latest.items()}
    return {"t": t * 1000, "run": os.path.basename(run_dir), "harness": run_info.get("harness"), "agent_container": AGENT_CONTAINER, "procs": out_procs, "roles": {k: round(v, 1) for k, v in by_role.items()},
            "role_info": ROLE_INFO, "threads": threads, "cg": cgsnap, "net": netsnap, "vm": {"load": vm.get("load"), "psi_cpu": ((vm.get("psi") or {}).get("cpu") or {}).get("some", {}).get("avg10")},
            "probes": probes, "agg": live_agg, "events": recent_events[-120:]}


def sampler_loop():
    global latest_snapshot
    last_slow = last_refresh = 0
    period = SAMPLE_MS / 1000.0
    next_t = time.time()
    while True:
        t = time.time()
        if t - last_refresh > 2:
            refresh_containers()
            last_refresh = t
        slow = t - last_slow >= SLOW_EVERY
        if slow:
            last_slow = t
        rows, thr_rows = [], []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            r = read_proc(int(name))
            if r is None:
                continue
            rows.append(r)
            if r[6] > 1:
                thr_rows.extend(read_threads(r[0]))
        cgs = {name: read_cgroup(path) for name, path in lab_cgroups().items()}
        nets, rep = {}, {}
        for r in rows:  # any process of a container sees that container's network namespace
            cg = pid_meta.get(r[0], {}).get("cg")
            if cg and cg not in rep:
                rep[cg] = r[0]
        for cg, pid in rep.items():
            nets[cg] = read_net(pid)
        vm = read_vm()
        write({"k": "t", "t": round(t * 1000, 1), "procs": [r[:18] for r in rows], "thr": thr_rows, "cg": cgs, "net": nets, "vm": vm})
        if slow:
            mem, fdt, socks = {}, {}, {}
            for r in rows:
                if r[5] > 2048:
                    mem[r[0]] = read_mem_detail(r[0])
                types, inodes = read_fds(r[0])
                fdt[r[0]] = types
                if inodes:
                    socks[r[0]] = inodes
            write({"k": "slow", "t": round(t * 1000, 1), "mem": mem, "fd": fdt, "sock_inodes": socks, "tcp": {cg: read_tcp(pid) for cg, pid in rep.items()}})
        try:
            scan_side_files(t)
            snap = build_snapshot(t, rows, thr_rows, cgs, nets, vm)
            with snap_cond:
                latest_snapshot = snap
                snap_cond.notify_all()
        except Exception as e:
            print("snapshot error", repr(e), file=sys.stderr)
        if slow:
            flush()
            for pid in list(pid_meta):
                if not os.path.exists(f"/proc/{pid}"):
                    pid_meta.pop(pid, None)
                    exec_hist.pop(pid, None)
        next_t += period
        delay = next_t - time.time()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.time()


# ---------------------------------------------------------------- eBPF
BOOT_OFFSET_NS = time.time_ns() - time.clock_gettime_ns(time.CLOCK_BOOTTIME)
BPF_FIELDS = {
    "A": ("ts", "pid", "tid", "comm", "file", "a1", "a2", "a3", "a4"),
    "X": ("ts", "pid", "tid", "ppid", "uid", "nspid", "comm", "file"),
    "V": ("ts", "pid", "comm", "argv"),
    "F": ("ts", "ppid", "tid"),
    "E": ("ts", "pid", "tid", "ppid", "uid", "nspid", "comm", "rt", "ut", "st", "maxrss", "start", "minflt", "majflt", "nvcsw", "nivcsw", "rqdelay", "slices", "rchar", "wchar", "syscr", "syscw", "rbytes", "wbytes"),
    "D": ("ts", "pid", "tid", "rt"),
    "M": ("ts", "pid", "tid", "hiwater", "rfile", "ranon", "rshmem"),
    "T": ("ts", "pid", "comm", "old", "new", "saddr", "sport", "daddr", "dport"),
    "S": ("ts", "pid", "comm", "tpid", "tcomm", "sig", "res"),
    "SY": ("pid", "comm", "nr", "n", "ns"),
    "FR": ("pid", "comm", "ftype", "dir", "name", "bytes", "ops"),
    "FW": ("pid", "comm", "ftype", "dir", "name", "bytes", "ops"),
    "OP": ("pid", "comm", "file", "n"),
    "NT": ("pid", "comm", "kind", "bytes"),
    "BK": ("comm", "rwbs", "bytes"),
    "TICK": ("ts",),
}
BPF_STR = {"comm", "file", "a1", "a2", "a3", "a4", "saddr", "daddr", "tcomm", "dir", "name", "kind", "rwbs", "argv"}
TCP_ST = {1: "ESTABLISHED", 2: "SYN_SENT", 3: "SYN_RECV", 4: "FIN_WAIT1", 5: "FIN_WAIT2", 6: "TIME_WAIT", 7: "CLOSE", 8: "CLOSE_WAIT", 9: "LAST_ACK", 10: "LISTEN", 11: "CLOSING"}
pending_agg = collections.defaultdict(list)
bpf_state = {"cid": None, "ready": False, "started": None, "last_tick_t": 0}


def bpf_parse(line):
    parts = line.rstrip("\n").split("\t")
    names = BPF_FIELDS.get(parts[0])
    if not names:
        return None
    rec = {"e": parts[0]}
    for name, val in zip(names, parts[1:]):
        if name in BPF_STR:
            rec[name] = val
        else:
            try:
                rec[name] = int(val)
            except ValueError:
                rec[name] = val
    if "ts" in rec:
        rec["t"] = round((rec["ts"] + BOOT_OFFSET_NS) / 1e6, 3)
    if isinstance(rec.get("start"), int):
        rec["start"] = round((rec["start"] + BOOT_OFFSET_NS) / 1e6, 3)
    if rec["e"] == "V":
        # bpftrace %r escapes non-printables as \xNN; argv entries are NUL-separated
        raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), rec.get("argv", ""))
        rec["argv"] = [a for a in raw.split("\x00") if a != ""]
    return rec


def summarize_agg(p):
    sy = collections.Counter()
    for r in p.get("SY", []):
        sy[(r["comm"], SYSCALLS.get(r["nr"], str(r["nr"])))] += r["n"]
    files = collections.Counter()
    for kind in ("FR", "FW"):
        for r in p.get(kind, []):
            files[(kind, r["comm"], r["ftype"], (r.get("dir", "") + "/" + r.get("name", "")).strip("/"))] += r["bytes"]
    net = collections.Counter()
    for r in p.get("NT", []):
        net[(r["comm"], r["kind"])] += r["bytes"]
    live_agg["syscalls"] = [[c, n, v] for (c, n), v in sy.most_common(25)]
    live_agg["files"] = [[k, c, ft, name, v] for (k, c, ft, name), v in files.most_common(20)]
    live_agg["net"] = [[c, k, v] for (c, k), v in net.most_common(15)]
    live_agg["opens"] = sum(r["n"] for r in p.get("OP", []))
    live_agg["blk"] = [[r["comm"], r["rwbs"], r["bytes"]] for r in p.get("BK", [])][:10]


def bpf_handle(line):
    if line.startswith("Attaching") or not line.strip() or line.startswith("@"):
        return
    rec = bpf_parse(line)
    if rec is None:
        print("bpftrace:", line.rstrip()[:200], flush=True)
        return
    k = rec["e"]
    if k in ("SY", "FR", "FW", "OP", "NT", "BK"):
        pending_agg[k].append(rec)
        return
    if k == "TICK":
        bpf_state["ready"] = True
        bpf_state["last_tick_t"] = rec["t"]
        t = rec["t"]  # aggregates printed just before this tick cover the preceding second
        with bpf_lock:
            if bpf_f:
                for recs in pending_agg.values():
                    for r in recs:
                        r["t"] = t
                        bpf_f.write(json.dumps(r, separators=(",", ":")) + "\n")
        summarize_agg(pending_agg)
        pending_agg.clear()
        return
    with bpf_lock:
        if bpf_f:
            bpf_f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    if k == "A":
        eh = exec_hist.setdefault(rec["pid"], {})
        eh.setdefault("first", rec.get("file"))
        eh["last"] = rec.get("file")
        args = " ".join(a for a in (rec.get("a1"), rec.get("a2"), rec.get("a3"), rec.get("a4")) if a)
        if not rec.get("comm", "").startswith("runc"):
            push_event({"t": rec["t"], "src": "bpf", "k": "exec", "d": {"pid": rec["pid"], "cmd": (os.path.basename(rec.get("file", "")) + " " + args)[:140], "by": rec.get("comm")}})
    elif k == "E" and rec.get("pid") == rec.get("tid") and not rec.get("comm", "").startswith("runc"):
        push_event({"t": rec["t"], "src": "bpf", "k": "pexit", "d": {"pid": rec["pid"], "comm": rec.get("comm"), "cpu_ms": round(rec.get("rt", 0) / 1e6, 2), "rq_ms": round(rec.get("rqdelay", 0) / 1e6, 2), "uid": rec.get("uid"), "life_ms": round(rec["t"] - rec.get("start", rec["t"]), 1), "wchar": rec.get("wchar"), "rchar": rec.get("rchar")}})
    elif k == "T":
        push_event({"t": rec["t"], "src": "bpf", "k": "tcp", "d": {"pid": rec["pid"], "comm": rec.get("comm"), "from": TCP_ST.get(rec.get("old"), rec.get("old")), "to": TCP_ST.get(rec.get("new"), rec.get("new")), "local": f"{rec.get('saddr')}:{rec.get('sport')}", "remote": f"{rec.get('daddr')}:{rec.get('dport')}"}})
    elif k == "S" and rec.get("sig") in (1, 2, 6, 9, 11, 15):
        push_event({"t": rec["t"], "src": "bpf", "k": "signal", "d": {"from": f"{rec.get('comm')}({rec.get('pid')})", "to": f"{rec.get('tcomm')}({rec.get('tpid')})", "sig": rec.get("sig"), "res": rec.get("res")}})


def bpf_manager():
    global BOOT_OFFSET_NS
    subprocess.run("mountpoint -q /sys/kernel/tracing || mount -t tracefs nodev /sys/kernel/tracing", shell=True)
    cur_cid, proc, reader = None, None, None
    while True:
        if proc and reader and not reader.is_alive() and proc.poll() is None:
            print("bpf reader died; restarting bpftrace", flush=True)
            proc.kill()
        refresh_containers()
        cid = next((k for k, v in container_names.items() if v == AGENT_CONTAINER), None)
        if cid and (cid != cur_cid or proc is None or proc.poll() is not None):
            if proc and proc.poll() is None:
                proc.terminate()
            cg = f"/sys/fs/cgroup/docker/{cid}"
            if not os.path.exists(cg):
                cg = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
            script = open(os.path.join(HERE, "proctrace.bt")).read().replace("CG", cg)
            open("/tmp/proctrace.bt", "w").write(script)
            BOOT_OFFSET_NS = time.time_ns() - time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            # big maps (32 agents x threads x syscalls per second), big per-CPU ring buffers, no per-event
            # warnings, and output to a file (never blocks, so bpftrace always drains its ring buffers
            # even when this process is busy); a reader thread tails the file.
            env = dict(os.environ, BPFTRACE_MAX_MAP_KEYS="131072", BPFTRACE_PERF_RB_PAGES="2048", BPFTRACE_MAX_STRLEN="200")
            outpath = f"/tmp/bpf-{int(time.time())}.out"
            # stdout redirected to a file (not -o, which ignores -B line and block-buffers): every line is
            # flushed immediately and writes never block on this reader
            proc = subprocess.Popen(["bpftrace", "--no-warnings", "-B", "line", "/tmp/proctrace.bt"], stdout=open(outpath, "wb"), stderr=open("/tmp/bpftrace.stderr", "a"), env=env)
            cur_cid = cid
            bpf_state.update(cid=cid, ready=False, started=time.time())
            reader = threading.Thread(target=bpf_reader, args=(proc, outpath), daemon=True)
            reader.start()
            print(f"bpftrace started for {AGENT_CONTAINER} {cid[:12]}", flush=True)
        time.sleep(0.5)


def bpf_reader(proc, path):
    # tail bpftrace's output file; binary-safe (file names can contain arbitrary bytes)
    while not os.path.exists(path) and proc.poll() is None:
        time.sleep(0.05)
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        buf = b""
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
                continue
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                try:
                    line = raw.decode("utf-8", errors="replace").replace("\ufffd", "?")
                    if line.startswith("Lost "):
                        print("bpftrace:", line, flush=True)
                    bpf_handle(line + "\n")
                except Exception as e:
                    print("bpf_reader error:", repr(e), raw[:200], flush=True)
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- HTTP
CT = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".md": "text/markdown; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/control/run":
            d = q.get("dir", [os.path.join(OUT_ROOT, "idle")])[0]
            open_run(d, q.get("agent", [None])[0], q.get("harness", [None])[0])
            write({"k": "mark", "t": time.time() * 1000, "label": "run-start " + os.path.basename(d)})
            recent_events.clear()
            self._json({"ok": True, "dir": d, "harness": run_info.get("harness")})
        elif u.path == "/control/mark":
            label = q.get("label", [""])[0]
            write({"k": "mark", "t": time.time() * 1000, "label": label})
            push_event({"t": time.time() * 1000, "src": "mark", "k": "mark", "d": {"label": label}})
            self._json({"ok": True})
        elif u.path == "/control/drop_caches":
            os.sync()
            open("/proc/sys/vm/drop_caches", "w").write("3\n")
            self._json({"ok": True, "dropped": True})
        elif u.path == "/control/sync":
            # block until the eBPF reader has processed a tick newer than `after` (epoch ms), so a run's
            # last per-second aggregates land in that run's files before the runner switches away
            after = float(q.get("after", [time.time() * 1000])[0])
            deadline = time.time() + float(q.get("timeout", ["15"])[0])
            while bpf_state["last_tick_t"] <= after and time.time() < deadline:
                time.sleep(0.1)
            flush()
            self._json({"ok": bpf_state["last_tick_t"] > after, "last_tick_t": bpf_state["last_tick_t"]})
        elif u.path == "/control/flush":
            flush()
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _file(self, path):
        try:
            b = open(path, "rb").read()
        except Exception:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("content-type", CT.get(os.path.splitext(path)[1], "application/octet-stream"))
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._file(os.path.join(HERE, "dashboard.html"))
        if u.path == "/explorer":
            self.send_response(301)
            self.send_header("location", "/explorer/")
            self.end_headers()
            return
        if u.path.startswith("/explorer/"):
            rel = u.path[len("/explorer/"):] or "index.html"
            full = os.path.normpath(os.path.join(EXPLORER_DIR, rel))
            if not full.startswith(os.path.normpath(EXPLORER_DIR)):
                return self.send_error(403)
            return self._file(full)
        if u.path == "/stream":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            every = float(parse_qs(u.query).get("every", ["0.25"])[0])
            last = 0
            try:
                while True:
                    with snap_cond:
                        snap_cond.wait(timeout=2)
                        snap = latest_snapshot
                    if snap is None or time.time() - last < every:
                        continue
                    last = time.time()
                    self.wfile.write(b"data: " + json.dumps(snap, separators=(",", ":")).encode() + b"\n\n")
                    self.wfile.flush()
            except Exception:
                return
        if u.path == "/health":
            return self._json({"ok": True, "run": run_dir, "harness": run_info.get("harness"), "agent_container": AGENT_CONTAINER, "syscalls_known": len(SYSCALLS),
                               "bpf_cid": bpf_state["cid"], "bpf_ready": bpf_state["ready"]})
        self.send_error(404)


if __name__ == "__main__":
    open_run(run_dir)
    threading.Thread(target=sampler_loop, daemon=True).start()
    if os.environ.get("BPF", "1") == "1":
        threading.Thread(target=bpf_manager, daemon=True).start()
    print(f"sampler {SAMPLE_MS}ms, agent container {AGENT_CONTAINER}, {len(SYSCALLS)} syscall names, dashboard http://0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
