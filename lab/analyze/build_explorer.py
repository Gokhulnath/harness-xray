#!/usr/bin/env python3
"""Build the explorer's data bundle from out/agentlab.sqlite.

  python3 analyze/build_explorer.py
writes lab/explorer/data/index.json and data/<harness>/<run>.json, plus data/docs/*.md.
Times inside a run bundle are seconds relative to `base` (epoch ms).
"""
import json, os, sqlite3, collections, statistics, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lablib import ROLE_INFO  # noqa: E402

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(LAB)
DB = os.path.join(LAB, "out", "agentlab.sqlite")
OUT = os.path.join(LAB, "explorer", "data")
AGENT_CONTAINER = "lab-agent"

GROUPS = [
    ("Baseline & startup", ["00-baseline", "01-startup", "02-idle-tui", "23-probe-overhead", "17-strace"]),
    ("Agent loop & memory", ["04-loop-200", "05-loop-mixed-300", "12-compaction", "14-rpc-session", "18-cache-warmer"]),
    ("Tools & processes", ["03-each-tool", "06-parallel-tools", "07-big-output", "08-cpu-child", "09-timeout-orphans", "16-fd-download"]),
    ("sudo & security", ["10-sudo", "22-env-leak"]),
    ("Streaming & TUI", ["11-stream-modes", "21-tui-scaling"]),
    ("Scale & resilience", ["13-concurrency", "20-lock-contention", "15-subagents", "19-retries"]),
    ("Real model (OpenRouter)", ["R1-fix-tests-json", "R2-sudo-task-json", "R3-explore-tui"]),
]
SIG = {1: "SIGHUP", 2: "SIGINT", 6: "SIGABRT", 9: "SIGKILL", 11: "SIGSEGV", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM", 17: "SIGCHLD", 19: "SIGSTOP", 23: "SIGURG", 28: "SIGWINCH"}


def r(x, n=3):
    return None if x is None else round(x, n)


class Q:
    def __init__(self, con, h, run):
        self.con, self.h, self.run = con, h, run

    def __call__(self, sql, *args):
        return self.con.execute(sql, (self.h, self.run) + args).fetchall()


def deltas(rows, cols):
    """rows: [(t, c1, c2, ...)] cumulative counters -> per-second rates between samples."""
    out = []
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        dt = (b[0] - a[0]) / 1000
        if dt <= 0:
            continue
        out.append([b[0]] + [((b[j] or 0) - (a[j] or 0)) / dt if cols[j - 1] else b[j] for j in range(1, len(b))])
    return out


def run_bundle(con, h, run):
    q = Q(con, h, run)
    desc, t0, t1, wall, agent_match, disk, leftover, hdisp, container = q("SELECT description, t0, t1, wall_s, agent_match, disk_usage, leftover_processes, harness_display, container_json FROM runs WHERE harness=? AND run=?")[0]
    base = (t0 or 0) - 2000
    rel = lambda t: None if t is None else round((t - base) / 1000, 4)
    B = {"harness": h, "harness_display": hdisp, "run": run, "desc": desc, "base": base, "t0": rel(t0), "t1": rel(t1), "wall_s": wall, "agent_match": agent_match,
         "disk_usage": disk, "leftover": leftover, "role_info": ROLE_INFO, "container": json.loads(container) if container else None}
    B["steps"] = []
    for idx, kind, tag, mode, w, rc, d in q("SELECT idx, kind, tag, mode, wall_s, rc, detail_json FROM steps WHERE harness=? AND run=? ORDER BY idx"):
        det = json.loads(d)
        if kind == "parallel":
            det["runs"] = [{k: x.get(k) for k in ("tag", "rc", "wall_s")} for x in det.get("runs", []) if x]
        B["steps"].append({"idx": idx, "kind": kind, "tag": tag, "mode": mode, "wall_s": w, "rc": rc, "detail": det})

    # ---- processes (eBPF)
    P = q("""SELECT pid, nspid, ppid, uid, comm, argv, role, label, start_ms, end_ms, cpu_ms, peak_rss_kb, threads, depth, runq_wait_ms, rchar, wchar, read_bytes, write_bytes,
             minflt, majflt, nvcsw, nivcsw, first_exec, exe, timeslices FROM procs WHERE harness=? AND run=? ORDER BY start_ms""")
    B["proc_cols"] = ["pid", "nspid", "ppid", "uid", "comm", "argv", "role", "label", "start", "end", "cpu_ms", "rss_kb", "threads", "depth", "runq_ms", "rchar", "wchar", "read_bytes", "write_bytes", "minflt", "majflt", "nvcsw", "nivcsw", "first_exec", "exe", "slices"]
    B["procs"] = [[p[0], p[1], p[2], p[3], p[4], (p[5] or "")[:200], p[6], p[7], rel(p[8]), rel(p[9]), r(p[10]), p[11], p[12], p[13], r(p[14]), p[15], p[16], p[17], p[18], p[19], p[20], p[21], p[22], p[23], p[24], p[25]] for p in P]
    th = collections.defaultdict(list)
    for pid, tid, comm, cpu, rq in q("SELECT pid, tid, comm, cpu_ms, runq_wait_ms FROM threads WHERE harness=? AND run=?"):
        th[pid].append([tid, comm, r(cpu), r(rq)])
    B["threads"] = {str(k): sorted(v, key=lambda x: -(x[2] or 0)) for k, v in th.items() if len(v) > 1}
    B["roles"] = [[role, cpu, rq, n, r(mx, 1), rc, wc] for role, cpu, rq, n, mx, rc, wc in q("SELECT role, cpu_ms, runq_wait_ms, n, max_rss_mb, rchar, wchar FROM v_cpu_by_role WHERE harness=? AND run=? ORDER BY cpu_ms DESC")]
    B["commands"] = [list(x) for x in q("SELECT role, comm, n, cpu_ms, ROUND(max_rss_mb,1) FROM v_cpu_by_command WHERE harness=? AND run=? ORDER BY cpu_ms DESC LIMIT 60")]
    B["agent_threads"] = [list(x) for x in q("SELECT thread, n, cpu_ms, runq_wait_ms FROM v_thread_cpu WHERE harness=? AND run=? AND role IN ('agent','agent:nested') ORDER BY cpu_ms DESC")]

    # ---- container / VM / network series
    def cg_series(cname):
        rows = q("SELECT t, cpu_us, mem, anon, file, pids, io_wbytes, psi_cpu_some_us, kernel, sock, file_dirty FROM cgroup_samples WHERE harness=? AND run=? AND container=? ORDER BY t", cname)
        out = []
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            dt = (b[0] - a[0]) / 1000
            if dt <= 0:
                continue
            out.append([rel(b[0]), r(((b[1] or 0) - (a[1] or 0)) / 1e4 / dt, 1), r((b[2] or 0) / 1048576, 1), r((b[3] or 0) / 1048576, 1), r((b[4] or 0) / 1048576, 1), b[5],
                        r(((b[6] or 0) - (a[6] or 0)) / 1048576 / dt, 3), r(((b[7] or 0) - (a[7] or 0)) / 1e4 / dt, 1), r((b[8] or 0) / 1048576, 1), r((b[9] or 0) / 1048576, 2), r((b[10] or 0) / 1048576, 1)])
        return out
    B["cg"] = {AGENT_CONTAINER: cg_series(AGENT_CONTAINER), "lab-mockllm": cg_series("lab-mockllm")}
    B["cg_cols"] = ["t", "cpu_pct", "mem_mb", "anon_mb", "file_mb", "pids", "io_write_mb_s", "psi_cpu_pct", "kernel_mb", "sock_mb", "dirty_mb"]
    net = {}
    for cname in (AGENT_CONTAINER, "lab-mockllm"):
        rows = q("SELECT t, SUM(CASE WHEN iface!='lo' THEN rx_bytes ELSE 0 END), SUM(CASE WHEN iface!='lo' THEN tx_bytes ELSE 0 END), SUM(CASE WHEN iface='lo' THEN rx_bytes ELSE 0 END), SUM(rx_packets), SUM(tx_packets) FROM net_dev WHERE harness=? AND run=? AND container=? GROUP BY t ORDER BY t", cname)
        snmp = {t: (e, ao, ins, outs, rt) for t, e, ao, ins, outs, rt in q("SELECT t, tcp_curr_estab, tcp_active_opens, tcp_in_segs, tcp_out_segs, tcp_retrans FROM net_snmp WHERE harness=? AND run=? AND container=?", cname)}
        ser = []
        for i in range(1, len(rows)):
            a, b = rows[i - 1], rows[i]
            dt = (b[0] - a[0]) / 1000
            if dt <= 0:
                continue
            s = snmp.get(b[0], (None,) * 5)
            ser.append([rel(b[0]), r(((b[1] or 0) - (a[1] or 0)) / dt, 1), r(((b[2] or 0) - (a[2] or 0)) / dt, 1), r(((b[3] or 0) - (a[3] or 0)) / dt, 1), s[0], s[1]])
        net[cname] = ser
        first = rows[0] if rows else None
        last = rows[-1] if rows else None
        net[cname + "_total"] = {"rx": (last[1] - first[1]) if rows else 0, "tx": (last[2] - first[2]) if rows else 0, "lo": (last[3] - first[3]) if rows else 0}
    B["net"] = net
    B["net_cols"] = ["t", "rx_Bps", "tx_Bps", "lo_Bps", "tcp_estab", "tcp_active_opens_total"]
    vm = q("SELECT t, cpu_user, cpu_nice, cpu_system, cpu_idle, cpu_iowait, cpu_irq, cpu_softirq, cpu_steal, mem_available_kb, load1, psi_cpu_some_us FROM vm_samples WHERE harness=? AND run=? ORDER BY t")
    vser = []
    for i in range(1, len(vm)):
        a, b = vm[i - 1], vm[i]
        tot = sum((b[j] or 0) - (a[j] or 0) for j in range(1, 9))
        idle = (b[4] or 0) - (a[4] or 0) + (b[5] or 0) - (a[5] or 0)
        dt = (b[0] - a[0]) / 1000
        vser.append([rel(b[0]), r(100 * (tot - idle) / tot, 1) if tot else 0, r((b[9] or 0) / 1024, 0), b[10], r(((b[11] or 0) - (a[11] or 0)) / 1e4 / dt, 1) if dt > 0 else 0])
    B["vm"] = vser
    B["vm_cols"] = ["t", "vm_cpu_busy_pct", "mem_available_mb", "load1", "vm_psi_cpu_pct"]

    # ---- per-thread CPU series of the main agent processes (from 100 ms /proc task samples)
    agents = [p for p in P if p[6] in ("agent", "agent:nested")]
    agents_top = sorted(agents, key=lambda p: -(p[10] or 0))[:3]
    tseries = []
    for p in agents_top:
        rows = q("SELECT t, tid, name, utime_s+stime_s, runq_wait_ns FROM thread_samples WHERE harness=? AND run=? AND pid=? ORDER BY t", p[0])
        prev = {}
        names = []
        frames = collections.OrderedDict()
        for t, tid, name, cpu, rq in rows:
            key = tid
            if key in prev:
                pt, pc, prq = prev[key]
                dt = (t - pt) / 1000
                if dt > 0:
                    f = frames.setdefault(t, {})
                    f[name] = f.get(name, 0) + (cpu - pc) / dt * 100
                    f["_rq"] = f.get("_rq", 0) + ((rq or 0) - (prq or 0)) / 1e6 / dt
            prev[key] = (t, cpu, rq)
            if name not in names:
                names.append(name)
        tseries.append({"pid": p[0], "nspid": p[1], "names": names, "rows": [[rel(t)] + [r(f.get(n, 0), 1) for n in names] + [r(f.get("_rq", 0), 1)] for t, f in frames.items()]})
    B["thread_series"] = tseries

    # ---- syscalls / files / opens / net (eBPF aggregates)
    B["syscalls"] = [list(x) for x in q("SELECT thread, name, count, total_ms FROM v_syscall_totals WHERE harness=? AND run=? ORDER BY count DESC LIMIT 80")]
    roles_by_pid = {p[0]: p[6] for p in P}
    sc_role = collections.Counter()
    sc_time = collections.defaultdict(collections.Counter)
    for t, pid, name, n in q("SELECT t, pid, name, count FROM syscalls WHERE harness=? AND run=?"):
        sc_role[(roles_by_pid.get(pid, "?"), name)] += n
        sc_time[rel(t)][name] += n
    B["syscalls_by_role"] = [[ro, nm, n] for (ro, nm), n in sc_role.most_common(80)]
    top_names = [nm for nm, _ in sum(sc_time.values(), collections.Counter()).most_common(8)]
    B["syscall_series"] = {"names": top_names, "rows": [[t] + [c.get(n, 0) for n in top_names] + [sum(c.values())] for t, c in sorted(sc_time.items()) if t is not None]}
    B["files"] = [list(x) for x in q("SELECT op, comm, ftype, dir, name, bytes, ops FROM v_file_totals WHERE harness=? AND run=? ORDER BY bytes DESC LIMIT 120")]
    fio = collections.defaultdict(lambda: collections.Counter())
    for t, op, ftype, b in q("SELECT t, op, ftype, SUM(bytes) FROM file_io WHERE harness=? AND run=? GROUP BY t, op, ftype"):
        fio[rel(t)][f"{ftype}_{op}"] += b
    kinds = sorted({k for c in fio.values() for k in c})
    B["file_series"] = {"kinds": kinds, "rows": [[t] + [c.get(k, 0) for k in kinds] for t, c in sorted(fio.items()) if t is not None]}
    B["opens"] = [list(x) for x in q("SELECT comm, file, SUM(count) c FROM opens WHERE harness=? AND run=? GROUP BY comm, file ORDER BY c DESC LIMIT 120")]
    B["opens_total"] = q("SELECT COALESCE(SUM(count),0) FROM opens WHERE harness=? AND run=?")[0][0]
    B["net_procs"] = [list(x) for x in q("SELECT comm, kind, bytes FROM v_net_totals WHERE harness=? AND run=? ORDER BY bytes DESC LIMIT 60")]
    B["tcp_events"] = [[rel(t), pid, comm, o, n, lo, re] for t, pid, comm, o, n, lo, re in q("SELECT t, pid, comm, old_state, new_state, local, remote FROM tcp_events WHERE harness=? AND run=? ORDER BY t")]
    B["tcp_conns"] = [list(x) for x in q("SELECT container, local, remote, state, MAX(tx_queue), MAX(rx_queue), owner_pid, COUNT(*) FROM tcp_conns WHERE harness=? AND run=? GROUP BY container, local, remote, state ORDER BY 8 DESC LIMIT 80")]
    sigs = q("SELECT t, sender_pid, sender_comm, target_pid, target_comm, sig, result FROM signals WHERE harness=? AND run=? ORDER BY t")
    B["signals"] = [[rel(t), sp, sc, tp, tc, SIG.get(s, str(s)), res] for t, sp, sc, tp, tc, s, res in sigs if s != 17][:500]
    B["signal_counts"] = [[SIG.get(s, str(s)), n] for s, n in collections.Counter(x[5] for x in sigs).most_common()]
    B["block_io"] = [list(x) for x in q("SELECT comm, rwbs, SUM(bytes) FROM block_io WHERE harness=? AND run=? GROUP BY comm, rwbs ORDER BY 3 DESC LIMIT 30")]
    B["mem_detail"] = {}
    for p in agents_top:
        rows = q("SELECT t, rss_kb, pss_kb, pss_anon_kb, pss_file_kb, pss_shmem_kb, private_dirty_kb, vm_size_kb, vm_data_kb, vm_pte_kb FROM mem_detail WHERE harness=? AND run=? AND pid=? ORDER BY t", p[0])
        if rows:
            B["mem_detail"][str(p[0])] = [[rel(x[0])] + list(x[1:]) for x in rows]
    B["fd_types"] = {}
    for p in agents_top:
        rows = q("SELECT kind, MAX(count) FROM fd_types WHERE harness=? AND run=? AND pid=? GROUP BY kind ORDER BY 2 DESC", p[0])
        if rows:
            B["fd_types"][str(p[0])] = [list(x) for x in rows]

    # ---- KPIs
    cg = B["cg"][AGENT_CONTAINER]
    win = [x for x in cg if B["t0"] is None or (B["t0"] - 0.2 <= x[0] <= (B["t1"] or 1e9) + 2.5)]
    cpu_rows = q("SELECT t, cpu_us FROM cgroup_samples WHERE harness=? AND run=? AND container=? AND t BETWEEN ? AND ? ORDER BY t", AGENT_CONTAINER, (t0 or 0) - 200, (t1 or 1e18) + 2500)
    mock_rows = q("SELECT t, cpu_us FROM cgroup_samples WHERE harness=? AND run=? AND container='lab-mockllm' AND t BETWEEN ? AND ? ORDER BY t", (t0 or 0) - 200, (t1 or 1e18) + 2500)
    cpu_ms = ((cpu_rows[-1][1] - cpu_rows[0][1]) / 1000) if len(cpu_rows) > 1 else None
    win_s = ((cpu_rows[-1][0] - cpu_rows[0][0]) / 1000) if len(cpu_rows) > 1 else None
    tc = q("SELECT COUNT(*), COALESCE(SUM(is_error),0) FROM tool_calls WHERE harness=? AND run=?")[0]
    mr = q("SELECT COUNT(*), SUM(req_bytes), MAX(req_bytes), SUM(summary), SUM(injected IS NOT NULL), SUM(aux) FROM mock_requests WHERE harness=? AND run=?")[0]
    http_n = q("SELECT COUNT(*) FROM http WHERE harness=? AND run=?")[0][0]
    B["kpi"] = {
        "wall_s": wall, "cpu_ms": r(cpu_ms, 1), "avg_cores": r(cpu_ms / 1000 / win_s, 3) if cpu_ms and win_s else None,
        "mem_max_mb": max((x[2] for x in win), default=None), "pids_max": max((x[5] or 0 for x in win), default=None),
        "processes": len(P), "agents": len(agents), "tool_calls": tc[0], "tool_errors": tc[1],
        "model_requests": mr[0] or http_n, "aux_requests": mr[5], "bytes_sent": mr[1], "max_request": mr[2], "summaries": mr[3], "injected_errors": mr[4],
        "net_rx": net[AGENT_CONTAINER + "_total"]["rx"], "net_tx": net[AGENT_CONTAINER + "_total"]["tx"], "net_lo": net[AGENT_CONTAINER + "_total"]["lo"],
        "syscalls": q("SELECT COALESCE(SUM(count),0) FROM syscalls WHERE harness=? AND run=?")[0][0],
        "file_read": q("SELECT COALESCE(SUM(bytes),0) FROM file_io WHERE harness=? AND run=? AND op='read' AND ftype='file'")[0][0],
        "file_write": q("SELECT COALESCE(SUM(bytes),0) FROM file_io WHERE harness=? AND run=? AND op='write' AND ftype='file'")[0][0],
        "opens": B["opens_total"], "tcp_events": len(B["tcp_events"]),
        "mock_cpu_ms": r((mock_rows[-1][1] - mock_rows[0][1]) / 1000, 1) if len(mock_rows) > 1 else None,
        "runq_wait_ms": r(sum(x[2] or 0 for x in B["roles"]), 1),
    }

    # ---- probes
    pr = []
    for pid, argv, pt0, pt1, u, s, mx, ccpu, fsw, vctx, ictx, code in q("SELECT probe_pid, argv, t0, t_end, user_ms, sys_ms, maxrss_kb, child_cpu_s, fs_write, vol_ctx, invol_ctx, exit_code FROM probe_procs WHERE harness=? AND run=? ORDER BY t0"):
        samples = q("SELECT t, dt, user_ms, sys_ms, rss, heap_used, heap_total, external, array_buffers, elu, lag99, lagmax, threads, handles_json FROM probe_samples WHERE harness=? AND run=? AND probe_pid=? ORDER BY t", pid)
        step = max(1, len(samples) // 1500)
        ss = [[rel(x[0]), r(((x[2] or 0) + (x[3] or 0)) / x[1] * 100 if x[1] else 0, 1), r(x[4] / 1048576, 1), r(x[5] / 1048576, 2), r(x[6] / 1048576, 1), r(x[7] / 1048576, 2), r(x[8] / 1048576, 2), x[9], x[10], x[11], x[12]] for i, x in enumerate(samples) if i % step == 0]
        gcs = q("SELECT kind, COUNT(*), SUM(dur_ms), MAX(dur_ms) FROM gc WHERE harness=? AND run=? AND probe_pid=? GROUP BY kind", pid)
        heap = q("SELECT space, used, size FROM heap_spaces WHERE harness=? AND run=? AND probe_pid=? AND t=(SELECT MAX(t) FROM heap_spaces WHERE harness=? AND run=? AND probe_pid=?)", pid, h, run, pid)
        first_ev = q("SELECT MIN(t) FROM agent_events WHERE harness=? AND run=? AND probe_pid=?", pid)[0][0]
        socks = q("SELECT host, port, bytes_read, bytes_written, t_close - t_open FROM sockets WHERE harness=? AND run=? AND probe_pid=?", pid)
        pr.append({"pid": pid, "argv": argv, "t0": rel(pt0), "t1": rel(pt1), "cpu_ms": r((u or 0) + (s or 0), 1), "user_ms": r(u, 1), "sys_ms": r(s, 1), "maxrss_mb": r((mx or 0) / 1024, 1),
                   "child_cpu_s": r(ccpu, 3), "fs_write": fsw, "vol_ctx": vctx, "invol_ctx": ictx, "exit_code": code, "samples": ss, "handles": json.loads(samples[-1][13]) if samples else {},
                   "gc": [[k, n, r(sm, 2), r(m, 2)] for k, n, sm, m in gcs], "heap_spaces": [list(x) for x in heap], "sockets": [list(x) for x in socks],
                   "startup_ms": r(first_ev - pt0, 1) if first_ev and pt0 else None})
    B["probes"] = pr
    B["probe_cols"] = ["t", "cpu_pct", "rss_mb", "heap_used_mb", "heap_total_mb", "external_mb", "array_buffers_mb", "elu", "lag99_ms", "lagmax_ms", "threads"]

    # ---- timeline events + log
    ev = []
    for t, pid, ty, tool in q("SELECT t, probe_pid, type, tool FROM agent_events WHERE harness=? AND run=? AND type NOT IN ('message_start','message_end','tool_execution_update')"):
        ev.append([rel(t), "agent", ty, f"{ty}{(' ' + tool) if tool else ''}", pid])
    for t, pid, fn, f, a, cp, code, sig, dur in q("SELECT t, probe_pid, fn, file, args, child_nspid, code, signal, dur_ms FROM spawns WHERE harness=? AND run=?"):
        ev.append([rel(t), "spawn", fn, f"{fn} {os.path.basename(f or '')} {a or ''}"[:200] + f"  → pid {cp}, exit {code}{' ' + sig if sig else ''}, {r(dur, 1)} ms", pid])
    for t, pid, fn, f, a, dur, st in q("SELECT t, probe_pid, fn, file, args, dur_ms, status FROM spawn_sync WHERE harness=? AND run=?"):
        ev.append([rel(t), "spawnSync", fn, f"{fn} {f} {a or ''} — blocked the event loop {r(dur, 2)} ms (status {st})", pid])
    for t, pid, o, m, pth, st, ttfb, dur, err in q("SELECT t, probe_pid, origin, method, path, status, ttfb_ms, dur_ms, error FROM http WHERE harness=? AND run=?"):
        ev.append([rel(t), "http", "http", f"{m or ''} {o}{pth or ''} → {st or err} · ttfb {r(ttfb, 1)} ms · total {r(dur, 1)} ms", pid])
    for t, pid, kind, dur in q("SELECT t, probe_pid, kind, dur_ms FROM gc WHERE harness=? AND run=? AND (kind='major' OR dur_ms > 3)"):
        ev.append([rel(t), "gc", kind, f"GC {kind} {r(dur, 2)} ms", pid])
    for t, pid, comm, f, a in q("SELECT t, pid, comm, file, args FROM execs WHERE harness=? AND run=? AND comm NOT LIKE 'runc%'"):
        ev.append([rel(t), "exec", "exec", f"exec {f} {a or ''}   (by {comm}, pid {pid})"[:240], pid])
    for x in B["tcp_events"]:
        ev.append([x[0], "tcp", "tcp", f"TCP {x[2]}({x[1]}) {x[5]} → {x[6]}: {x[3]} → {x[4]}", x[1]])
    for x in B["signals"]:
        ev.append([x[0], "signal", x[5], f"{x[5]} {x[2]}({x[1]}) → {x[4]}({x[3]}) result {x[6]}", x[1]])
    for t, label in q("SELECT t, label FROM marks WHERE harness=? AND run=?"):
        ev.append([rel(t), "mark", "mark", label, None])
    ev.sort(key=lambda e: e[0] or 0)
    if len(ev) > 30000:
        ev = [e for e in ev if e[1] not in ("exec",)][:30000]
    B["events"] = ev

    # ---- tool calls, turns, requests, tokens
    B["tools"] = [[rel(a), rel(b), tool, args, err, ch, tr, full, head, cp, r(ccpu, 2), crss, pid, r(dur, 3)] for a, b, tool, args, err, ch, tr, full, head, cp, ccpu, crss, pid, dur in
                  q("SELECT t_start, t_end, tool, args_json, is_error, result_chars, truncated, full_output_path, result_head, child_procs, child_cpu_ms, child_peak_rss_kb, probe_pid, dur_ms FROM tool_calls WHERE harness=? AND run=? ORDER BY t_start")]
    B["tool_cols"] = ["t0", "t1", "tool", "args", "is_error", "result_chars", "truncated", "full_output", "result_head", "child_procs", "child_cpu_ms", "child_peak_rss_kb", "probe_pid", "dur_ms"]
    B["turns"] = [list(x) for x in q("SELECT probe_pid, idx, turn_ms, pre_request_ms, model_ms, dispatch_ms, tool_ms, post_tool_ms, tools FROM turns WHERE harness=? AND run=? ORDER BY probe_pid, idx")]
    B["turn_cols"] = ["probe_pid", "idx", "turn_ms", "pre_request_ms", "model_ms", "dispatch_ms", "tool_ms", "post_tool_ms", "tools"]
    B["model_turns"] = [[s, i, rel(t), rel(te), m, hm, b, n, tl] for s, i, t, te, m, hm, b, n, tl in q("SELECT sid, idx, t, t_end, model_ms, harness_ms, req_bytes, n_messages, tools FROM model_turns WHERE harness=? AND run=? ORDER BY sid, idx")]
    B["model_turn_cols"] = ["sid", "idx", "t", "t_end", "model_ms", "harness_ms", "req_bytes", "n_messages", "tools"]
    B["requests"] = [[rel(t), rel(te), proto, st, sid, ax, sm, b, nm, nt, tb, sb, trb, json.loads(rp) if rp else None, r(tt, 2), r(sms, 2), ch, oc, inj, mt] for t, te, proto, st, sid, ax, sm, b, nm, nt, tb, sb, trb, rp, tt, sms, ch, oc, inj, mt in
                     q("SELECT t, t_end, proto, step, sid, aux, summary, req_bytes, n_messages, n_tools, tools_bytes, sys_bytes, tool_result_bytes, reply_json, ttfb_ms, stream_ms, chunks, out_chars, injected, max_tokens FROM mock_requests WHERE harness=? AND run=? ORDER BY t")]
    B["request_cols"] = ["t", "t_end", "proto", "step", "sid", "aux", "summary", "req_bytes", "n_messages", "n_tools", "tools_bytes", "sys_bytes", "tool_result_bytes", "reply", "ttfb_ms", "stream_ms", "chunks", "out_chars", "injected", "max_tokens"]
    if not B["requests"]:
        B["requests"] = [[rel(t), rel(t + (d or 0)), "https", None, None, 0, 0, None, None, None, None, None, None, {"status": st, "origin": o}, r(tt, 1), r(d, 1), None, None, None, None]
                         for t, o, st, tt, d in q("SELECT t, origin, status, ttfb_ms, dur_ms FROM http WHERE harness=? AND run=? ORDER BY t")]
    B["tokens"] = [[rel(t), n] for t, n in q("SELECT t, n FROM token_counts WHERE harness=? AND run=? ORDER BY t")]
    B["sudo_log"] = [x[0] for x in q("SELECT line FROM sudo_log WHERE harness=? AND run=?")]
    # long-lived non-agent processes: CPU/RSS curves from /proc samples
    ps = {}
    for pid, n in q("SELECT pid, COUNT(*) FROM proc_samples WHERE harness=? AND run=? GROUP BY pid HAVING COUNT(*) >= 4"):
        if roles_by_pid.get(pid) in ("agent", "agent:nested") or pid not in roles_by_pid:
            continue
        rows = q("SELECT t, utime_s+stime_s, rss_kb, threads, runq_wait_ns FROM proc_samples WHERE harness=? AND run=? AND pid=? ORDER BY t", pid)
        stp = max(1, len(rows) // 500)
        ser = []
        for i in range(stp, len(rows), stp):
            dt = (rows[i][0] - rows[i - stp][0]) / 1000
            ser.append([rel(rows[i][0]), r((rows[i][1] - rows[i - stp][1]) / dt * 100 if dt > 0 else 0, 1), r(rows[i][2] / 1024, 1), rows[i][3]])
        pmeta = next((p for p in P if p[0] == pid), None)
        ps[str(pid)] = {"comm": pmeta[4] if pmeta else "?", "role": roles_by_pid.get(pid), "argv": ((pmeta[5] if pmeta else "") or "")[:160], "series": ser}
    B["proc_series"] = ps
    return B


def comparisons(con, h):
    C = {}

    def clusters(run, gap=2000):
        rows = con.execute("SELECT probe_pid, t0, t_end, user_ms+sys_ms, maxrss_kb, argv FROM probe_procs WHERE harness=? AND run=? AND t_end IS NOT NULL ORDER BY t0", (h, run)).fetchall()
        out, cur = [], []
        for row in rows:
            if cur and row[1] - cur[-1][1] > gap:
                out.append(cur)
                cur = []
            cur.append(row)
        if cur:
            out.append(cur)
        return out

    def ebpf_agents(run):
        return con.execute("SELECT pid, nspid, start_ms, end_ms, cpu_ms, runq_wait_ms, peak_rss_kb, argv FROM procs WHERE harness=? AND run=? AND role='agent' ORDER BY start_ms", (h, run)).fetchall()

    def ebpf_clusters(run, gap=2000):
        rows = ebpf_agents(run)
        out, cur = [], []
        for row in rows:
            if cur and (row[2] or 0) - (cur[-1][2] or 0) > gap:
                out.append(cur)
                cur = []
            cur.append(row)
        if cur:
            out.append(cur)
        return out

    conc = []
    for g in ebpf_clusters("13-concurrency"):
        cpu = [x[4] for x in g]
        rq = [x[5] or 0 for x in g]
        wall = [(x[3] or 0) - (x[2] or 0) for x in g]
        t0, t1 = min(x[2] for x in g), max(x[3] or 0 for x in g)
        mem = con.execute("SELECT MAX(mem) FROM cgroup_samples WHERE harness=? AND run='13-concurrency' AND container=? AND t BETWEEN ? AND ?", (h, AGENT_CONTAINER, t0, t1)).fetchone()[0]
        conc.append({"n": len(g), "latency": "latency=" in (g[0][7] or ""), "cpu_ms_median": r(statistics.median(cpu), 1), "cpu_ms_max": r(max(cpu), 1), "runq_ms_median": r(statistics.median(rq), 1),
                     "wall_ms_median": r(statistics.median(wall), 1), "wall_ms_max": r(max(wall), 1), "rss_mb_median": r(statistics.median(x[6] for x in g) / 1024, 1), "container_mem_max_mb": r((mem or 0) / 1048576, 1)})
    C["concurrency"] = conc
    lock = []
    for g in ebpf_clusters("20-lock-contention"):
        if len(g) < 8:
            continue
        cpu = sorted(x[4] for x in g)
        lock.append({"n": len(g), "private_home": "echo p" in (g[0][7] or ""), "cpu_ms_median": r(statistics.median(cpu), 1), "cpu_ms_p90": r(cpu[int(len(cpu) * 0.9) - 1], 1),
                     "runq_ms_median": r(statistics.median(x[5] or 0 for x in g), 1), "wall_ms_median": r(statistics.median((x[3] or 0) - (x[2] or 0) for x in g), 1)})
    C["lock"] = lock
    for run in ("11-stream-modes", "21-tui-scaling", "01-startup", "23-probe-overhead"):
        C[run] = [{"pid": p, "nspid": ns, "argv": (a or "")[:120], "cpu_ms": r(c, 1), "runq_ms": r(rq, 1), "life_ms": r((e or 0) - (s or 0), 1), "rss_mb": r((m or 0) / 1024, 1)}
                  for p, ns, s, e, c, rq, m, a in ebpf_agents(run)]
    return C


def main():
    con = sqlite3.connect(DB)
    os.makedirs(OUT, exist_ok=True)
    index = {"generated_from": DB, "harnesses": {}}
    for (h,) in con.execute("SELECT DISTINCT harness FROM runs ORDER BY harness").fetchall():
        runs = [x[0] for x in con.execute("SELECT run FROM runs WHERE harness=? ORDER BY run", (h,)).fetchall()]
        disp = con.execute("SELECT harness_display FROM runs WHERE harness=? AND harness_display IS NOT NULL LIMIT 1", (h,)).fetchone()
        hi = {"display": disp[0] if disp else h, "runs": {}, "groups": []}
        os.makedirs(os.path.join(OUT, h), exist_ok=True)
        for run in runs:
            b = run_bundle(con, h, run)
            path = os.path.join(OUT, h, f"{run}.json")
            json.dump(b, open(path, "w"), separators=(",", ":"), default=str)
            hi["runs"][run] = {"desc": b["desc"], "kpi": b["kpi"], "roles": b["roles"], "agent_threads": b["agent_threads"][:6]}
            print(f"{h:12s} {run:22s} {os.path.getsize(path) / 1e6:6.2f} MB")
        seen = set()
        for g, names in GROUPS:
            present = [n for n in names if n in hi["runs"]]
            seen.update(present)
            if present:
                hi["groups"].append([g, present])
        rest = [n for n in runs if n not in seen]
        if rest:
            hi["groups"].append(["More", rest])
        hi["compare"] = comparisons(con, h)
        index["harnesses"][h] = hi
    pred = os.path.join(LAB, "explorer", "predictions.json")
    index["predictions"] = json.load(open(pred)) if os.path.exists(pred) else []
    index["role_info"] = ROLE_INFO
    json.dump(index, open(os.path.join(OUT, "index.json"), "w"), indent=1, default=str)
    os.makedirs(os.path.join(OUT, "docs"), exist_ok=True)
    for src in (os.path.join(ROOT, "docs", "ARCHITECTURE.md"), os.path.join(ROOT, "docs", "RESEARCH.md"), os.path.join(ROOT, "docs", "DATA.md"), os.path.join(ROOT, "reports", "REPORT.md"), os.path.join(ROOT, "reports", "HARNESS-COMPARISON.md"), os.path.join(LAB, "README.md")):
        if os.path.exists(src):
            shutil.copy(src, os.path.join(OUT, "docs", os.path.basename(src)))
    print("index.json written")


if __name__ == "__main__":
    main()
