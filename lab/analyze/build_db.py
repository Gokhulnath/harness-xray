#!/usr/bin/env python3
"""Load every run of every harness into one SQLite database: lab/out/agentlab.sqlite

  python3 analyze/build_db.py                 # all harnesses under out/<harness>/runs/
  python3 analyze/build_db.py pi              # just one harness (others kept if the DB exists? no: full rebuild)
  sqlite3 out/agentlab.sqlite '.tables'

Raw JSONL stays the source of truth; this database is the queryable copy. Every table has
(harness, run). Times are Unix epoch milliseconds. docs/DATA.md documents every table/column.
"""
import json, os, sys, glob, sqlite3, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lablib as L  # noqa: E402

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(LAB, "out")
DB = os.path.join(OUT, "agentlab.sqlite")
FTYPE = {0: "anon-inode", 1: "fifo", 2: "char-dev", 4: "dir", 6: "block-dev", 8: "file", 10: "symlink", 12: "socket"}
TCP_ST = {1: "ESTABLISHED", 2: "SYN_SENT", 3: "SYN_RECV", 4: "FIN_WAIT1", 5: "FIN_WAIT2", 6: "TIME_WAIT", 7: "CLOSE", 8: "CLOSE_WAIT", 9: "LAST_ACK", 10: "LISTEN", 11: "CLOSING"}

SCHEMA = """
CREATE TABLE runs (harness TEXT, run TEXT, harness_display TEXT, description TEXT, t0 REAL, t1 REAL, wall_s REAL, agent_match TEXT, disk_usage TEXT, leftover_processes TEXT, container_json TEXT, PRIMARY KEY (harness, run));
CREATE TABLE steps (harness TEXT, run TEXT, idx INTEGER, kind TEXT, tag TEXT, mode TEXT, wall_s REAL, rc TEXT, detail_json TEXT);
-- eBPF, one row per process. cpu_ms = exact sum of per-thread sum_exec_runtime. role = explicit role (monitor/labclass.py)
CREATE TABLE procs (harness TEXT, run TEXT, pid INTEGER, nspid INTEGER, ppid INTEGER, uid INTEGER, comm TEXT, first_exec TEXT, exe TEXT, argv TEXT,
  role TEXT, label TEXT, depth INTEGER, start_ms REAL, end_ms REAL, life_ms REAL, cpu_ms REAL, runq_wait_ms REAL, timeslices INTEGER, peak_rss_kb INTEGER, threads INTEGER,
  minflt INTEGER, majflt INTEGER, nvcsw INTEGER, nivcsw INTEGER, rchar INTEGER, wchar INTEGER, syscr INTEGER, syscw INTEGER, read_bytes INTEGER, write_bytes INTEGER);
CREATE TABLE threads (harness TEXT, run TEXT, pid INTEGER, tid INTEGER, comm TEXT, cpu_ms REAL, runq_wait_ms REAL, end_ms REAL, peak_rss_kb INTEGER);
CREATE TABLE execs (harness TEXT, run TEXT, t REAL, pid INTEGER, tid INTEGER, comm TEXT, file TEXT, args TEXT);
CREATE TABLE tcp_events (harness TEXT, run TEXT, t REAL, pid INTEGER, comm TEXT, old_state TEXT, new_state TEXT, local TEXT, remote TEXT);
CREATE TABLE signals (harness TEXT, run TEXT, t REAL, sender_pid INTEGER, sender_comm TEXT, target_pid INTEGER, target_comm TEXT, sig INTEGER, result INTEGER);
-- eBPF per-second aggregates (t = end of the 1 s window)
CREATE TABLE syscalls (harness TEXT, run TEXT, t REAL, pid INTEGER, thread TEXT, nr INTEGER, name TEXT, count INTEGER, total_ns INTEGER);
CREATE TABLE file_io (harness TEXT, run TEXT, t REAL, op TEXT, pid INTEGER, comm TEXT, ftype TEXT, dir TEXT, name TEXT, bytes INTEGER, ops INTEGER);
CREATE TABLE opens (harness TEXT, run TEXT, t REAL, pid INTEGER, comm TEXT, file TEXT, count INTEGER);
CREATE TABLE net_bytes (harness TEXT, run TEXT, t REAL, pid INTEGER, comm TEXT, kind TEXT, bytes INTEGER);
CREATE TABLE block_io (harness TEXT, run TEXT, t REAL, comm TEXT, rwbs TEXT, bytes INTEGER);
-- in-process probe (Node agents)
CREATE TABLE probe_procs (harness TEXT, run TEXT, probe_pid INTEGER, argv TEXT, t0 REAL, t_end REAL, user_ms REAL, sys_ms REAL, maxrss_kb INTEGER, child_cpu_s REAL,
  fs_write INTEGER, vol_ctx INTEGER, invol_ctx INTEGER, exit_code INTEGER);
CREATE TABLE probe_samples (harness TEXT, run TEXT, probe_pid INTEGER, t REAL, dt REAL, user_ms REAL, sys_ms REAL, elu REAL, lag50 REAL, lag99 REAL, lagmax REAL,
  rss INTEGER, heap_used INTEGER, heap_total INTEGER, external INTEGER, array_buffers INTEGER, malloced INTEGER, threads INTEGER, child_user_s REAL, child_sys_s REAL, handles_json TEXT);
CREATE TABLE heap_spaces (harness TEXT, run TEXT, probe_pid INTEGER, t REAL, space TEXT, used INTEGER, size INTEGER);
CREATE TABLE gc (harness TEXT, run TEXT, probe_pid INTEGER, t REAL, kind TEXT, dur_ms REAL);
CREATE TABLE spawns (harness TEXT, run TEXT, probe_pid INTEGER, id INTEGER, t REAL, fn TEXT, file TEXT, args TEXT, child_nspid INTEGER, detached INTEGER, cwd TEXT, exit_t REAL, code INTEGER, signal TEXT, dur_ms REAL);
CREATE TABLE spawn_sync (harness TEXT, run TEXT, probe_pid INTEGER, t REAL, fn TEXT, file TEXT, args TEXT, dur_ms REAL, status TEXT);
CREATE TABLE http (harness TEXT, run TEXT, probe_pid INTEGER, t REAL, origin TEXT, method TEXT, path TEXT, status INTEGER, ttfb_ms REAL, dur_ms REAL, error TEXT);
CREATE TABLE sockets (harness TEXT, run TEXT, probe_pid INTEGER, t_open REAL, t_close REAL, host TEXT, port INTEGER, bytes_read INTEGER, bytes_written INTEGER);
CREATE TABLE resource_usage (harness TEXT, run TEXT, probe_pid INTEGER, t REAL, maxrss_kb INTEGER, fs_read INTEGER, fs_write INTEGER, minflt INTEGER, majflt INTEGER, vol_ctx INTEGER, invol_ctx INTEGER, user_ms REAL, sys_ms REAL);
CREATE TABLE agent_events (harness TEXT, run TEXT, probe_pid INTEGER, t REAL, type TEXT, tool_call_id TEXT, tool TEXT, bytes INTEGER);
CREATE TABLE token_counts (harness TEXT, run TEXT, probe_pid INTEGER, t REAL, n INTEGER);
CREATE TABLE tool_calls (harness TEXT, run TEXT, probe_pid INTEGER, source_file TEXT, tool_call_id TEXT, tool TEXT, args_json TEXT, t_start REAL, t_end REAL, dur_ms REAL,
  is_error INTEGER, result_chars INTEGER, truncated INTEGER, full_output_path TEXT, result_head TEXT, child_procs INTEGER, child_cpu_ms REAL, child_peak_rss_kb INTEGER);
CREATE TABLE turns (harness TEXT, run TEXT, probe_pid INTEGER, idx INTEGER, t_start REAL, t_end REAL, turn_ms REAL, pre_request_ms REAL, model_ms REAL, dispatch_ms REAL, tool_ms REAL, post_tool_ms REAL, tools TEXT);
CREATE TABLE model_turns (harness TEXT, run TEXT, sid TEXT, idx INTEGER, t REAL, t_end REAL, model_ms REAL, harness_ms REAL, req_bytes INTEGER, n_messages INTEGER, tools TEXT);
CREATE TABLE mock_requests (harness TEXT, run TEXT, t REAL, t_end REAL, proto TEXT, path TEXT, model TEXT, aux INTEGER, summary INTEGER, scenario TEXT, step INTEGER, sid TEXT, stream INTEGER,
  req_bytes INTEGER, n_messages INTEGER, n_tools INTEGER, tool_names TEXT, tools_bytes INTEGER, sys_bytes INTEGER, tool_result_bytes INTEGER, max_tokens INTEGER,
  reply_json TEXT, notes TEXT, ttfb_ms REAL, stream_ms REAL, chunks INTEGER, out_chars INTEGER, injected INTEGER, user_agent TEXT);
-- monitor samples (100 ms; slow = 1 s)
CREATE TABLE cgroup_samples (harness TEXT, run TEXT, container TEXT, t REAL, cpu_us INTEGER, user_us INTEGER, sys_us INTEGER, mem INTEGER, peak INTEGER, anon INTEGER, file INTEGER, kernel INTEGER,
  sock INTEGER, shmem INTEGER, file_dirty INTEGER, pids INTEGER, io_rbytes INTEGER, io_wbytes INTEGER, psi_cpu_some_us INTEGER, psi_mem_some_us INTEGER, psi_io_some_us INTEGER, pgfault INTEGER, pgmajfault INTEGER, throttled INTEGER);
CREATE TABLE proc_samples (harness TEXT, run TEXT, t REAL, pid INTEGER, ppid INTEGER, state TEXT, utime_s REAL, stime_s REAL, rss_kb INTEGER, threads INTEGER, vctx INTEGER, nvctx INTEGER,
  rchar INTEGER, wchar INTEGER, read_bytes INTEGER, write_bytes INTEGER, runq_wait_ns INTEGER, timeslices INTEGER, cpu_core INTEGER);
CREATE TABLE thread_samples (harness TEXT, run TEXT, t REAL, pid INTEGER, tid INTEGER, name TEXT, utime_s REAL, stime_s REAL, state TEXT, cpu_core INTEGER, runq_wait_ns INTEGER, timeslices INTEGER);
CREATE TABLE proc_meta (harness TEXT, run TEXT, t REAL, pid INTEGER, nspid INTEGER, ppid INTEGER, comm TEXT, cmd TEXT, uid INTEGER, container TEXT);
CREATE TABLE net_dev (harness TEXT, run TEXT, container TEXT, t REAL, iface TEXT, rx_bytes INTEGER, rx_packets INTEGER, rx_errs INTEGER, rx_drop INTEGER, tx_bytes INTEGER, tx_packets INTEGER, tx_errs INTEGER, tx_drop INTEGER);
CREATE TABLE net_snmp (harness TEXT, run TEXT, container TEXT, t REAL, tcp_active_opens INTEGER, tcp_passive_opens INTEGER, tcp_curr_estab INTEGER, tcp_in_segs INTEGER, tcp_out_segs INTEGER,
  tcp_retrans INTEGER, tcp_out_rsts INTEGER, udp_in INTEGER, udp_out INTEGER, sockets_used INTEGER, tcp_inuse INTEGER, tcp_tw INTEGER, tcp_mem_pages INTEGER);
CREATE TABLE vm_samples (harness TEXT, run TEXT, t REAL, cpu_user INTEGER, cpu_nice INTEGER, cpu_system INTEGER, cpu_idle INTEGER, cpu_iowait INTEGER, cpu_irq INTEGER, cpu_softirq INTEGER, cpu_steal INTEGER,
  mem_total_kb INTEGER, mem_available_kb INTEGER, cached_kb INTEGER, dirty_kb INTEGER, load1 REAL, psi_cpu_some_us INTEGER, psi_mem_some_us INTEGER, psi_io_some_us INTEGER, pgfault INTEGER, pgmajfault INTEGER);
CREATE TABLE mem_detail (harness TEXT, run TEXT, t REAL, pid INTEGER, rss_kb INTEGER, pss_kb INTEGER, pss_anon_kb INTEGER, pss_file_kb INTEGER, pss_shmem_kb INTEGER, private_dirty_kb INTEGER, shared_clean_kb INTEGER,
  swap_kb INTEGER, vm_peak_kb INTEGER, vm_size_kb INTEGER, vm_hwm_kb INTEGER, vm_data_kb INTEGER, vm_stk_kb INTEGER, vm_exe_kb INTEGER, vm_lib_kb INTEGER, vm_pte_kb INTEGER);
CREATE TABLE fd_types (harness TEXT, run TEXT, t REAL, pid INTEGER, kind TEXT, count INTEGER);
CREATE TABLE tcp_conns (harness TEXT, run TEXT, container TEXT, t REAL, local TEXT, remote TEXT, state TEXT, tx_queue INTEGER, rx_queue INTEGER, uid INTEGER, inode INTEGER, owner_pid INTEGER);
CREATE TABLE marks (harness TEXT, run TEXT, t REAL, label TEXT);
CREATE TABLE sudo_log (harness TEXT, run TEXT, line TEXT);

CREATE VIEW v_cpu_by_role AS SELECT harness, run, role, ROUND(SUM(cpu_ms),3) cpu_ms, ROUND(SUM(runq_wait_ms),3) runq_wait_ms, COUNT(*) n, MAX(peak_rss_kb)/1024.0 max_rss_mb, SUM(rchar) rchar, SUM(wchar) wchar FROM procs GROUP BY harness, run, role;
CREATE VIEW v_cpu_by_command AS SELECT harness, run, role, comm, COUNT(*) n, ROUND(SUM(cpu_ms),3) cpu_ms, MAX(peak_rss_kb)/1024.0 max_rss_mb FROM procs GROUP BY harness, run, role, comm;
CREATE VIEW v_thread_cpu AS SELECT t.harness, t.run, p.role, t.comm thread, COUNT(*) n, ROUND(SUM(t.cpu_ms),3) cpu_ms, ROUND(SUM(t.runq_wait_ms),3) runq_wait_ms FROM threads t JOIN procs p USING (harness, run, pid) GROUP BY t.harness, t.run, p.role, t.comm;
CREATE VIEW v_syscall_totals AS SELECT harness, run, thread, name, SUM(count) count, ROUND(SUM(total_ns)/1e6,3) total_ms FROM syscalls GROUP BY harness, run, thread, name;
CREATE VIEW v_file_totals AS SELECT harness, run, op, comm, ftype, dir, name, SUM(bytes) bytes, SUM(ops) ops FROM file_io GROUP BY harness, run, op, comm, ftype, dir, name;
CREATE VIEW v_net_totals AS SELECT harness, run, comm, kind, SUM(bytes) bytes FROM net_bytes GROUP BY harness, run, comm, kind;
CREATE VIEW v_tool_latency AS SELECT harness, run, tool, COUNT(*) n, ROUND(AVG(dur_ms),3) avg_ms, MAX(dur_ms) max_ms, SUM(is_error) errors FROM tool_calls GROUP BY harness, run, tool;
"""
INDEXES = """
CREATE INDEX i_procs ON procs(harness, run, pid); CREATE INDEX i_ps ON probe_samples(harness, run, probe_pid, t);
CREATE INDEX i_ev ON agent_events(harness, run, probe_pid, t); CREATE INDEX i_cg ON cgroup_samples(harness, run, container, t);
CREATE INDEX i_pss ON proc_samples(harness, run, pid, t); CREATE INDEX i_ts ON thread_samples(harness, run, pid, tid, t);
CREATE INDEX i_tc ON tool_calls(harness, run, t_start); CREATE INDEX i_sc ON syscalls(harness, run, t); CREATE INDEX i_fio ON file_io(harness, run, t);
CREATE INDEX i_threads ON threads(harness, run, pid);
"""


def ins(con, table, rows):
    rows = list(rows)
    if rows:
        con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)


def load_run(con, harness, rd):
    run = os.path.basename(rd)
    meta = L.load_meta(rd)
    HR = (harness, run)
    syscalls = {}
    try:
        syscalls = {int(k): v for k, v in json.load(open(os.path.join(rd, "syscalls.json"))).items()}
    except Exception:
        pass
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*HR, meta.get("harness_display"), meta.get("desc"), meta.get("t0"), meta.get("t1"), meta.get("wall_s"), meta.get("agent_match"), meta.get("disk_usage"), meta.get("leftover_processes"), json.dumps(meta.get("container")) if meta.get("container") else None))
    for i, st in enumerate(meta.get("steps", [])):
        if not isinstance(st, dict):
            continue
        kind = "parallel" if st.get("parallel") else "rpc" if st.get("rpc") else "tui" if st.get("mode") == "tui" else "shell" if st.get("shell") else "sleep" if "sleep" in st else "agent"
        con.execute("INSERT INTO steps VALUES (?,?,?,?,?,?,?,?,?)", (*HR, i, kind, st.get("tag") or (st.get("shell") or "")[:80] or st.get("label"), st.get("mode"), st.get("wall_s"), str(st.get("rc")), json.dumps(st)[:30000]))
        if (st.get("shell") or "").startswith("diff /tmp/sudo.before"):
            ins(con, "sudo_log", [(*HR, line) for line in (st.get("out") or "").splitlines()])

    # ---- eBPF
    procs, threads, ev = L.load_procs(rd, meta.get("agent_match"))
    ins(con, "procs", [(*HR, p["pid"], p.get("nspid"), p.get("ppid"), p.get("uid"), p.get("comm"), p.get("first_exec"), p.get("exe"), p.get("argv"), p["role"], p["label"], p["depth"],
                         p.get("start"), p.get("end"), p.get("life_ms"), p["cpu_ns"] / 1e6, p["rqdelay"] / 1e6, p["slices"], p["mem_kb"], p["threads"], p["minflt"], p["majflt"], p["nvcsw"], p["nivcsw"],
                         p["rchar"], p["wchar"], p["syscr"], p["syscw"], p["rbytes"], p["wbytes"]) for p in procs.values()])
    ins(con, "threads", [(*HR, pid, tid, t.get("comm"), t["rt"] / 1e6, (t.get("rqdelay") or 0) / 1e6, t.get("end"), t.get("mem_kb")) for (pid, tid), t in threads.items()])
    rows = collections.defaultdict(list)
    for e in ev:
        k = e["e"]
        if k == "A":
            rows["execs"].append((*HR, e["t"], e["pid"], e["tid"], e.get("comm"), e.get("file"), " ".join(a for a in (e.get("a1"), e.get("a2"), e.get("a3"), e.get("a4")) if a)))
        elif k == "T":
            rows["tcp_events"].append((*HR, e["t"], e.get("pid"), e.get("comm"), TCP_ST.get(e.get("old"), e.get("old")), TCP_ST.get(e.get("new"), e.get("new")), f"{e.get('saddr')}:{e.get('sport')}", f"{e.get('daddr')}:{e.get('dport')}"))
        elif k == "S":
            rows["signals"].append((*HR, e["t"], e.get("pid"), e.get("comm"), e.get("tpid"), e.get("tcomm"), e.get("sig"), e.get("res")))
        elif k == "SY":
            rows["syscalls"].append((*HR, e.get("t"), e["pid"], e.get("comm"), e["nr"], syscalls.get(e["nr"], str(e["nr"])), e["n"], e["ns"]))
        elif k in ("FR", "FW"):
            rows["file_io"].append((*HR, e.get("t"), "read" if k == "FR" else "write", e["pid"], e.get("comm"), FTYPE.get(e.get("ftype"), str(e.get("ftype"))), e.get("dir"), e.get("name"), e["bytes"], e["ops"]))
        elif k == "OP":
            rows["opens"].append((*HR, e.get("t"), e["pid"], e.get("comm"), e.get("file"), e["n"]))
        elif k == "NT":
            rows["net_bytes"].append((*HR, e.get("t"), e["pid"], e.get("comm"), e.get("kind"), e["bytes"]))
        elif k == "BK":
            rows["block_io"].append((*HR, e.get("t"), e.get("comm"), e.get("rwbs"), e["bytes"]))
    for table, rr in rows.items():
        ins(con, table, rr)

    # ---- probes
    probes = L.load_probes(rd)
    ev_by_id = {}
    for pid, pr in probes.items():
        d = pr["recs"]
        end = d["end"][0] if d["end"] else {}
        con.execute("INSERT INTO probe_procs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*HR, pid, " ".join(pr["argv"][1:])[:1000], pr["t0"], end.get("t"), end.get("userMs"), end.get("sysMs"), end.get("maxRssKB"),
                                                                                    (end.get("cut") or 0) + (end.get("cst") or 0), end.get("fsWrite"), end.get("volCtx"), end.get("involCtx"), end.get("code")))
        ins(con, "probe_samples", [(*HR, pid, s["t"], s["dt"], s["u"], s["sy"], s["elu"], s["lag50"], s["lag99"], s["lagMax"], s["rss"], s["heapUsed"], s["heapTotal"], s["ext"], s["ab"], s.get("mallocd"), s.get("thr"), s.get("cut"), s.get("cst"), json.dumps(s.get("h", {}))) for s in d["s"]])
        ins(con, "heap_spaces", [(*HR, pid, h["t"], sp[0], sp[1], sp[2]) for h in d["heap"] for sp in h["spaces"]])
        ins(con, "gc", [(*HR, pid, g["t"], str(g["kind"]), g["dur"]) for g in d["gc"]])
        exits = {x["id"]: x for x in d["exit"]}
        ins(con, "spawns", [(*HR, pid, s["id"], s["t"], s["fn"], s["file"], s.get("args"), s.get("pid"), int(bool(s.get("detached"))), s.get("cwd"), exits.get(s["id"], {}).get("t"), exits.get(s["id"], {}).get("code"), exits.get(s["id"], {}).get("signal"), exits.get(s["id"], {}).get("durMs")) for s in d["spawn"]])
        ins(con, "spawn_sync", [(*HR, pid, s["t"], s["fn"], s["file"], s.get("args"), s["durMs"], str(s.get("status"))) for s in d["spawnSync"]])
        ins(con, "http", [(*HR, pid, h["t"], h.get("origin"), h.get("method"), h.get("path"), h.get("status"), h.get("ttfb"), h.get("durMs"), h.get("error")) for h in d["http"]])
        ins(con, "sockets", [(*HR, pid, s.get("opened"), s["t"], s.get("host"), s.get("port"), s.get("bytesRead"), s.get("bytesWritten")) for s in d["sockclose"]])
        ins(con, "resource_usage", [(*HR, pid, r["t"], r.get("maxRssKB"), r.get("fsRead"), r.get("fsWrite"), r.get("minflt"), r.get("majflt"), r.get("volCtx"), r.get("involCtx"), r.get("userMs"), r.get("sysMs")) for r in d["ru"]])
        ins(con, "agent_events", [(*HR, pid, e["t"], e["type"], e.get("id"), e.get("tool"), e.get("bytes")) for e in d["ev"]])
        ins(con, "token_counts", [(*HR, pid, e["t"], e["n"]) for e in d["tok"]])
        for e in d["ev"]:
            if e.get("id"):
                ev_by_id.setdefault(e["id"], {"pid": pid})[e["type"]] = e["t"]
        ins(con, "turns", [(*HR, pid, r["i"], r.get("t0"), r.get("t1"), r.get("turn_ms"), r.get("pre_request_ms"), r.get("model_ms"), r.get("dispatch_ms"), r.get("tool_ms"), r.get("post_tool_ms"), ",".join(r.get("tools", []))) for r in L.turn_analysis_probe(pr)])

    # ---- tool calls: harness JSON stream (args/results) + probe timing + eBPF children
    by_nspid = collections.defaultdict(list)
    for p in procs.values():
        if p.get("nspid") is not None:
            by_nspid[p["nspid"]].append(p)
    kids = collections.defaultdict(list)
    for p in procs.values():
        kids[p.get("ppid")].append(p)

    def subtree(p):
        out, stack = [p], list(kids.get(p["pid"], []))
        while stack:
            c = stack.pop()
            out.append(c)
            stack.extend(kids.get(c["pid"], []))
        return out

    tc_rows = []
    for ef in sorted(glob.glob(os.path.join(rd, "events-*.jsonl"))):
        starts = {}
        for e in L.jl(ef):
            ty = e.get("type")
            if ty == "tool_execution_start":
                starts[e.get("toolCallId")] = e
            elif ty == "tool_execution_end":
                s = starts.get(e.get("toolCallId"), {})
                tm = ev_by_id.get(e.get("toolCallId"), {})
                t0, t1 = tm.get("tool_execution_start"), tm.get("tool_execution_end")
                content = (e.get("result") or {}).get("content") or [{}]
                text = content[0].get("text", "") if isinstance(content[0], dict) else ""
                det = (e.get("result") or {}).get("details") or {}
                full = text.split("Full output: ", 1)[1].split("]")[0] if "Full output: " in text else None
                cc, ccpu, crss = 0, 0.0, 0
                pip = tm.get("pid")
                if t0 and t1 and pip is not None:
                    for pp in by_nspid.get(pip, []):
                        if not pp["role"].startswith("agent"):
                            continue
                        for c in kids.get(pp["pid"], []):
                            if c.get("start") and t0 - 5 <= c["start"] <= t1 + 5:
                                for x in subtree(c):
                                    cc += 1
                                    ccpu += x["cpu_ns"] / 1e6
                                    crss = max(crss, x["mem_kb"])
                tc_rows.append((*HR, pip, os.path.basename(ef), e.get("toolCallId"), e.get("toolName"), json.dumps(s.get("args"))[:4000], t0, t1, (t1 - t0) if (t0 and t1) else None,
                                int(bool(e.get("isError"))), len(text), int(bool(isinstance(det, dict) and det.get("truncation"))), full, text[:600], cc, round(ccpu, 3), crss))
    ins(con, "tool_calls", tc_rows)

    # ---- model requests + harness-agnostic turns
    mock = L.jl(os.path.join(rd, "mockllm.jsonl"))
    ins(con, "mock_requests", [(*HR, m.get("t"), m.get("tEnd"), m.get("proto"), m.get("path"), m.get("model"), int(bool(m.get("aux"))), int(bool(m.get("summary"))), m.get("scenario"), m.get("step"), m.get("sid"), int(bool(m.get("stream"))),
                                 m.get("reqBytes"), m.get("nMessages"), m.get("nTools"), ",".join(m.get("toolNames") or []), m.get("toolsBytes"), m.get("sysBytes"), m.get("toolResultBytes"), m.get("maxTokens"),
                                 json.dumps(m.get("reply")), json.dumps(m.get("notes")) if m.get("notes") else None, m.get("ttfbMs"), m.get("streamMs"), m.get("chunks"), m.get("outChars"), m.get("injected"), m.get("ua")) for m in mock])
    ins(con, "model_turns", [(*HR, r["sid"], r["i"], r["t"], r["t_end"], r["model_ms"], r["harness_ms"], r["req_bytes"], r["n_messages"], ",".join(r["tools"] or [])) for r in L.turn_analysis_mock(mock)])

    # ---- monitor samples
    rows = collections.defaultdict(list)
    for r in L.jl(os.path.join(rd, "samples.jsonl")):
        k = r.get("k")
        t = r.get("t")
        if k == "t":
            for cname, c in (r.get("cg") or {}).items():
                ms, io, psi = c.get("ms") or {}, c.get("io") or {}, c.get("psi") or {}
                g = lambda kind: (((psi.get(kind) or {}).get("some") or {}).get("total"))
                rows["cgroup_samples"].append((*HR, cname, t, c.get("cpu_us"), c.get("user_us"), c.get("sys_us"), c.get("mem"), c.get("peak"), ms.get("anon"), ms.get("file"), ms.get("kernel"), ms.get("sock"), ms.get("shmem"), ms.get("file_dirty"),
                                               c.get("pids"), io.get("rbytes"), io.get("wbytes"), g("cpu"), g("mem"), g("io"), ms.get("pgfault"), ms.get("pgmajfault"), c.get("thr")))
            for p in r.get("procs") or []:
                rows["proc_samples"].append((*HR, t, p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8], p[9], p[10], p[11], p[12], p[15] if len(p) > 15 else None, p[16] if len(p) > 16 else None, p[17] if len(p) > 17 else None))
            for tr in r.get("thr") or []:
                rows["thread_samples"].append((*HR, t, *tr))
            for cname, n in (r.get("net") or {}).items():
                for iface, v in (n.get("dev") or {}).items():
                    if any(v):
                        rows["net_dev"].append((*HR, cname, t, iface, *v))
                tcp, udp, sk = n.get("tcp") or {}, n.get("udp") or {}, n.get("sock") or {}
                rows["net_snmp"].append((*HR, cname, t, tcp.get("ActiveOpens"), tcp.get("PassiveOpens"), tcp.get("CurrEstab"), tcp.get("InSegs"), tcp.get("OutSegs"), tcp.get("RetransSegs"), tcp.get("OutRsts"),
                                         udp.get("InDatagrams"), udp.get("OutDatagrams"), sk.get("used"), sk.get("tcp_inuse"), sk.get("tcp_tw"), sk.get("tcp_mem")))
            vm = r.get("vm") or {}
            cpu = vm.get("cpu") or [None] * 8
            mem = vm.get("mem") or {}
            psi = vm.get("psi") or {}
            vs = vm.get("vmstat") or {}
            g = lambda kind: (((psi.get(kind) or {}).get("some") or {}).get("total"))
            rows["vm_samples"].append((*HR, t, *cpu[:8], mem.get("MemTotal"), mem.get("MemAvailable"), mem.get("Cached"), mem.get("Dirty"), (vm.get("load") or [None])[0], g("cpu"), g("memory"), g("io"), vs.get("pgfault"), vs.get("pgmajfault")))
        elif k == "slow":
            inode_owner = {}
            for pid, inodes in (r.get("sock_inodes") or {}).items():
                for ino in inodes:
                    inode_owner[ino] = int(pid)
            for pid, m in (r.get("mem") or {}).items():
                rows["mem_detail"].append((*HR, t, int(pid), m.get("Rss"), m.get("Pss"), m.get("Pss_Anon"), m.get("Pss_File"), m.get("Pss_Shmem"), m.get("Private_Dirty"), m.get("Shared_Clean"), m.get("Swap"),
                                           m.get("VmPeak"), m.get("VmSize"), m.get("VmHWM"), m.get("VmData"), m.get("VmStk"), m.get("VmExe"), m.get("VmLib"), m.get("VmPTE")))
            for pid, kinds in (r.get("fd") or {}).items():
                for kind, n in kinds.items():
                    rows["fd_types"].append((*HR, t, int(pid), kind, n))
            for cname, conns in (r.get("tcp") or {}).items():
                for c in conns:
                    rows["tcp_conns"].append((*HR, cname, t, *c, inode_owner.get(c[6])))
        elif k == "meta":
            rows["proc_meta"].append((*HR, t, r.get("pid"), r.get("nspid"), r.get("ppid"), r.get("comm"), r.get("cmd"), r.get("uid"), r.get("cg")))
        elif k == "mark":
            rows["marks"].append((*HR, t, r.get("label")))
    for table, rr in rows.items():
        ins(con, table, rr)
    return len(procs), len(probes)


def main():
    only = sys.argv[1:]
    harnesses = sorted(d for d in os.listdir(OUT) if os.path.isdir(os.path.join(OUT, d, "runs")) and (not only or d in only))
    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    for h in harnesses:
        for rd in sorted(glob.glob(os.path.join(OUT, h, "runs", "*"))):
            if not os.path.isdir(rd):
                continue
            np_, npr = load_run(con, h, rd)
            con.commit()
            print(f"{h:12s} {os.path.basename(rd):22s} procs={np_:5d} probes={npr}")
    con.executescript(INDEXES)
    con.commit()
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print(f"  {t:16s} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>10,d} rows")
    con.close()
    os.replace(tmp, DB)
    print("wrote", DB, round(os.path.getsize(DB) / 1e6, 1), "MB")


if __name__ == "__main__":
    main()
