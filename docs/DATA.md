# Data guide: every trace and metric the agent lab stores

Everything lives under `lab/out/`. Raw JSONL is the source of truth; the
SQLite database is a rebuildable, queryable copy; the explorer bundles are derived from the database.

```
lab/out/
  <harness>/runs/<scenario>/      raw traces for one scenario run (one folder per run)
  <harness>/sweep.log             runner log for the last full sweep
  agentlab.sqlite                 every run of every harness, one database (rebuild: python3 analyze/build_db.py)
  archive-v1/                     first-generation pi runs (pi-only lab, kept for reference)
lab/explorer/data/                explorer bundles (rebuild: python3 analyze/build_explorer.py)
```

## Raw files in a run folder

| File | Written by | What is in it |
|---|---|---|
| `meta.json` | runner (`run.py`) | scenario, harness, start/end time, every step (command, mode, wall time, exit code, shell output), disk usage after the run, processes left behind |
| `samples.jsonl` | monitor (`sampler.py`) | `k=meta` one line per new process (pid, container pid, parent, uid, cmdline, container); `k=t` every 100 ms: all processes, all threads, cgroup counters for each lab container, container network counters, VM-wide CPU/memory/pressure; `k=slow` every 1 s: memory maps (smaps_rollup + status), open-fd kinds, socket inodes, TCP connection tables; `k=mark` runner markers |
| `proc-events.jsonl` | monitor (bpftrace, `proctrace.bt`) | kernel events for the agent container's cgroup: `A` execve (file + argv), `X` exec done, `F` fork/clone, `E` thread exit (exact CPU, run-queue wait, I/O accounting, faults, context switches), `D` final context switch (complete CPU), `M` memory map at exit (real peak RSS), `T` TCP state change, `S` signal; per-second aggregates `SY` syscalls, `FR`/`FW` file reads/writes, `OP` opens, `NT` network bytes, `BK` block I/O |
| `syscalls.json` | monitor | syscall number → name for this kernel (arm64) |
| `probe-<pid>.jsonl` | in-process probe (`probe/probe.cjs`, Node agents only) | `start`, `s` every 100 ms (CPU, ELU, loop lag, RSS, heap, external, array buffers, children CPU, active handles), `heap` (V8 spaces), `gc`, `spawn`/`exit`/`spawnSync`/`worker`, `http`, `net`, `sockclose` (bytes per connection), `ru` (resourceUsage each second), `ev` (agent JSON events with in-process timestamps), `tok` (streamed-token event counts), `end` |
| `mockllm.jsonl` | mock LLM | every model request: protocol, model, step, session id, request bytes, message count, tool names and schema bytes, system prompt size, tool-result bytes, max tokens, reply (tool calls + how each intent mapped onto the harness's tools), injected errors, timing |
| `events-<tag>.jsonl` | the harness (stdout) | the harness's own structured output (pi `--mode json` events, Claude Code `stream-json`, ...) |
| `stderr-<tag>.txt`, `tmux-<tag>.txt` | the harness | stderr; the final TUI screen for interactive runs |
| `strace-*.txt` | strace (scenario 17) | syscall census and exec/connect/open trace |

Process identity: pids in `samples.jsonl` and `proc-events.jsonl` are VM pids (the monitor runs in the
VM's root pid namespace); `nspid` is the pid inside the container, which is what the agent, the probe
(`probe-<nspid>.jsonl`) and tool output report.

## How each number is measured

| Metric | Source | Precision |
|---|---|---|
| CPU per process / thread | eBPF `sched_process_exit` + final `sched_switch`: `task->se.sum_exec_runtime` | exact (ns), every process incl. ones that live 0.1 ms |
| Run-queue wait (CPU contention) | eBPF exit `task->sched_info.run_delay`; live `/proc/<pid>/task/<tid>/schedstat` | exact at exit; 100 ms live |
| Peak RSS per process | eBPF `kprobe:do_exit`: `max(mm->hiwater_rss, file+anon+shmem)` | exact at exit (the kernel's `maxrss` also counts the pre-exec copy of a forked parent, so it is not used) |
| Read/write bytes per process | eBPF exit `task->ioac` (rchar, wchar = all read/write syscalls incl. pipes and sockets; read_bytes/write_bytes = storage) | exact |
| Syscalls by thread and name, time in syscall | eBPF `raw_syscalls` enter/exit, per second | exact counts; time includes blocking |
| File I/O by file | eBPF `vfs_read`/`vfs_readv`/`vfs_write`/`vfs_writev` return values, keyed by file type + dir + name | exact bytes per second |
| Opens | eBPF `sys_enter_openat` | exact |
| Network bytes per process | eBPF `tcp_sendmsg`, `tcp_cleanup_rbuf`, `udp_sendmsg/recvmsg`, `unix_stream_sendmsg/recvmsg` | exact bytes per second |
| TCP connections | eBPF `inet_sock_set_state`; `/proc/net/tcp` tables each second | every state change |
| Signals | eBPF `signal_generate` | every signal sent from inside the container |
| Container totals | cgroup v2 `cpu.stat`, `memory.current`, `memory.stat`, `io.stat`, `pids.current`, PSI | 100 ms |
| Container network | `/proc/<pid>/net/dev`, `/net/snmp`, `/net/sockstat` | 100 ms |
| VM | `/proc/stat`, `/proc/meminfo`, `/proc/loadavg`, `/proc/pressure/*`, `/proc/vmstat` | 100 ms |
| Node internals | in-process probe: `perf_hooks` (ELU, loop delay histogram, GC observer), `v8.getHeapStatistics`, `process.cpuUsage`, `diagnostics_channel` (undici, net), `child_process` wrappers | 100 ms samples, every event |
| What the agent sent the model | mock LLM log | every request |

## Process roles

Every process gets one explicit role (`monitor/labclass.py`); there is no catch-all bucket.

| Role | Meaning |
|---|---|
| `agent` | the harness process (matched by the adapter's `agent_match` regex) |
| `agent:nested` | an agent started by another agent (subagents) |
| `tool:shell` | a shell the agent spawned for a tool call |
| `tool:command` | a program a tool call ran (python3, seq, uname, apt-get's helpers...) |
| `tool:search` | rg / fd spawned by grep / find tools |
| `tool:sudo` | the sudo binary inside a tool call |
| `tool:root` | a program running as root under sudo |
| `agent:helper` | a direct child of the agent that is not a tool call (tmux queries, git, which...) |
| `tui:tmux` | tmux hosting an interactive run, and the pane shell that launches the agent |
| `lab:docker-exec` | runc processes behind each `docker exec` |
| `lab:harness` | commands the lab runner itself executed |
| `container:init` | docker-init and the keep-alive `sleep infinity` |

## SQLite tables (`lab/out/agentlab.sqlite`)

Every table has `harness` and `run`. Times are Unix epoch milliseconds.

| Table | Grain | Key columns |
|---|---|---|
| `runs` | one per run | description, t0, t1, wall_s, agent_match, disk_usage, leftover_processes, container_json (fresh container id, image, recreate time, eBPF attach, cold) |
| `steps` | one per runner step | kind, tag, mode, wall_s, rc, detail_json |
| `procs` | one per process (eBPF) | pid, nspid, ppid, uid, comm, first_exec, exe, argv, **role**, label, depth, start_ms, end_ms, life_ms, **cpu_ms**, **runq_wait_ms**, timeslices, **peak_rss_kb**, threads, minflt, majflt, nvcsw, nivcsw, rchar, wchar, syscr, syscw, read_bytes, write_bytes |
| `threads` | one per thread (eBPF) | pid, tid, comm (thread name), cpu_ms, runq_wait_ms, end_ms, peak_rss_kb |
| `execs` | every execve | t, pid, comm, file, args |
| `tcp_events` | every TCP state change | pid, comm, old_state, new_state, local, remote |
| `signals` | every signal | sender, target, sig, result |
| `syscalls` | per second × pid × thread name × syscall | nr, name, count, total_ns |
| `file_io` | per second × pid × file | op, ftype (file, socket, fifo, char-dev, anon-inode...), dir, name, bytes, ops |
| `opens` | per second × pid × path | count |
| `net_bytes` | per second × pid × kind | kind (tcp_tx, tcp_rx, udp_tx, udp_rx, unix_tx, unix_rx), bytes |
| `block_io` | per second × comm | rwbs, bytes (VM-wide) |
| `probe_procs`, `probe_samples`, `heap_spaces`, `gc`, `spawns`, `spawn_sync`, `http`, `sockets`, `resource_usage`, `agent_events`, `token_counts` | in-process probe | see raw file table above |
| `tool_calls` | one per tool call | tool, args_json, t_start, t_end, dur_ms, is_error, result_chars, truncated, full_output_path, child_procs, child_cpu_ms, child_peak_rss_kb |
| `turns` | one per agent turn (pi events) | pre_request_ms, model_ms, dispatch_ms, tool_ms, post_tool_ms |
| `model_turns` | one per model request (harness-agnostic) | model_ms, harness_ms (time until the next request), req_bytes, n_messages |
| `mock_requests` | every request the mock served | proto, aux, summary, step, req_bytes, n_messages, n_tools, tool_names, tools_bytes, sys_bytes, tool_result_bytes, reply_json, injected |
| `cgroup_samples` | 100 ms × container | cpu_us, user_us, sys_us, mem, peak, anon, file, kernel, sock, shmem, file_dirty, pids, io_rbytes, io_wbytes, PSI totals, pgfault |
| `proc_samples` | 100 ms × process | utime, stime, rss_kb, threads, ctx switches, rchar/wchar/read_bytes/write_bytes, runq_wait_ns, timeslices, cpu_core |
| `thread_samples` | 100 ms × thread | name, utime, stime, state, cpu_core, runq_wait_ns, timeslices |
| `net_dev`, `net_snmp` | 100 ms × container | interface byte/packet/error counters; TCP opens, established, segments, retransmits; UDP datagrams; sockets in use |
| `vm_samples` | 100 ms | VM CPU jiffies by state, memory available, load, PSI, page faults |
| `mem_detail` | 1 s × process | RSS, PSS (anon/file/shmem), private dirty, VmSize, VmData, VmPTE ... |
| `fd_types` | 1 s × process | kind (socket, pipe, file, dev, eventpoll, eventfd, timerfd, io_uring ...), count |
| `tcp_conns` | 1 s × connection | local, remote, state, queues, uid, inode, owner_pid |
| `marks`, `sudo_log`, `proc_meta` | — | runner markers, sudo log lines written during the run, /proc identity |

Views: `v_cpu_by_role`, `v_cpu_by_command`, `v_thread_cpu`, `v_syscall_totals`, `v_file_totals`,
`v_net_totals`, `v_tool_latency`.

## Example queries

```sql
-- Where did the CPU go in a run?
SELECT role, cpu_ms, runq_wait_ms, n, max_rss_mb FROM v_cpu_by_role WHERE harness='pi' AND run='10-sudo' ORDER BY cpu_ms DESC;

-- Which of the agent's threads burn CPU?
SELECT thread, n, cpu_ms FROM v_thread_cpu WHERE harness='pi' AND run='04-loop-200' AND role='agent' ORDER BY cpu_ms DESC;

-- Top syscalls by thread name
SELECT thread, name, count, total_ms FROM v_syscall_totals WHERE harness='pi' AND run='05-loop-mixed-300' ORDER BY count DESC LIMIT 20;

-- Which files did the agent read/write most?
SELECT op, comm, ftype, dir || '/' || name AS file, bytes FROM v_file_totals WHERE harness='pi' AND run='07-big-output' ORDER BY bytes DESC LIMIT 20;

-- Request size per turn: the whole conversation re-sent each time
SELECT step, req_bytes, n_messages FROM mock_requests WHERE harness='pi' AND run='04-loop-200' AND aux=0 ORDER BY t;

-- The sudo chain of a run, with exact CPU and memory
SELECT pid, ppid, uid, comm, role, ROUND(cpu_ms,2) cpu_ms, peak_rss_kb, argv FROM procs WHERE harness='pi' AND run='10-sudo' AND role IN ('tool:sudo','tool:root') ORDER BY start_ms;

-- CPU contention: run-queue wait per agent as concurrency grows
SELECT run, COUNT(*) agents, ROUND(AVG(cpu_ms),1) cpu, ROUND(AVG(runq_wait_ms),1) wait FROM procs WHERE harness='pi' AND role='agent' GROUP BY run;

-- Every TCP connection the agent opened
SELECT t, comm, old_state, new_state, local, remote FROM tcp_events WHERE harness='pi' AND run='02-idle-tui' ORDER BY t;
```

## Rebuilding

```bash
cd lab
python3 analyze/build_db.py            # raw JSONL -> out/agentlab.sqlite
python3 analyze/build_explorer.py      # database -> explorer/data/
```
