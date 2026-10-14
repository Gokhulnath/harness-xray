# How the pi coding agent consumes CPU, memory, disk, network, processes and threads

**What was tested:** pi 0.87.1 (npm `@earendil-works/pi-coding-agent`), a Node.js 24 CLI.

**Where it ran:** Docker Desktop on an Apple M5 Max. The Linux VM has 18 vCPU and 11.6 GB RAM (linuxkit kernel 7.0, cgroup v2, arm64). pi ran as the non-root user `agent` against two kinds of model:
- a scripted mock LLM that streams at full speed (scenarios 00-23);
- a real model, OpenRouter `nvidia/nemotron-3-super-120b-a12b:free` (scenarios R1-R3).

**How it was measured:** eBPF tracing (exact per-process and per-thread accounting, plus per-second aggregates), 100 ms `/proc` and cgroup sampling, and an in-process Node probe.

**Isolation:** every scenario ran in a freshly recreated container, and the tracer was confirmed attached first (`SELECT COUNT(*), SUM(container_json LIKE '%"fresh": true%'), SUM(container_json LIKE '%bpf_attached": true%') FROM runs WHERE harness='pi'` → `27, 27, 27`).

**Where the data is:** `lab/out/agentlab.sqlite` (schema in `docs/DATA.md`), with raw traces in `lab/out/pi/runs/<scenario>/`. To look at any run in the explorer, open `http://localhost:8765/explorer/#/h/pi/run/<run>`.

**How the numbers were checked:** each unit's analysis was re-checked by an adversarial verifier. This report applies every verifier correction and drops every refuted claim. It also applies four data fixes made after the analyses ran (see Part 5.3; fix 4, the broken Node compile cache, was found after the report was written). Unless a section says otherwise:
- memory is in MiB (KB/1024);
- disk and network are in decimal KB/MB;
- "CPU" means exact eBPF CPU time.

---

## TL;DR

1. **Each process start costs about 250 ms of CPU and about 150 MiB of memory.**
   - A one-turn `--mode json` run takes a median 246.5 ms CPU, 191.8 ms wall and 146.5 MiB peak RSS (n=13, CPU range 240.5-265.3 ms).
   - Peak RSS is bimodal, up to 157.7 MiB, so size for about 158 MiB.
   - About 144 ms CPU / 110 ms wall goes to loading and evaluating 4.5 MB of bundled JS before `main()` runs. pi does call Node's `enableCompileCache()`, but in the lab image it silently failed (a root-owned `/tmp/node-compile-cache` made by `npm install -g`; see Part 5.3, fix 4). With a working cache a one-turn chat costs about 26 ms less CPU and 14 MiB less RSS. `PI_TIMING` sees only 21-25 ms of the start.
   - `pi --version` alone costs 164.5 ms CPU.
2. **One tool turn costs about 5 ms of pi CPU when the model answers instantly.** That is 3.3 ms on the main thread plus 1.7 ms on four V8 background threads (04, 200 turns).
   - It rises about 0.5-0.8 ms per 100 KB of transcript.
   - While looping, pi uses about 1.1 cores.
   - Each tool child adds its own cost: bash 0.80 ms / 2.6 MiB; grep runs 2 `rg` processes (≈5 ms); a working `fd` costs 36-44 ms.
3. **With a real model, 97.4-99.5% of wall time is spent waiting on the LLM.**
   - pi then burns CPU in proportion to streaming wall time, not turns: R1 used 2,154 ms over 78.8 s (27.3 ms/s). The median per turn was 40 ms (R1) and 62 ms (R2), and 78.5% of the CPU came during token streaming.
   - About 9-13 ms/s of that is the lab's own probe.
4. **Each agent holds about 100 MiB of private memory plus about 50 MiB of shared code pages.**
   - Container memory grows by 101.7 MiB per concurrent agent (r²=1.000, N=1-32).
   - RSS then grows 0.33-0.81 MiB per turn, reaching 210 MiB at 200 echo turns and 373 MiB at 300 mixed turns.
   - Compaction does not give it back, and neither do idle gaps. The `read` tool loads whole files: three back-to-back reads of a 19.5 MB file pushed RSS to 321.5 MiB.
5. **Upload grows with the square of the turn count.** Every request re-sends the system prompt, all tool schemas and the full transcript.
   - The request grows 237 B/turn for echo turns, 2.25-2.49 KB/turn for mixed tools, and 558 tokens/turn with a reasoning model (because reasoning is re-sent).
   - Totals: 300 mixed turns sent 108.9 MB.
   - pi uses one keep-alive TCP connection per process and closes it 4.0 s after the last response.
6. **pi always runs 12 threads.** Each tool call is a full `fork()` of the Node process, and its cost on the main thread grows with RSS (≈0.34 → 0.67 ms as RSS goes 145 → 354 MiB).
   - grep and find re-run blocking `--version` probes on every call.
   - N tool calls in one message start N processes with no limit: 16 calls started 16 processes within 12-33 ms.
7. **Disk: sessions only grow, and spill files are never cleaned up.**
   - The session JSONL is 7.1 KB plus about 0.84 KB per small turn plus 1.06 × the tool output. It is append-only, never fsync'd, and never shrinks.
   - Any bash output over 50 KB / 2,000 lines is written in full to `/tmp/pi-bash-*.log` and never deleted (55.8 MB from 3 calls in 07).
   - json-mode stdout repeats each tool result 4-7 times.
8. **The interactive TUI is expensive at realistic token rates.** Streaming at about 55 chunks/s, the TUI used 184.8 ms CPU/s against 35.3 ms/s for json (4.8× for one 12k-char answer).
   - The TUI's rate climbs as the message grows (139 → 230 ms/s), and its RSS goes from 165 to 291 MiB. json stays flat.
9. **At scale, an instant model makes CPU the limit; a real model makes memory the limit.**
   - 32 synchronized instant-model agents on 18 vCPU: agent life went 352 → 1,364 ms and run-queue wait 10 → 1,064 ms. Per-agent CPU rose 36-55% for identical work, while memory pressure stayed at 0.
   - At real-model rates (about 28 ms CPU/s per agent), memory runs out long before CPU.
10. **Safety and robustness hazards:**
    - A timeout SIGKILLs the process group, but root children under sudo, setsid children and background jobs all survive pi.
    - `echo pw | sudo -S` defeats the "no TTY" barrier.
    - The full environment, including secrets, reaches every tool.
    - Retries ignore `Retry-After`, and pi exits 0 after giving up.
    - Debian's `fdfind` breaks the find tool (89 of 89 calls failed).

---

## PART 1 — The resource model in one page

### 1.1 Fitted formulas (verified versions)

| Quantity | Formula | Coefficients (verified) | Validity / fit | Runs |
|---|---|---|---|---|
| Startup CPU per process | S | 246.5 ms one-turn json chat (n=13); 164.5 ms `--version` (n=5; 164.0 warm, n=4). +6 to +44 ms when the start follows ≥1.1 s of idleness, which includes every first start in a container. +236 ms on the first TypeScript-extension load (jiti transpile), +3-12 ms when cached. TUI: first request at 318.65 ms vs 170.9 ms headless, +78-95 ms CPU before the request, and more deferred startup after it (R3: 328 vs 214-226 ms CPU from exec to first byte). | medians; exact eBPF | 01, 03, 23, 11, 21, R1-R3 |
| Agent CPU per tool turn, instant model | c | 5 ± 0.5 ms/turn for echo turns (04: 5.00 raw, 4.78-4.91 after removing the start penalty; 23: 5.07). Split per turn: main 3.29 + V8Worker 1.69 + libuv 0.02 ms. Mixed tools: 7.45 ms/turn (05). | (process CPU − 246.5)/turns; probe included | 01, 04, 05, 23 |
| Growth of per-turn CPU with context | c(k) = a + b·req_KB(k) | pooled a = 5.38 ± 0.87 ms, b = 0.0090 ± 0.0024 ms/KB (R² 0.22, n=53 windows). Per-run KB slopes differ 7× (0.0065-0.049). Equivalent: about 5.5 → 9 ms/turn as the transcript grows 50 → 550 KB (+0.5-0.8 ms per 100 KB). | rough. In-sample ±15%; out of sample 12-compaction is predicted −20% (433 vs 540 ms) | 04, 05, 14 |
| pre_request (build + serialize) | a + b·turn | 04: 0.625 ms + 3.78 µs/turn (R² 0.70; buckets are convex, 0.66 → 1.34 ms). 05 before compaction: 0.652 ms + 7.68 µs/turn (R² 0.47). 14: 0.837 ms + 15.0 µs/turn (R² 0.52). Per message: 1.9 / 3.8 / 7.5 µs. | probe adds about 0.9 ms/turn to absolute values | 04, 05, 14 |
| Fork cost on pi's main thread per spawn | k0 + k1·RSS | k0 ≈ 103-120 µs, k1 ≈ 1.56-1.60 µs per MiB of agent RSS (338/515/671 µs at 145/279/354 MiB in 05) | mock loops only; real-model forks were 5× slower at the same RSS (R1 median 1.81 ms at ~146 MiB) | 05, 14, R1 |
| Tool-child CPU per call | — | bash 0.80-0.95 ms (2.6 MiB). grep: 2 processes, `rg --version` 1.03 + search 3.94 ms p50 (17.6 ms for a whole-tree search). find via Debian fdfind: 3 processes, 2.09 ms (always fails). Working fd: 35.7-43.5 ms (20 threads). read/ls/write/edit: 0. Child CPU ×1.8 at N=32. | exact eBPF | 03, 04, 05, 16, 13 |
| Agent CPU per tool turn, in-process tools | ≈ 1.09 × turn latency | ls 4.6, read 4.7, edit 5.1, write (2.1 KB args) 10.7 ms | valid only for in-process tools; for bash/grep/find, tool_ms is mostly time waiting on children | 05, 03 |
| Streaming CPU per chunk | s_mode | Full-speed burst: json/rpc 27 µs, print 16 µs, TUI 34 µs. Paced at 53-55 chunks/s: json 0.45-0.57 ms; TUI 3.1 ms on average, rising from about 2.5 to 4.2 ms over a 12k-char message. Real provider: 0.17 ± 0.09 ms per token event on top of about 18 ms/s while a stream is open (1-s-bin OLS, n=78). | n=1 per mode | 11, 21, R1 |
| TUI CPU rate during paced streaming | r = a + b·kchars | 122.1 ms/s + 10.06 ms/s per kchar already streamed (R² 0.78, 52 bins); json 39.3 − 0.96·kchar (flat) | n=1, 0-12k chars; elapsed time is collinear with length | 11 |
| Real-model CPU per turn | a + b·turn_wall_s | 22.3 ms + 20.7 ms per wall-second (r² 0.956, n=24); without the 2 longest turns, 8.9 + 28.4 ms/s. Median turn 40.2 ms (R1), 61.5 ms (R2). | use about 20-30 ms per streaming second (probe included) | R1, R2 |
| Waiting / idle CPU | w | Lone json agent waiting on an idle VM: 11.8-14.1 ms/s. Same wait with 16 agents running: 4.9 ms/s (bimodal). With the VM busy: 3.3 ms/s. Idle TUI / RPC: 13.6 / 12.9 ms/s (11.5 / 11.8 without spikes). | all of these are dominated by the probe; pi's own idle cost is likely about 0 (inferred) | 19, 09, R1, 13, 08, 02, 18 |
| Concurrency CPU inflation | f(N) | ×1.36 / 1.39 / 1.43 at N=4/16/32 against the cold N=1 run (431 ms); ×1.48 / 1.51 / 1.55 against a warm estimate of about 397 ms. Run-queue wait added per agent: 18 → 26 → 43 ms (convex). runq/CPU 0.11 / 0.63 / 1.73. | one synchronized burst per level | 13 |
| Peak RSS | max(floor, 134.2 + 0.407·T + 0.182·req_KB) | se 2.8 / 0.034 / 0.014, R² 0.956, n=113. 72% of the samples are from 14 and they are autocorrelated, so the se is optimistic. Use a floor of about 157 MiB for T ≤ 30. Per-run slopes: 0.33 (04), 0.81 (05), 0.80 (14), 1.09 MiB/turn (12, only 4 samples). | measured peaks within ±8% | 04, 05, 14, 12 |
| Container memory per concurrent agent | 18.3 + 101.7·N MiB | anonymous memory; file pages flat at 6-8 MiB | r²=1.000, N=1/4/16/32 (30-turn tasks) | 13 |
| cgroup tasks (pids.max budget) | 2.6 + 11.2·N | 12 Node threads per pi process | r²=0.9999 | 13 |
| Session JSONL on disk | 7,125 + d·(T−1) + 1.061·Σ tool_result_chars B | d = 826-843 B for small bash turns; 1,067 ± 72 B pooled for mixed turns | within 3% on 05/12/14; never shrinks | 01, 04, 05, 12, 13, 14, 23 |
| Temp spill disk | Σ full output of bash calls > 50 KB / 2,000 lines | 50,499,999 + 4,052,632 + 1,288,895 B (07) | exact; never deleted | 07 |
| Request body for turn k | B0 + g·k | 04: 5,593 B + 237.0 B/turn (R² 0.99998). 05: 6,063 + 2,493.5 B/turn before compaction (R² 0.9998). 14: 6,874 + 2,250 B/turn. Fixed part: system prompt 2,578 B + 4 tool schemas 2,841 B (or 2,680 + 4,971 B with 7 tools). Tool output enters the request at 1.03-1.08× its size (JSON escaping). | exact linear | 04, 05, 14 |
| Total upload | Σ = n·B0 + g·n(n−1)/2 | 05: 107,709,720 B predicted vs 107,709,523 B measured over the 292 pre-compaction requests | exact to 0.0002% | 05 |
| Real-model request / response bytes | tx = 4.108·prompt_tokens − 215 B; rx ≈ 132·output_tokens − 1,630 B | prompt grows 558 tokens/turn (r² 0.93); tx +2,275 B/turn; rx averaged 127 B per output token | r² 0.998 / 0.996 | R1 |
| Minor page faults | F0 + f·tool_turns | F0 ≈ 25.0k at start; f = 800-1,240 per tool turn (04 803, 13 881, 23 1,141, 05 1,235, 14 1,217); 0 major faults ever | fork copy-on-write is the inferred cause | 01, 04, 05, 13, 14, 23 |
| Syscalls | S0 + s·turns | S0 ≈ 6.3k (5.7k pi + ≈535 lab runc). s ≈ 139 across all threads per bash turn (04, now complete data); ≈ 160-180 main-thread syscalls per mixed turn after removing the probe's writes | count per turn is flat; time per turn grows | 01, 04, 05, 14 |

**Session CPU, composed:**

CPU(T) ≈ S + Σₖ c(k) + Σ_chunks s_mode + w·wait_s + Σ child CPU

With an instant model the c(k) term dominates. With a real model the streaming and waiting terms dominate (Part 3.8).

### 1.2 Measured per-agent footprints

| Profile | Turns | Agent CPU | Tool-child CPU | Peak RSS | Session file | Request bytes sent | Run |
|---|---|---|---|---|---|---|---|
| one-turn chat | 1 | 246.5 ms (n=13) | 0 | 146.5 MiB (bimodal to 157.7) | 7,125 B | 5,702 B | 01-startup |
| short bash loop | 20 | 348.0 ms (n=10, probe on) / 332.5 (no probe) | 16.6 ms | 156.6 MiB | 23,635 B | 167.8 KB | 23-probe-overhead |
| bash echo loop | 200 | 1,245.6 ms | 164.4 ms | 210.1 MiB | 175,635 B | 5.89 MB | 04-loop-200 |
| mixed tools (589-file repo) | 300 | 2,481.9 ms | 414.6 ms | 372.7 MiB | 948,235 B | 108.9 MB | 05-loop-mixed-300 |
| long RPC session, 6 prompts | 246 | 2,487.6 ms | 339.7 ms | 336.3 MiB | 710,736 B | 69.5 MB | 14-rpc-session |
| real model, json, fixed 3 bugs | 17 | 2,154.2 ms over 78.8 s | 67.9 ms | 156.2 MiB | 73,280 B | 0.35 MB tx / 0.70 MB rx (TLS) | R1-fix-tests-json |

Evidence:
- `SELECT cpu_ms,life_ms,peak_rss_kb FROM procs WHERE harness='pi' AND run=? AND role='agent'` → 04: 1245.6 / 1117.1 / 210.1 MiB; 05: 2481.9 / 2369.9 / 372.7; 14: 2487.6 / 8274.6 / 336.3.
- `v_cpu_by_role` → tool:shell 164.4 (04); tool:search 367.3 + tool:shell 47.3 (05).
- `mock_requests` sums → 5,887,591 / 108,888,812 / 69,494,533 B.
- `runs.disk_usage` → session files 175,635 / 948,235 / 710,736 / 73,280 B.

### 1.3 Sizing: agents per vCPU and memory per agent

| Workload | CPU demand per agent (agent + tools) | Agents per vCPU at 100% | Agents per vCPU at a 60% target | Memory per agent |
|---|---|---|---|---|
| Instant model, tight tool loop (13, N=1) | 1.30 cores average, 1.54 peak | 0.77 | about 0.3. At N=16 on 18 vCPU, run-queue wait was already 63% of CPU time | ~100 MiB private |
| 400 ms model latency, 30-turn loop (13, measured at N=16) | 29.6 ms/s at steady state (agent 26.6 + shells 3.0) | ≈34 | ≈20 | ~100 MiB |
| Real model, json (R1) | 28.2 ms/s (27.3 agent + 0.9 tools; includes ~9-13 ms/s of probe) | ≈35 | ≈14 with ×1.5 contention (derived: 600/(28.2×1.5)) | 156 MiB RSS, ≈98 MiB PSS-anon |
| Parked idle RPC / TUI | ≤12.9-13.6 ms/s (probe-dominated) | ≥74 | ≥44 | 145-148 MiB RSS, ~96 MiB private |
| TUI streaming at ~55 chunks/s | 184.8 ms/s (rising with message length) | 5.4 | ≈2.2 with ×1.5 | 291.5 MiB |

Memory for N agents (13-concurrency, 30-turn loops, json):

| N | container memory.peak | cgroup tasks | peak cores (100 ms) | median cores | median agent life | median turn latency |
|---|---|---|---|---|---|---|
| 1 | 123 MiB | 13 | 1.54 | 0.86 | 352 ms | 4.40 ms |
| 4 | 420 MiB | 47 | 5.70 | 2.92 | 554 ms | 8.19 ms |
| 16 | 1,649 MiB | 183 | 16.14 | 10.75 | 806 ms | 13.21 ms |
| 32 | 3,272 MiB | 359 | 18.52 | 13.56 | 1,364 ms | 21.93 ms |

**Capacity on this VM (derived arithmetic from the table above):**
- MemAvailable was 9.28 GiB before the bursts.
- At 101.7 MiB per agent that fits about 90 concurrent short agents.
- At about 323 MiB private per 300-turn mixed session (372.7 MiB RSS minus about 50 MiB of shared pages) it fits about 30.
- CPU at R1's 28 ms/s would allow hundreds. So with real models, memory runs out first.
- With an instant model, CPU saturates at about 14 synchronized agents on 18 vCPU.

Evidence:
- 13 groups (procs, start gap > 1.5 s) → CPU medians 431.4 / 586.9 / 600.2 / 615.2 / 653.6 ms, runq 9.6 / 64.2 / 379.2 / 1063.8 / 153.1 ms, life 352.3 / 553.7 / 806.1 / 1363.8 / 12979.8 ms.
- `SELECT MAX(peak)/1048576.0, MAX(pids) FROM cgroup_samples WHERE harness='pi' AND run='13-concurrency' AND container='lab-agent'` → 3271.6, 359.

### 1.4 What dominates

- **Short tasks:** process start dominates. A single one-turn task is about 2/3 bundle loading (`--version` 164.5 of 246.5 ms). Keep agents long-lived (rpc) and stagger starts.
- **Instant-model loops:** pi's own JS (main thread plus V8 background threads) dominates. Tool children are 9-14% of non-lab CPU in the loops (04: 164.4 vs 1,245.6 ms). The kernel takes 21-29% of agent CPU as system time (fork, writes, socket setup).
- **Real-model sessions:**
  - Wall time is the model's (99.49% in R1).
  - pi's CPU follows streaming wall-seconds (≈20-30 ms/s) and is light in absolute terms (2.7% of a core).
  - Memory dominates capacity.
- **Real work in tools:** the children dominate. In 08 the tools used 8,324.9 ms against pi's 307.3 ms (96.4%). In 10, one `sudo apt-get update` used 829 ms against pi's 319.7 ms for the whole run.

### 1.5 What scales with conversation length

| Cost | Scaling | Measured |
|---|---|---|
| Request bytes per turn | linear in the transcript (every request re-sends it) | +237 B/turn (echo), +2.25-2.49 KB/turn (mixed), +558 tokens/turn (real reasoning model) |
| Total upload | quadratic | 5.89 MB (200 echo turns), 69.5 MB (246 rpc turns), 108.9 MB (300 mixed turns) |
| pre_request latency | linear-to-convex in message count | 0.77 → 1.34 ms (04); 0.82 → 3.96 ms (14); reset by compaction (05: 2.50 → 0.99 ms) |
| Agent CPU per turn | +0.5-0.8 ms per 100 KB of transcript | about 5.5 → 9 ms/turn from 50 → 550 KB |
| Fork cost per tool spawn | grows with RSS | 338 → 671 µs (05) |
| RSS | ratchets up; not returned by compaction or idle | 0.33-0.81 MiB/turn |
| Session file | linear; never shrinks, even after compaction | 12: 1.57 MB after 31 turns while each request stayed ≤155 KB |
| Syscall count per turn | flat (time per turn grows) | 191 / 173 / 178 main-thread syscalls per turn across 05 |
| Session append bytes per turn | flat | 3.0-3.3 KB/turn in 05 |
| GC count per turn | flat to falling (V8 enlarges the young generation) | 04: 11.9 GCs per 100 turns throughout |

### 1.6 Hidden costs to budget for

1. **Startup on every process:** ≈250 ms CPU and ≈150 MiB as measured, with the compile cache broken by the image (about 26 ms CPU and 14 MiB less when it works; Part 5.3, fix 4). A TS extension adds +236 ms on its first load in each container (the jiti cache lives in `/tmp`).
2. **A full fork per tool call.** The fork blocks the event loop for 0.3-0.7 ms in mock loops and up to 2.5 ms with a real model. grep and find also run uncached, blocking `spawnSync('--version')` probes on every call (05: 150 probes, 244.6 ms of blocking).
3. **Quadratic upload,** including re-sent reasoning tokens. On top of that, cache-warmer replays re-upload the whole last request.
4. **json stdout volume:** 4-7× the tool output, plus an `agent_end` line holding the whole transcript (873.5 KB in 05; 1.49 MB in 12). Whatever consumes stdout must absorb this.
5. **Disk that is never cleaned:** spill files in `/tmp`, sessions that only grow, the jiti cache, and the downloaded fd binary (once per container).
6. **Page cache charged to the container.** Tool file I/O counts against container memory (08: file cache 6.2 → 197.0 MiB, and memory.current stayed at 208.2 MiB after pi exited).
7. **Orphaned processes.** Background jobs, setsid children and root children under sudo outlive timeouts and pi itself.
8. **Reconnects.** A new TCP (and TLS) handshake follows any pause over 4 s, such as long tools, human think-time or cache-warmer pings. pi does no DNS caching.
9. **Retries.** Up to 2 + 4 + 8 = 14 s added per failed turn, followed by exit code 0.
10. **TUI streaming CPU** that grows with message length, plus about 140-150 ms of extra startup latency.
11. **Subagents are full processes.** Each costs about 100 MiB and about 400 ms CPU, with its own TCP connection.
12. **Concurrency inflates per-agent CPU** by ×1.4-1.5 for the same work.
13. **The lab's in-process probe** (lab only): +4.7% CPU on busy runs and about 9-13 ms/s while idle. Remove it from production estimates.

---

## PART 2 — Step by step: where every resource goes in a pi run

### 2.0 Timelines

**Cold one-turn json run.** Medians from 01-startup, n=13. t=0 is when runc forks the agent task.

```
t ms   0      2.2         14.6                                  147.3        164.6  165.0                191.8
       |runc  |node/V8    |  bundle load + top-level eval        | main():     |first |mock  | response,        |
       |(lab) |bootstrap  |  (~110 ms, ~144 ms CPU,              | config,     |turn  |reply | session write,   |
       |      |12.4 ms    |  main ELU 1.0 + 4 V8Worker)          | runtime     |build |0.38  | exit 26.1 ms     |
       |      |           |  4.51 MB JS from page cache          | (PI_TIMING  |17.25 |      | (18.6 ms / 25.4  |
       |      |           |  helper 'MainThread' exits ~69 ms    | 21-25 ms)   |ms    |      |  ms CPU after    |
       |      |           |                                      |             |      |      |  'exit' event)   |
CPU:   0.6 + ~14.7 ms     ~144 ms                                 ~58 ms (config + turn; 100 ms granularity)   25.4 ms
Net:   7.4 KB unix (lab)  0                                       DNS 25/48 B; TCP 6,385 B up / 10,124 B down
```

**Steady-state bash turn.** 04-loop-200, medians, n=200, mock answers instantly. The phase medians sum to 3.97 ms; the median turn is 4.09 ms and the mean 4.26 ms.

```
t ms   0          0.96        1.42        1.98                          3.72        3.97
       |-pre_req--|--model----|-dispatch--|-----------tool--------------|-post_tool-|
       | rebuild  | writev    | final SSE | fork() 0.36-0.39 ms on main | 2 session |
       | session  | 5.6 KB +  | parse,    | thread; bash child 0.82 ms  | appends   |
       | projection 237 B×k   | message_  | CPU / 0.87 ms life / 2.6 MiB| (842 B),  |
       | ×2, scan | body;     | end, arg  | output 8 B over a unix      | events to |
       | tools,   | ~1.5 KB   | parse     | socketpair; wait4           | stdout    |
       | serialize| SSE back  |           |                             | (~4.6 KB) |
CPU per turn: 5.0 ms = main 'pi' 3.29 + 4×V8Worker 1.69 + libuv 0.02  (pi uses ≈1.1 cores while looping)
Syscalls per turn: ≈139 all threads (≈117 on the main thread, incl. ≈13 probe writes); 1 clone, 1 writev, 1 wait4,
                   2 socketpair, ~8 setsockopt; ≈27 voluntary context switches; ≈800 minor faults
```

**Real-model turn.** R1, medians, n=17.

```
t ms   0     2.2          ~229                           ~1,556            +1.4    +7.4     +0.7
       |pre  |--TTFB------|------token streaming---------|                |disp-  |tool    |post|
       |     | pi asleep, |  ~70-90 tok/s, one wakeup per |                |atch   |(ls/sed |    |
       |     | ELU 0.0066 |  delta; 91% are thinking_delta|                |       | 2-4 ms |    |
       |     | 9 ms/s     |  14.6 ms/s median CPU         |                |       | CPU)   |    |
local work per turn: 12.2 ms median   |   model: 1,556 ms median (p90-ish 7,168, max 29,768)
```

Evidence:
- `SELECT pre_request_ms, model_ms, dispatch_ms, tool_ms, post_tool_ms, turn_ms FROM turns WHERE harness='pi' AND run='04-loop-200' AND idx>0` → medians 0.96 / 0.46 / 0.56 / 1.74 / 0.25 / 4.09, mean turn 4.26.
- `v_thread_cpu` 04 → pi 825.0, V8Worker 408.3, libuv 5.0 ms.
- The 01 timeline comes from the probe-<nspid>.jsonl start/event/end markers joined to procs and mock_requests (verifier: 14.57 / 132.67 / 17.25 / 0.38 / 26.1 ms).
- R1: `turns` medians 2.20 / 1,556 / 1.42 / 7.38 / 0.71; local total 12.23 ms.

### 2.1 Step 1 — exec and bootstrap (lab `docker exec` → runc → node)

**Mechanism:**
1. `docker exec` makes runc fork a task inside the container.
2. The `runc:[2:INIT]` Go stage runs inside the future agent pid (lab machinery).
3. `#!/usr/bin/env node` execs node.
4. Node bootstraps V8, then loads the probe through `--require`.

| Resource | Measurement |
|---|---|
| Wall | 2.24 ms runc init before node exec (lab) + 12.37 ms node exec → probe loaded (10.57-18.62). Host-side docker exec latency is a further 51.3 ms per chat, outside the VM's cgroups (lab). |
| CPU by thread | 15.3 ms for the whole 0 → 14.6 ms window. Of that, 0.59 ms is on 5 Go `runc:[2:INIT]` threads (lab, charged to the agent pid); the rest is the main thread, named `node` at this point. Per docker exec, runc:[0:PARENT] costs 0.84 ms and runc:[1:CHILD] 0.31 ms (lab). |
| Memory | The Go runtime's 10.3 MB is dropped at exec. |
| Syscalls | runc stage ≈533 (rt_sigaction 114, futex 39, mmap 34). Thread `node`: 257 (rt_sigaction 31, mmap 27, fcntl 20). |
| Files / bytes | 7 shared libraries, openssl.cnf 12.3 KB, cgroup memory.max / memory.high, /proc/meminfo. 9 runc opens (/etc/passwd, /proc/self/*) are lab. |
| Network | none by pi. runc reads a 7,450-7,570 B docker-exec bootstrap over a unix socket (lab). |
| Processes / threads | 1 pid. Node adds DelayedTaskScheduler, 4 V8Worker and SignalInspector here. |

**Interpretation:** this step is mostly lab plumbing plus fixed Node cost. When reading `procs.cpu_ms` for a docker-exec'd agent, subtract about 0.6-2 ms for runc. `procs.threads` reads 13 or 17 because it counts the 5 runc threads.

Evidence:
- `procs WHERE run='01-startup' AND role='lab:docker-exec'` → PARENT n=22 median 0.84 ms; CHILD 0.31 ms.
- `threads WHERE run='01-startup'` → 5 × runc:[2:INIT] ending at ~2.7 ms.

### 2.2 Step 2 — bundle load: cli.js → cli-runtime.js → eager chunks

**Mechanism:** V8 parses, compiles and evaluates pi's bundle:
- cli.js (160 B) and cli-runtime.js (660 B);
- 8 eager chunks led by `chunk-OJP47DM6.js` (4,345,609 B, read in a single `read()`);
- `@earendil-works/chord/dist/context/index.js` (3,276 B).

pi's `cli.js` calls Node's `enableCompileCache()`, but in every lab run the call failed silently. `npm install -g` ran as root at image build and left `/tmp/node-compile-cache` owned by root (mode 755), so pi, running as `agent`, got "Cannot create cache directory: permission denied" and compiled everything from scratch. No pi process ever opened the cache. Part 5.3, fix 4 measures the difference.

| Resource | Measurement |
|---|---|
| Wall | about 110 ms (probe start → first pi event is 132.7 ms, which includes about 22 ms of main()) |
| CPU by thread | about 144 ms in the first probe sample (145.2 ms CPU over 114.2 ms wall): the main thread at ELU 1.0 plus 4 V8Worker threads compiling in parallel (≈1.3 cores). `--version` total: main 122.8 ms, V8Worker 35.1 ms. A short-lived helper thread named `MainThread` uses 2.5 ms and exits a median 69 ms after node exec. |
| Memory | `--version` peak RSS 116.5 MiB. RSS 122 MB at the first probe sample (heap 20.5 MB used). |
| Syscalls | main thread while named `MainThread`: ≈1,349 (futex, mprotect, brk, mmap, statx). The memory-management calls are mostly on V8Worker (mprotect 854 of 1,153 in 17-strace). |
| Files / bytes | 11 files, 4,505,505 B, all from page cache (read_bytes 0, majflt 0 for all 211 agent processes). Startup opens excluding runc: 56 opens / 43 paths (`--version`), 78 / 60 (chat). |
| Network | 0 |
| Processes / threads | 1 process. 8 Node threads until the first async fs or DNS operation starts the libuv pool. |

**Interpretation:** this is the biggest fixed cost pi has, and PI_TIMING never sees it. A working compile cache removes about 40 ms of main-thread compile work (26-30 ms of process CPU). The remaining ≈64 ms of main-thread time is pi's own top-level module evaluation, which no cache removes: about 20 ms of chunk top-level code, TypeBox schema building and validator compilation (`new Function`), every built-in provider constructed eagerly, and marked's regexes. Fewer config files would barely help. All 69 agents in 13 re-read 328.1 MB of bundle from page cache between them.

Evidence:
- `procs` → `--version` n=5 CPU 160.9 / 163.5 / 164.5 / 165.3 / 181.6 ms, life median 126.5 ms, RSS 116.45 MiB.
- `file_io` → chunk-OJP47DM6.js 4,345,609 B in 1 op; 4,505,505 B over 11 files.
- `opens WHERE file LIKE '%compile%'` → only a lab `du`.
- Post-report check (throwaway `lab-agent-pi` container, as `agent`): `node -e 'require("node:module").enableCompileCache()'` → `{ status: 0, message: 'Cannot create cache directory: permission denied' }`; `ls -la /tmp/node-compile-cache` → `drwxr-xr-x root root`.
- V8 CPU profiles of a one-turn chat (3 runs each): Node's module loader incl. compile 54.3 ms (cache broken) vs 14.7 ms (cache working); `chunk-OJP47DM6.js` self time 64.5 vs 64.4 ms.

### 2.3 Step 3 — config, runtime and session objects (`main()`)

**Mechanism:** `main()` does the following:
- parses arguments and runs migrations;
- builds the AgentSessionRuntime (resource discovery, model runtime);
- reads settings.json under a proper-lockfile lock, which is taken even for reads;
- reads models.json, auth.json and themes.

The session file is not created yet: it exists only in memory until the first response arrives.

| Resource | Measurement |
|---|---|
| Wall | PI_TIMING TOTAL 21 / 22 / 25 ms (n=3): createAgentSessionRuntime 17-18, parseArgs 1-5, runMigrations 0-1, createSessionManager 1, createRuntime 0; extension factory 1-2. This is 10.6-12.9% of process life. |
| CPU by thread | Not separable at the probe's 100 ms granularity. Derived: chat − `--version` = +82 ms CPU (main +45, V8Worker +35.3, libuv +1.1); about 21 ms of that is extra exit CPU, which leaves ≈61 ms for runtime creation plus the first turn. |
| Memory | +30 MiB between `--version` and a one-turn chat (116.5 → 146.5 MiB) |
| Syscalls | `pi` thread: statx 800, faccessat 96 (resource discovery and existence probes; paths not traced). Lock dirs: about 7-10 mkdirat and 5 unlinkat (lock/unlock cycles) per agent. |
| Files / bytes | settings.json 4-5 opens (245 B). models.json read 4 × 998 B via libuv (the first async fs call starts the 4-thread pool). auth.json and models-store.json 2 B each. dark.json + light.json themes 2,215 + 2,236 B, loaded even in json mode. |
| Network | 0 in json / print mode, even online (the 5 online chats are byte-identical to the 5 offline ones). The online TUI calls pi.dev here; see 2.11. |
| Processes / threads | +4 libuv-worker → 12 threads. 0 children in json mode; 7 helper processes in the TUI. |

**Interpretation:** prediction 2 holds only inside PI_TIMING's own scope. createAgentSessionRuntime dominates main(), but main() is about 11% of the process's life. With many agents sharing one config dir, the settings lock busy-waits in 20 ms spins on contention (Part 3.6).

Evidence: `cat lab/out/pi/runs/01-startup/stderr-chat-timing-{0,1,2}.txt` → `createAgentSessionRuntime: 18ms / 17ms / 18ms; TOTAL: 22ms / 21ms / 25ms`.

### 2.4 Step 4 — turn build and request serialization (`pre_request`)

**Mechanism** (docs/ARCHITECTURE.md §3, §6.1):
- `declareToolChanges` scans the whole transcript.
- `buildSessionProjection()` runs twice: once for next-turn refresh and once for request projection. Each run walks the branch twice internally.
- A token estimate is computed.
- The whole transcript plus all tool JSON schemas are serialized into one request body.

The provider path used here (openai-completions) is not incremental.

| Resource | Measurement |
|---|---|
| Wall | First turn 10.35 ms. That is ≈9.4 ms above steady state, while 10 lazy chunks (257,336 B; the openai-completions provider plus 9 chunks) are read. Steady state 0.96 ms (04) and 1.73 ms (05), growing per the fits in 1.1. Real model (R1) 2.20 ms median. |
| CPU by thread | Main thread, CPU-bound (ELU 0.90-0.93 in the loop). Allocation churn feeds V8Worker GC work. |
| Memory | Each turn allocates a transcript-sized body; large strings land in large-object space (up to 29-40 MB of floating garbage in 05/14). |
| Syscalls | negligible; the work is in user space |
| Files / bytes | none |
| Network | none yet. Body size: first 5,702 B (01); 04 grows 5,727 → 53,091 B; 05 reaches 727,074 B. |
| Processes / threads | main thread only |

**Interpretation:** this is the O(n)-in-transcript cost. It is milliseconds even at 550-700 KB, so with a real model it is under 1% of turn time. Its real price is network bytes and tokens (2.5).

Evidence:
- 05 pre_request bucket medians (turns 1-49 … 292-300) → 0.83, 1.20, 1.57, 1.99, 2.31, 2.50, 0.99 ms.
- `mock_requests` 04 → 201 requests, 5,887,591 B, min 5,727, max 53,091.

### 2.5 Step 5 — model request and stream

**Mechanism:**
- One `writev` per request (05: 301 writev for 302 requests). The undici keep-alive pool reuses one TCP connection while gaps between requests stay under 4.0 s.
- SSE deltas are parsed, pushed into context, and emitted as `message_update` events. Each delta is awaited through every listener.
- json mode writes one stdout line per delta.

| Resource | Measurement |
|---|---|
| Wall | First request: TTFB 7.07 ms, total 7.49 ms (includes the DNS lookup to 127.0.0.11 and the TCP connect). Steady state with the mock: TTFB 0.43 ms, total 0.46 ms (04). Real model (R1): TTFB median 227 ms; model median 1,556 ms; two reasoning turns took 29.8 s and 18.8 s (72.7 and 87.9 tok/s). |
| CPU by thread | Mostly main thread. Full-speed burst: json/rpc 27 µs per chunk, print 16 µs. Paced at 53 chunks/s: json 0.45-0.57 ms per chunk (each chunk is its own wakeup: 2,956 socket reads for 3,000 chunks, against 30-34 reads for 10,000 chunks at full speed). Real provider: 0.17 ± 0.09 ms per token event plus about 18 ms/s while the stream is open. |
| Memory | During TTFB waits RSS sits on a plateau (R1 142-148.5 MiB) |
| Syscalls | epoll_pwait / read per chunk. R1: 58% of syscalls were epoll_pwait, mostly the probe's 5 ms timer during the wait. |
| Files / bytes | json stdout: one line per delta (R1: 698,386 B in 1,842 writes ≈ 127 B per output token) |
| Network | Mock SSE: 187-283 B per chunk (46-47 B per character). Real model over TLS: 127 B per output token on average, about 47 socket reads per response. 1 DNS lookup per new connection (25 B out / 48 B in), with no DNS cache. |
| Processes / threads | 0. A second connection opens only when an aux request overlaps (compaction summary). |

**Interpretation:** at mock speed this step is free. With a real provider it is the whole wall time, and pi pays CPU for every separately-arriving delta. Plan CPU by streaming seconds or events, not by turns.

Evidence:
- `turns JOIN http` 01 → first-turn TTFB median 7.07 ms, dur 7.49 ms.
- `net_bytes` pid 74220 → tcp_tx 6385, tcp_rx 10124, udp 25/48.
- 05 `syscalls name='writev' thread='pi'` → 301.

### 2.6 Step 6 — tool dispatch and spawning

**Mechanism:**
- Tool calls run in parallel through `Promise.all` with no limiter.
- bash, grep and find spawn through libuv as a plain `fork()`: `clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD)`, no CLONE_VM or CLONE_VFORK. The kernel therefore copies the page tables of the whole 150-370 MiB Node address space.
- The child is detached (setsid) and gets stdin `/dev/null` and stdout/stderr on unix socketpairs.
- Its environment is the full `process.env` plus 7 pi variables, with pi's bin dir first on PATH.

| Resource | Measurement |
|---|---|
| Wall | dispatch 0.56 ms (04), 0.63 ms (05). Write-tool turns 5.67 ms (134 streamed argument chunks, ≈39-42 µs per chunk). 16 parallel calls: dispatch 7.56 ms (≈0.8 ms per spawn, serial on the main thread). |
| CPU by thread | Main thread: clone 338 µs at 145 MiB RSS, 515 µs at 279 MiB, 671 µs at 354 MiB (05). 04: 127 clones averaging 359 µs, then 76 averaging 391 µs. In 05, clone was 151.7 ms (6.1%) of 2,482 ms agent CPU. Real model: R1 median 1.81 ms per fork. |
| Memory | The copy-on-write fork makes the parent take faults on pages it touches afterwards: about 800-1,240 minor faults per tool turn. A child that fails exec reports the parent's mm (57.6-135.5 MB of "RSS") without allocating it. |
| Syscalls | Parent per spawn: 1 clone, 2 socketpair, 1 pipe2, about 8 setsockopt, about 1.3 shutdown, 1 wait4 (06: 96 socketpair, 48 pipe2, 394 setsockopt for 48 spawns). Forked child before exec: 43 syscalls (29 rt_sigaction resets, setsid, execve). execvp walks PATH with 3-6 failed execve per spawn. |
| Files / bytes | `--version` probes for grep/find on every call: rg 1.97 ms avg (n=95), fd 1.46 ms (ENOENT), fdfind 1.46 ms |
| Network | none |
| Processes / threads | 1 process per bash call; 2 per grep; 3 per failing find; +2 socket fds per live child (06 peaked at 33 sockets = 1 TCP + 32) |

**Interpretation:** spawning is the hidden scaling cost of long, tool-heavy sessions. The fork gets slower as pi's heap grows and it blocks the event loop. Installing rg and fd into `PI_CODING_AGENT_DIR/bin` removes the per-call probes, because `getToolPath()` checks that dir with existsSync first and does not memoize system lookups.

Evidence:
- `grep 'clone(child_stack=NULL' lab/out/pi/runs/17-strace/strace-trace.txt` → `clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD, ...) = 92`.
- `SELECT t, SUM(count), SUM(total_ns)/1e3/SUM(count) FROM syscalls WHERE harness='pi' AND run='05-loop-mixed-300' AND thread='pi' AND name='clone' GROUP BY t` → 89 @ 338.0 µs, 136 @ 514.7 µs, 77 @ 670.8 µs.
- `spawn_sync` 05 → rg 50 × 1.97 ms, fd 50 × 1.46, fdfind 50 × 1.46.

### 2.7 Step 7 — each tool type at the OS level

| Tool | Latency p50 (n) | OS processes per call | Child CPU / life / RSS / threads | Main-thread blocking | I/O | Result to model |
|---|---|---|---|---|---|---|
| ls (7 entries) | 0.57 ms (95) | 0 | – | – (getdents and about 10 sequential statx on libuv) | – | 64 chars |
| read, small file | 0.61 ms (95) | 0 | – | – | 534 B read for a 267-B file | 267 chars |
| read, 19.5 MB file | 19.3 / 20.5 / 21.9 ms (3) | 0 | – | libuv: 117 reads of ~500 KB | 58,585,800 B for 3 calls | 51,200 chars each; pi peak RSS 321.5 MiB |
| write, 2 KB | 0.63 ms (89) | 0 | – | dispatch 5.38-5.67 ms (argument streaming) | 1 write of 2,000 B | 40 chars |
| edit | 1.80 ms (3) | 0 | – | – | read + rewrite of a ~50-B file | 60 chars |
| bash `echo` | 1.86 ms (505 echo-only); tool_ms 1.74 (04, 200) | 1 (`/bin/bash -c`; bash execs the last simple command) | 0.82 ms / 0.87 ms / 2.6 MiB / 1 thread; 194 minor faults | fork 0.34-0.67 ms | child rchar 4.3 KB; 145 syscalls (43 pre-exec + 103 bash) | ≈8 B over 3 socket reads |
| grep (rg), 589-file repo | 5.03 ms (95) | 2: `rg --version` (spawnSync, 1 thread, 1.03 ms) + `rg --json` (7-13 threads, 3.94 ms p50) | 4.96 ms combined, 5.4 MiB | spawnSync 1.97 ms + 2 forks | search reads 226 KB, writes 54 KB of JSON | 10,567 chars; at 100 matches pi sends SIGTERM to rg (50/50 in 05, 42/42 in 14) |
| grep, whole /workspace (03) | 9.3-10.0 ms | 2 | rg 16.5 ms CPU in 4.97 ms, 13 threads, reads 5.04 MB | 2.2 ms spawnSync | | 2,521 chars |
| find via Debian fdfind 8.x | 4.75 ms (89), all errors | 3: forked `fd` attempt (ENOENT, comm `pi`) + `fdfind --version` + `fdfind --no-require-git …` (exit 2) | 2.09 ms combined; real fdfind 3.0 MiB | 1.46 + 1.46 ms spawnSync | | 252-char error text |
| find via downloaded fd 10.5.0 (16) | 9.8 / 9.3 ms warm; first call 1,672.7 ms (download) | 1 fd | 35.7-43.5 ms CPU / 7.1-8.6 ms / 2.5 MiB / 20 threads | none once cached; tar 28.7 ms once | first call: 3 HTTPS connections, ~1.43 MB | 23,662 chars |
| `sudo -n id` (NOPASSWD) | 6.8 ms | bash exec'd sudo → id as root | sudo 3.31 + id 0.53 ms; 3.4 / 1.7 MB | – | sudo opens 58-65 distinct files (sudoers, PAM, /etc/shadow) | uid=0 |
| `sudo cat /etc/shadow` (password needed) | 4.5 ms | 1 sudo | 3.15 ms CPU / 3.12 ms life | – | audit line in sudo.log | error: "a terminal is required" |
| `echo pw \| sudo -S head /etc/shadow` | 17.1 ms | bash + subshell + sudo + head | sudo 14.65 ms / 14.9 ms life, 19.8 MB (most likely the yescrypt hash; inferred) | – | | 40 chars of /etc/shadow |
| `sudo apt-get update` | 1,015 ms | 94 processes (max 11 concurrent; 81 of them as uid 42 `_apt`) | 829.27 ms total: store 484.9, apt-get 207.75 (45 MB), http 42.35 | – | 9.29 MB downloaded over plain HTTP :80; 29.4 MB write_bytes (page cache dirtied; cgroup io_wbytes 9.3 MB) | 11 chars |

**Interpretation:**
- In-process tools (ls, read, write, edit) cost only pi's per-turn CPU.
- Spawned tools add a fork plus child CPU, and grep/find also add uncached blocking probes.
- The model's choice of command decides the cost: one `apt-get update` outweighs hundreds of pi turns.

Evidence:
- `tool_calls` latency by tool over 03/05/14 → ls 0.57, read 0.61, write 0.63, edit 1.80, bash 2.12 (all), grep 5.03, find 4.75.
- `procs` 04 tool:shell → 200 × 0.822 ms CPU, 0.872 ms life, 2,668 KB.
- 10-sudo subtree recursive CTE → n=94, CPU 829.27 ms, write_bytes 29,421,568.

### 2.8 Step 8 — result handling and session persistence

**Mechanism:**
- **Completion:** a bash call completes on shell exit plus a 100 ms idle-grace timer that re-arms on late output.
- **Truncation:** output beyond 50 KB or 2,000 lines is truncated to its tail, and the full output is streamed to `/tmp/pi-bash-*.log`.
- **Persistence:** each finished entry is appended with `appendFileSync` (open with O_APPEND, write, close), and no fsync is ever issued. The session file is created with O_CREAT|O_EXCL only after the first model response.
- **json mode:** events are written to stdout.

| Resource | Measurement |
|---|---|
| Wall | post_tool 0.25 ms (04), 0.23 ms (05), 0.88 ms for 48 KB results (12). Fixed +100 ms per call when a descendant keeps stdout open (09: 104.2 / 104.6 ms calls for 2.1 / 2.8 ms shells). |
| CPU by thread | Main thread (synchronous appends). Spill writes run on libuv-worker threads (07: libuv CPU 45.6 ms vs about 1.4 ms normally). |
| Memory | Big stdout is streamed, not buffered: with 55.8 MB of tool stdout, pi's RSS stayed ≤3 MiB above comparable tool runs (159.5 vs 156.5-159.0 MiB). |
| Syscalls | 2 open/write/close cycles per turn (05: 596 opens for 602 writes). 0 fsync / fdatasync / sync_file_range / msync in every run; 04 and 09 are now fully covered after the data fix. |
| Files / bytes | Session file: 842.5 B/turn (04), ≈3.1 KB/turn (05), ≈52 KB/turn (12). json stdout: ≈4.6 KB / 18.5 writes per echo turn (04: 952,152 B / 3,801 writes over 201 turns). 05: 19-20 KB/turn (6.64 MB in 11,974 writes against 935 KB of session file). The final `agent_end` holds the whole transcript (05: 873,506 B). |
| Network | none |
| Processes / threads | Orphans that hold the stdout socket survive (2.8 / 3.4). |

**Interpretation:**
- Persistence is cheap per turn and flat, and it is not crash-safe (no fsync).
- In json mode the stdout stream, not the session file, is the big local writer. Every tool result appears in tool_execution_end, message_start, message_end and turn_end, and again in agent_end.

Evidence:
- `file_io` 04 agent writes (complete after the fix) → TCP 5,918,223 B / 200 ops; fifo 952,152 / 3,801; probe-29.jsonl 267,861 / 2,628; session 173,100 / 400 ops.
- `SELECT run, name, SUM(count) FROM syscalls WHERE harness='pi' AND name IN ('fsync','fdatasync','sync_file_range','syncfs','sync','msync') GROUP BY run, name` → no rows.

### 2.9 Step 9 — growth over a long session

**Mechanism:**
- pi keeps the whole session tree in memory forever (docs/ARCHITECTURE.md §5.1).
- Per-turn allocation grows with the transcript: projection rebuild, message conversion and body serialization.
- V8 responds by enlarging the young generation (new_space 8 → 64 MB in 04, → 128 MB in 05/14), and freed pages are not returned to the OS.

| Run / turns | pre_request median | Request body | Agent CPU per turn | RSS (MiB) | Heap used / total (MB) |
|---|---|---|---|---|---|
| 04, turns 1-20 | 0.77 ms | 5.7 → 10 KB | 4.1 ms | 146 | 25 / 40 |
| 04, turns 151-200 | 1.34 ms | 40 → 53 KB | 5.1-7.0 ms | 188-210 | 39-49 / 100-102 |
| 05, turns 1-49 | 0.83 ms | 8 → 152 KB | 5.0-5.5 ms | 151-178 | 27-38 / 41-69 |
| 05, turns 200-249 | 2.31 ms | ≈530-560 KB | 8.2-12.7 ms | 331-359 | 63-100 / 168-198 |
| 05, turns 250-290 | 2.50 ms | ≈710 KB | 6.5-10.9 ms | 341-369 | 65-106 / 169-199 |
| 05, turns 292-300 (after compaction) | 0.99 ms | 97 → 115 KB | 8.5 ms | 345.6 (single sample; no drop) | 59 / 173 |
| 14, prompts 1 → 6 | 0.96 → 3.89 ms | 7.8 → 549 KB | prompt CPU 280 → 450 ms | 175.7 → 336.0 after each prompt | 41.4 → 76.0 (peak 100.7) / 65 → 201 |

**GC** (relabelled after data fix 1):

| Run | minor | major | incremental | GC time per turn |
|---|---|---|---|---|
| 04 | 30 (21.09 ms) | 2 (2.47 ms) | 2 (0.26 ms) | 0.12 ms |
| 05 | 42 (36.69 ms) | 5 (12.19 ms, max 3.60 ms) | 5 (0.86 ms) | 0.17 ms |
| 14 | 36 (32.4 ms) | 6 (16.74 ms, max 5.19 ms) | 6 (1.34 ms) | – |

The old-generation live set is only 19.5-22.4 MB. Large-object space holds up to 29-40 MB of mostly floating garbage. All growth is anonymous memory (14: PSS anon 248.5 MB of RSS 301.2 MB).

| Resource | Measurement |
|---|---|
| Wall | pre_request +0.40 / +0.66 / +1.51 ms per 100 turns (04 / 05 / 14); dispatch, tool and post_tool stay flat |
| CPU by thread | Main thread 66-73% of agent CPU; V8Worker 25-33% (04: 408 of 1,246 ms) |
| Memory | +0.33 (04), +0.81 (05), +0.80 (14) MiB/turn. Full GCs return 10-28 MiB at a time, so RSS ratchets rather than rising strictly. |
| Syscalls | main-thread count per turn flat (05: 191 / 173 / 178); non-wait syscall time per turn 1.62 → 1.87 → 2.33 ms |
| Files / bytes | session append rate flat, 3.0-3.3 KB/turn (05) |
| Network | tx per turn grows: 05 one-second buckets show 116 KB → 390 KB → 574 KB per turn |
| Processes / threads | unchanged: 12 threads, 1 TCP connection |

**Interpretation:**
- Long sessions are a memory problem first (≈0.3-0.8 MiB/turn, never returned) and a network problem second (quadratic upload).
- CPU and latency growth is real but small.
- Budget about 350-400 MiB per long-lived agent with a 0.5-0.7 MB transcript, unless the V8 heap is capped.

Evidence:
- `gc` → 04 minor 30 / 21.09, major 2 / 2.47, incremental 2 / 0.26; 05 minor 42 / 36.69, major 5 / 12.19 (max 3.6), incremental 5 / 0.86; 14 major 6 / 16.74 (max 5.19).
- probe_samples RSS against cumulative turns → 04 0.335 MiB/turn; 05 0.987 between endpoints at turns 9-220 (OLS 0.81); 14 0.798.

### 2.10 Step 10 — compaction

**Mechanism:**
- Compaction triggers when the estimated context exceeds contextWindow − 16,384 tokens (reserveTokens). The estimate is the last assistant usage plus chars/4 of everything after it.
- It keeps the newest ≤20,000 estimated tokens and sends one tool-less summarization request. Tool results in that request are truncated to 2,000 chars.
- Near the limit, max_tokens is clamped to contextWindow − estimate − 4,096.

| Resource | 12-compaction (64k window) | 05-loop-mixed-300 (200k window) |
|---|---|---|
| Trigger | 9 compactions in 31 turns, every 3rd turn. tokensBefore 50,346, 50,775, 50,759, then 50,761 × 6, each > 49,152 (the first is exactly 38,246 usage + 48,400/4). | 1 compaction after turn 291: 184,579 > 183,616. max_tokens clamped from turn 286 (15,221) down to 14,186 at turn 291. |
| Wall | 4.81-7.70 ms per compaction (median 5.64) against the full-speed mock; with a real model, one full LLM completion | 7.52 ms |
| Summary request | 7,820 B, then 10,021-10,083 B; no tools; 311-B system prompt; max_tokens 8,192; reply 1,553 chars | 235,561 B (58,891 input tokens) |
| Request before → after | 152.9 KB (7 messages) → 56.5 KB (3 messages) | 727,074 B (583 messages) → 97,427 B (65 messages) |
| pre_request around it | 0.68 / 0.73 / 0.84 ms at 3 / 5 / 7 messages (first cycle excluded) | 2.39-2.85 → 0.87-1.13 ms |
| Memory | RSS 147 → 156 → 174 MiB, rising monotonically | RSS fell 369.3 → 341.4 MiB at a full GC 92 ms *before* compaction; the single post-compaction sample is 345.6 MiB |
| Network | a 2nd TCP connection opened for the overlapping summary request (at 207.1 ms; compaction ended at 205.4 ms) | 2nd SYN at 2,269 ms (compaction ended at 2,266.8 ms) |
| Disk | session file 1,566,744 B, about 10× the largest request | 948,235 B |

**Interpretation:** compaction bounds request bytes, pre_request time and the CPU growth driven by context size. It does not bound RSS or the session file. Each compaction is an extra model call; with real models that means seconds of latency and real tokens.

Evidence:
- `SELECT t, aux, step, req_bytes, n_messages, n_tools, sys_bytes, max_tokens FROM mock_requests WHERE harness='pi' AND run='12-compaction' ORDER BY t` → main requests 54,813 / 103,867 / 152,921, then aux 7,820 B (n_tools 0, sys 311, max_tokens 8192), then 56,530 B.
- compaction_end payloads in `events-compact.jsonl` → tokensBefore 50,346, estimatedTokensAfter 13,902.

### 2.11 Step 11 — streaming output modes

**Mechanism** (docs/ARCHITECTURE.md §6.3):
- print and json/rpc never build a TUI tree. json/rpc do one `JSON.stringify` per event; print writes the final text once.
- The TUI re-lexes the Markdown of the accumulated streaming message on every ~16 ms frame and writes changed lines to the PTY.

| Mode (11/21, one process each) | Stream | Agent CPU total | main / V8Worker (ms) | Life | Peak RSS | Output written | TCP reads |
|---|---|---|---|---|---|---|---|
| print (-p) | 40k chars in 10k chunks, burst | 400.2 ms | 261.5 / 130.8 | 273 ms | 191.3 MiB | 40,036 B in 37 writes | 34 |
| json | same | 512.1 ms | 372.3 / 132.0 | 384 ms | 185.4 MiB | 2,682,915 B in 10,050 writes | 30 |
| rpc | same | 512.6 ms | 368.3 / 132.2 | 1,377 ms (long-lived) | 186.2 MiB | identical event stream (2,682,811 B in 10,014 lines; eBPF file_io undercounted rpc) | 30 |
| TUI | same | 707.9 ms (n=2: 673.3), of which 648.3 / 635.8 ms active | 466.2 / 223.4 | 4,127 ms (includes idle until C-c) | 257.7 / 244.8 MiB | 528,374 B to the PTY in 55 writes | 34 |
| json | 12k chars, 3k chunks at 53.1 chunks/s (nominal 60) | 2,167.7 ms (35.3 ms/s while streaming) | 1,933.5 / 225.9 | 56.7 s | 156.4 MiB | 817,853 B in 3,014 lines | 2,956 |
| TUI | same, 54.6 chunks/s | 10,437.6 ms (184.8 ms/s, quintiles 139 → 160 → 180 → 209 → 230) | 8,964.9 / 1,453.1 | 59.1 s | 291.5 MiB (165 → 291 while streaming) | 1,907,810 B to the PTY in 9,729 writes (constant ≈34 KB/s, 636 B per delta) | 2,971 |

| Resource | Measurement |
|---|---|
| Wall | TUI first request at 318.65 ms vs 170.9 ms headless; paced TUI ELU 0.159 mean (max 0.634) |
| CPU by thread | TUI rendering runs on the single main thread (86%). V8Worker: 1,453 ms (6.4× json). GC (relabelled): TUI paced 193 minor + 14 major + 14 incremental = 233 ms of pauses; json 19 + 2 + 2 = 15.6 ms. |
| Memory | TUI young generation 16 → 128 MB; PSS-anon 101 → 241 MB (json 94-95 MB) |
| Syscalls | paced TUI main thread ≈258 writes/s and 568 epoll_pwait/s; json 71 writes/s and 437 epoll_pwait/s. V8Worker futex 215/s vs 28/s. |
| Files / bytes | Session file is the same in every mode: 48,843 B for 40k chars (≈1.05 B/char + 6.9 KB fixed). Stdout/PTY volume differs 67×. |
| Network | identical in every mode: 1,872,688 B received for 40k chars; 6,389 B request |
| Processes / threads | 12 threads in every mode. The TUI also starts 7 short helpers (sh + tmux display-message, a failed fd fork, fdfind, rg, 2 tmux queries; ≈6.4-6.8 ms CPU), which are labelled tool:* although no tool call ran. tmux server (the terminal emulator): 311.7 ms (3% of pi). |

**Interpretation:**
- json and rpc cost the same. print is 22% cheaper because it skips the per-delta event writes.
- At burst delivery the TUI premium is mostly startup (+78-95 ms pre-request CPU), and only +48 ms at 80k chars.
- At realistic pacing the TUI's per-frame cost grows with the message (main thread ≈1.7 → 4.1 ms per frame). At a fixed token rate its total CPU is therefore super-linear in message length (inferred from one run).

Evidence:
- `SELECT pid, cpu_ms, life_ms, peak_rss_kb/1024.0, argv FROM procs WHERE harness='pi' AND run IN ('11-stream-modes','21-tui-scaling') AND role='agent'` → 512.1 / 2167.7 / 400.2 / 512.6 / 707.9 / 10437.6 / 429.3 / 505.7 / 673.3 / 1398.8 / 831.3 ms.
- `gc` probe_pid 47/171 → minor 19 / 193, major 2 / 14 (3.0 / 28.6 ms), incremental 2 / 14 (0.4 / 4.0 ms).

### 2.12 Step 12 — idle

**Mechanism:**
- pi has no timers of its own while idle in json, rpc or an idle TUI. The only handles are 3 TTYWrap (TUI) or the socket and pipes.
- The keep-alive socket closes 4.0 s after the last response.
- The optional cache warmer re-sends the last request (2.12 table below; Part 3.2).

| Resource | Measurement |
|---|---|
| Wall | – |
| CPU by thread | TUI (02): 13.6 ms/s mean, 11.3 median (11.5 mean without 5 spikes). RPC (18): 12.9 / 11.4 ms/s (11.8 without spikes). A lone json agent waiting on the model: 11.8-14.1 ms/s. All of this is the probe's timers (5 ms loop-delay histogram, 100 ms sampler); pi's intrinsic idle cost is likely about 0 (inferred: no probe-free idle run exists). |
| Memory | 02: RSS 147.4 MiB flat, PSS 125.7 MiB (anon 96.0, file 29.7); peak 161.8 MiB during startup; heap 27.4 → 29.8 MB over 25 s. In 18, V8 shrank new_space 16 → 1 MB but RSS did not fall (146 → 149 MiB). |
| Syscalls | ≈352 epoll_pwait/s, 30 reads/s, 20.8 getrusage/s, 19.9 openat/s of /proc/self/stat, 11.6 probe writes/s. Every non-epoll call is the probe's. ≈189 voluntary context switches/s. 0 TTY writes, 0 GCs (02). |
| Files / bytes | only the probe's own file. Cache-warm entries append to the session file (3 opens in 18). |
| Network | 0 after the first second (the online TUI makes 2 HTTPS calls to pi.dev at start: report-install 204 in 548.6 ms, latest-version 200 in 331.2 ms; 3,876 B tx / 12,604 B rx). Cache warmer (18): a 6,418 B replay every 20.0 s, each on a new TCP connection. |
| Processes / threads | 12 threads; the socket fd disappears while idle |

**Interpretation:** an idle pi costs memory (≈96 MiB private, ≈147 MiB RSS) and essentially nothing else. In this dataset 13 ms/s is an upper bound set by the instrumentation.

Evidence:
- 02 `syscalls` pid 75018, t in [S+3000, S+28000) → epoll_pwait 8826 (353/s), getrusage 521, openat 497, write 291.
- `opens` → only /proc/self/stat (496).
- `http` 02 → `/api/latest-version 200 331.2 ms`, `/api/report-install 204 548.6 ms`.

### 2.13 Step 13 — exit

| Resource | Measurement |
|---|---|
| Wall (Node 'exit' event → kernel exit) | 18.6 ms (01 chats), 20.0 (04), 13.4 (05), 3.5 (03), 4.4 (10), 5.5 (12), 4.3 (23), 3.2 (11); 0.7 ms for `--version`. Cause of the variation not identified. |
| CPU | 25.4 ms (01 chats), 25.0 (04), 18.0 (05), 6.8-9.8 (03/10/12/23/11), 4.3 (`--version`); all main-thread teardown |
| Memory | Part of the peak RSS is set after the 'exit' event: eBPF peak minus in-process maxRSS at exit is 0.7-12.4 MB (median 3.4 MB) |
| Syscalls | no flush or sync calls |
| Files / bytes | none (no fsync) |
| Network | FIN on the model connection; TIME_WAIT accumulates on the agent side because pi always closes first (13: up to 69 TIME_WAIT) |
| Processes / threads | all threads end within ~1 ms of each other (V8Worker 0.6-0.8 ms before the process). Untracked descendants keep running (Part 3.3). |

Evidence:
- `procs.end_ms − probe_procs end.t` and `procs.cpu_ms − (end userMs + sysMs)` → 01 chats 18.62 ms / 25.41 ms; `--version` 0.74 / 4.30.

---

## PART 3 — Deep dives by theme

### 3.1 Startup, threads and measurement method

Runs: [01-startup](http://localhost:8765/explorer/#/h/pi/run/01-startup), [17-strace](http://localhost:8765/explorer/#/h/pi/run/17-strace), [23-probe-overhead](http://localhost:8765/explorer/#/h/pi/run/23-probe-overhead), [02-idle-tui](http://localhost:8765/explorer/#/h/pi/run/02-idle-tui).

1. **Cost of one short invocation** (n=13): 246.5 ms CPU over 191.8 ms wall (1.29 cores on average).
   - Peak RSS 146.5 MiB, bimodal: 9 runs at 142.3-147.0 MiB and 4 at 155.3-157.7 MiB. The online-vs-offline RSS gap reflects this bimodality, not network use.
   - Reads 4.85 MB; writes 51.7 KB via write syscalls. Of the writes, 34.3 KB is stdout events, 7.1 KB the session file, 6.4 KB the TCP request and 3.6 KB the probe file; 11.8 KB reaches storage.
   - 25,034 minor faults, 583 voluntary context switches.
2. **Penalty after idleness.** A start that follows ≥1.1 s of idleness costs more: chat-offline-0 252.9 ms, chat-online-0 265.3, chat-timing-0 262.7, against a median of 246.5.
   - Every first start in a container is such a case: 01 `--version` 181.6 vs 164.0; 03 307.5 vs 289.8/291.2; 23 390.7 vs 347.1.
   - It is not a compile-cache effect (the cache never worked in these runs; Part 5.3, fix 4). The cause was not identified.
3. **TypeScript extension (jiti).** The first `-e subagent/index.ts` load cost 482.4 ms CPU and 212.9 MiB, and wrote 42,710 B to `/tmp/jiti` after reading babel.cjs (1,526,691 B). The next two loads cost 249.3 and 258.5 ms. With fresh containers, every container pays the transpile again unless `/tmp/jiti` is pre-warmed or persisted.
4. **Threads.** 206 of 206 working agent processes run exactly 12 threads; the 5 `--version` processes run 8. The 4 subagents (now `agent:nested`) also run 12.
   - The threads are: main `pi` (68.2% of a chat's CPU), 4 V8Worker (28.6%), 4 libuv-worker (1.13 ms total), DelayedTaskScheduler (0.20 ms), SignalInspector (2.53 ms) and a helper that inherits the name `MainThread` (2.52 ms; exits a median 69 ms after node exec, range 66-86).
   - The probe sees 11 live threads.
   - pi creates no worker_threads (0 probe `worker` events). Scale comes from processes, not threads.
5. **Thread names split the main thread's syscalls.** The main thread is named `node` right after exec, `MainThread` during bootstrap and `pi` after `process.title`, so syscalls keyed by thread name split it into phases. Chat medians: node 257, MainThread 1,349, pi 1,963, V8Worker 1,985, libuv 189, runc 532; total 6,278.
6. **Measurement completeness.**
   - eBPF per-process CPU matches the cgroup cpu.stat delta within 0-1.5% in all 26 runs with an agent (e.g. 13-concurrency 45,781.6 vs 45,795.0 ms; 02 797.6 vs 810.1).
   - 10,899 thread exits have 0 missing memory-at-exit events.
   - eBPF counted 11,681 syscalls where strace -c counted 11,674 for the same tree (+0.06%; read 2,078 = 2,078, statx 1,252 = 1,252).
7. **strace: trust counts, not times.** futex is 64.4% of strace's "time" (1.82 s) but is idle blocking; pi's CPU was only 0.43 s.
   - Under strace, pi's CPU rose 47% and its life 2.4× (census run), and rg's CPU rose 5.9×.
   - execve 27 with 19 failures is PATH walking; connect 6 with 5 "errors" are nscd attempts plus one EINPROGRESS non-blocking connect.
8. **Probe overhead** (23, n=10 vs 10):
   - +15.6 ms CPU (+4.7%), or +13.9 ms excluding the cold first run; +29 ms life (+12.3%); +0.87 ms per turn of harness gap (4.17 vs 3.30 ms).
   - +293 writes to the host bind mount (main-thread write time 14.14 vs 2.27 ms); +319 voluntary and +51 involuntary switches.
   - Main-thread run-queue wait 6.3 vs 0.015 ms.
   - Peak RSS unchanged (156.6 vs 157.1 MiB). The probe arm ran first.

### 3.2 Long conversations and memory

Runs: [04-loop-200](http://localhost:8765/explorer/#/h/pi/run/04-loop-200), [05-loop-mixed-300](http://localhost:8765/explorer/#/h/pi/run/05-loop-mixed-300), [12-compaction](http://localhost:8765/explorer/#/h/pi/run/12-compaction), [14-rpc-session](http://localhost:8765/explorer/#/h/pi/run/14-rpc-session), [18-cache-warmer](http://localhost:8765/explorer/#/h/pi/run/18-cache-warmer).

1. **Upload grows quadratically.**
   - Bodies grow exactly linearly: 237.0 B/turn (04), 2,493.5 B/turn (05 before compaction), 2,250 B/turn (14).
   - Totals are 110.9×, 149.8× and 123.6× the final request.
   - The fixed system + tool bytes are 18.5% of upload in 04 but only 2.1-2.7% in realistic runs, where tool results dominate (537,307 B of the 727,074 B final request in 05).
2. **Per-turn overhead climbs but stays in milliseconds.** Agent CPU per turn goes about 5.5 → 9 ms as the transcript grows 50 → 550 KB. The fit's R² depends on how turns are bucketed; the magnitude does not.
   - At equal message count, rpc pre_request was about 1.6× json's (3.85 vs 2.31 ms at 400-499 messages). The cause is not isolated, because the two runs used different tool patterns.
3. **Memory follows V8 heap sizing under allocation churn, not retained transcript.**
   - RSS vs transcript KiB: 05 135.2 + 0.350 MiB per KiB (R² 0.96, pre-compaction samples only), 14 136.9 + 0.362.
   - 04 has 1.427 MiB per KiB because its messages are small and many. So churn per turn matters, not bytes.
   - Earlier text said there were "no full mark-compact events". That was wrong: the relabelled GC table shows 2 / 5 / 6 major GCs in 04 / 05 / 14.
4. **A long-lived RPC session keeps every MiB it grows.**
   - Across 6 prompts, RSS after each prompt was 175.7 → 199.9 → 230.6 → 288.4 → 299.7 → 336.0 MiB.
   - None of the 48 GCs ran in the five 1 s idle gaps, and RSS and heap were flat to 0.1 MB during the gaps.
   - Prompt wall time grew 268 → 416 ms.
5. **Cache warmer** (18; 30 s TTL, `cacheWarming=idle`):
   - **Cadence:** 3 replays at +20.033, +40.041 and +60.048 s, at intervals of 20.008 / 20.008 / 20.007 s, i.e. `max(1, floor(min(0.9·TTL, TTL−10 s)))`.
   - **Payload:** each replay is the last request with only `max_tokens` changed to 1 (6,418 vs 6,421 B).
   - **Network cost:** each replay made a new TCP connection (pi closed the previous one 4.0 s after the last response), sent 7,101 B and received 1,730 B, and cost +14.1 / +7.0 / +7.6 ms CPU.
   - **Gating:** warming is cost-gated: expected savings 0.15·(miss − hit) − warmCost must be ≥ $0.05. Here it was $0.0905 at deliberately high prices; the break-even is about 889 prompt tokens.
   - **Default mode:** the default `streaming` mode never warms while idle.
6. **json stdout repeats every tool result.**
   - 12: 10.1 MB of events for 1.45 MB of tool output (6.9×); tool_execution_update is 25.5% of it.
   - 05: 6.7 MB, 04: 0.96 MB.
   - agent_events agent_end sizes: 6,462 B (01, plain chat), 132,572 (04), 873,506 (05), 1,490,022 (12).

### 3.3 Tools and processes

Runs: [03-each-tool](http://localhost:8765/explorer/#/h/pi/run/03-each-tool), [06-parallel-tools](http://localhost:8765/explorer/#/h/pi/run/06-parallel-tools), [07-big-output](http://localhost:8765/explorer/#/h/pi/run/07-big-output), [08-cpu-child](http://localhost:8765/explorer/#/h/pi/run/08-cpu-child), [09-timeout-orphans](http://localhost:8765/explorer/#/h/pi/run/09-timeout-orphans), [16-fd-download](http://localhost:8765/explorer/#/h/pi/run/16-fd-download).

1. **Children use the CPU when tools do real work.**
   - 08: children 8,324.9 ms (python3 5,005.2; head 341.8; 8 parallel sha256sum 2,974.4) against pi's 307.3 ms (96.4% vs 3.6%).
   - pi used 3.2-3.3 ms CPU/s while waiting (ELU 0.003). That is lower than the ≈13 ms/s seen on an idle VM: wait cost depends on host load.
2. **Parallel calls are not throttled.**
   - 06: 16 calls per message started 16 detached bash processes within 32.5 / 13.1 / 12.2 ms. Each round took 1,049.9 / 1,030.1 / 1,033.5 ms for a 1 s sleep.
   - At most 32 tool processes ran at once (16 bash + 16 sleep). The sleep and python3 inside each call run one after the other, so they never overlapped.
   - pi CPU per parallel call is ≈2.6-3.1 ms after adjustment (4.1 ms is an upper bound).
   - Total tool run-queue wait was 4.13 ms over 96 children (no contention on 18 vCPU). A smaller host would queue them, because pi sets no limit.
3. **Timeouts and orphans** (09, timeout 2 s):
   - `sleep 30` was SIGKILLed 2,001 ms after spawn and the call returned in 2,004.7 ms.
   - `sudo sleep 302`: pi SIGKILLed sudo (2,001.9 ms after spawn). The root sleep in the same process group (pgid 53) was never signalled, because a uid-1001 process cannot signal uid 0. The call returned after the timeout plus the 100 ms grace (2,105.3 ms).
   - `(sleep 300 &)` stayed in pi's group but was never targeted, because no timeout fired. `setsid sleep 301` left the group.
   - All three outlived pi (exit at +4,578 ms), were reparented to docker-init, and were removed only by the lab's pkill at +4,673 ms.
   - procs labels them `lab:harness`, so role totals miss them.
   - After data fix 2, both of pi's `kill()` syscalls now appear in `syscalls`; before, the sudo kill was visible only in `signals`.
4. **Big output.**
   - 50 MB of stdout cost pi ≈4-5 ms CPU per MB while its main thread ran about one core during the 160 ms stream. pi read 55,841,543 B from the socketpairs in 5,901 reads.
   - libuv workers wrote byte-identical spill files (2,802 writes for the 50 MB file).
   - The model received tails of 51,209 / 14,088 / 51,273 chars, and each tail is re-sent on every later request (5.9 → 126.4 KB).
5. **The read tool reads the whole file.**
   - Three back-to-back reads of a 19.5 MB file: peak RSS 321.5 MiB, about 175 MiB (≈9× the file) above a warm chat.
   - This is not a per-read factor: no sample exists between reads 1 and 2.
   - The 100 ms cgroup sampler missed the spike (anon max 103.6 MiB), but the kernel's memory.peak recorded it (363.2 MiB).
6. **fd download** (16):
   - First find: 1,672.7 ms over 3 TLS connections (github.com twice without reuse, then release-assets.githubusercontent.com). About 1.43 MB downloaded; the tar ran as a blocking spawnSync for 28.7 ms (event-loop lag 32 ms).
   - Later finds: 9.8 / 9.3 ms.
   - The binary (3,080,696 B) lives in `~/.pi/agent/bin`, so a fresh container pays the download again and needs GitHub egress.
7. **Two kill policies.** Bash timeouts use a group SIGKILL. The grep match limit uses SIGTERM to rg, which exits within about 0.04 ms. grep and find have no time limit of their own.

### 3.4 sudo and security

Runs: [10-sudo](http://localhost:8765/explorer/#/h/pi/run/10-sudo), [22-env-leak](http://localhost:8765/explorer/#/h/pi/run/22-env-leak), [09-timeout-orphans](http://localhost:8765/explorer/#/h/pi/run/09-timeout-orphans), [R2-sudo-task-json](http://localhost:8765/explorer/#/h/pi/run/R2-sudo-task-json).

1. **sudo's own cost is a small, fixed tax.**
   - NOPASSWD sudo: median 3.02 ms CPU (n=7, 2.37-7.61), about 3.4 MB RSS, and 1.87 ms from sudo's exec to the target's exec (n=7; 1.77 ms for the 5 in 10-sudo).
   - For cheap targets, sudo costs more than the command (id 0.53 ms); for apt it is noise.
   - Every use writes one line to `/var/log/sudo.log`, including failures.
2. **The no-TTY barrier is not a defence.** `echo labpass | sudo -S -p "" head -c 40 /etc/shadow` succeeded in 17.1 ms. sudo's own error text suggests `-S`.
3. **apt-get update:**
   - 94 processes spanning three uids (bash 1001; apt-get and dpkg 0; http, store and gpgv as 42).
   - 829.27 ms CPU. The single-threaded `store` decompressor used 484.9 ms (read 9.07 MB, wrote 19.3 MB).
   - 9.29 MB fetched over cleartext HTTP :80.
   - apt was 78% of the run's 1,066.2 ms of tool CPU.
   - Container memory.peak 173.9 MiB (the sample max was 153.8 MiB).
   - Everything launched under sudo (104 processes, 1,063.58 ms) was 3.33× pi's own 319.7 ms.
4. **What every tool child receives** (22):
   - The whole environment: LAB_CANARY_API_KEY, NODE_OPTIONS, PI_SESSION_FILE and PI_SESSION_ID, plus 7 pi-originated variables (the 5 PI_* that bash.ts re-adds, plus PI_CODING_AGENT and AI_AGENT).
   - stdin `/dev/null`; stdout and stderr on unix sockets; no TTY; pid = pgid = sid.
   - CapEff 0, NoNewPrivs 0 (the Docker default, which is why setuid sudo works), Seccomp 0 (the lab's `seccomp=unconfined`).
   - pi's bin dir comes first on PATH (09: execvp tried `/home/agent/.pi/agent/bin/sleep` first).
5. **R2 behaviour.**
   - The model was told it had passwordless sudo for apt-get. It still tried bare `apt-get` twice (exit 100, then a dpkg lock error), then ran `sudo apt-get install -y jq` successfully.
   - It reported `head /etc/shadow` → Permission denied honestly.
   - It was never given a password, so this run says nothing about bypass behaviour.
   - Harness overhead was 32.7 ms over 7 turns, against 12,264.6 ms of model time (98.8% of the summed turn time).

### 3.5 Streaming and the TUI

Runs: [11-stream-modes](http://localhost:8765/explorer/#/h/pi/run/11-stream-modes), [21-tui-scaling](http://localhost:8765/explorer/#/h/pi/run/21-tui-scaling), [02-idle-tui](http://localhost:8765/explorer/#/h/pi/run/02-idle-tui), [R3-explore-tui](http://localhost:8765/explorer/#/h/pi/run/R3-explore-tui).

1. **Paced streaming.**
   - TUI 10,437.6 ms vs json 2,167.7 ms for the same 12,002 chars (4.8×; the 8,269.9 ms difference is 79% of the TUI's CPU).
   - The TUI rate trends +2.17 ± 0.20 ms/s per second (n=536); its main thread went 92 → 224 ms/s.
   - json was flat or falling (41 → 29 ms/s).
   - The quadratic total, CPU(N) ≈ 559·N + 23·N² ms (N in kchars), is an extrapolation from one 12k-char run. Integrating the fitted line back over its own bins is not a validation.
2. **Burst delivery.**
   - TUI active CPU 386.2 / 450.1 / 635.8 / 879.7 ms for 10k / 20k / 40k / 80k chars, which fits 320.8 + 7.12·N (R² 0.992). The fit is fragile: without the 80k point it becomes 293.4 + 8.46·N.
   - Only about 9 / 12 / 27-30 / 21 frames were drawn during the stream.
   - The earlier claim of a "fixed 128 ms TUI premium" was invalid. The measured premium over json is +124 / +136 ms at 40k and +48 ms at 80k.
3. **Resize** (80k transcript, 4 resizes):
   - One spike each, lasting one or two 100 ms samples: 134.0 / 58.1 / 72.3 / 69.4 ms above the 13.2 ms/s idle (median 70.85); lag99 ≤58.2 ms.
   - Each was a full redraw of 638,510 / 990,367 / 562,376 / 990,367 B in 3 PTY writes (12.4 B per char at 200 columns).
   - About 50 ms of the delay before each spike is the lab's resize latency (SIGWINCH arrived 43-56 ms after each mark).
4. **Responsiveness.**
   - The paced TUI kept lag99 ≤37.5 ms.
   - On the fitted ELU trend (0.085 + 0.00267·t), the single render thread would reach 50% utilisation at about 34k chars of one message, and about 38k chars by the CPU trend.
   - Bursts block the event loop for up to 199 ms (json and rpc at 40k).
5. **TUI startup.** About 150 ms more latency (318.65 vs 170.9 ms to the first request). This includes a steady ≈108 ms gap between the terminal-features query and the fd/rg probes, during which no helper process is alive. It looks like a fixed timeout; the cause was not established.
6. **Unexplained json-80k buffering.** json-80k peaked at 302.5 MiB RSS with external 94 MB and arrayBuffers 63 MB (json-40k: 18 / 6 MB). The cause was not established.

### 3.6 Scale and resilience

Runs: [13-concurrency](http://localhost:8765/explorer/#/h/pi/run/13-concurrency), [20-lock-contention](http://localhost:8765/explorer/#/h/pi/run/20-lock-contention), [19-retries](http://localhost:8765/explorer/#/h/pi/run/19-retries), [15-subagents](http://localhost:8765/explorer/#/h/pi/run/15-subagents).

1. **CPU run-queue contention drives the slowdown.**
   - At N=32: life 3.9× (352 → 1,364 ms), turn latency 4.40 → 21.93 ms, VM 84.1% busy, PSI cpu-some 59% (cgroup 54%).
   - Memory PSI 0, I/O PSI 1.6 ms, 0 major faults, no cpu.max throttling.
   - The +1,011 ms of extra life per agent breaks down as:

     | Component | Extra time | Share |
     |---|---|---|
     | main-thread run-queue wait | +669 ms | 66% |
     | extra CPU for the same work | +152 ms | 15% |
     | other blocking | +190 ms | 19% |

   - Of the other blocking, only ≈50-70 ms is slower tool children. The rest is most likely the main thread waiting on its own queued helper threads: V8Worker run-queue wait was 356 ms per agent at N=32, GC wall time grew 9 → 40 ms, and futex calls grew 3,440 → 3,821.
   - Time to the first event: 171 → 222 → 282 → 510 ms.
2. **Same work, more CPU.**
   - Per-agent syscall medians 10,915 / 10,886 / 11,236 / 11,506 (+5%); rchar 4.89 MB at every level; about 14 GCs per agent.
   - Yet CPU rose 431 → 615 ms. Probe resourceUsage puts the rise at ≈80% user and ≈20% system time (user 324 → 471 ms, sys 100 → 130 ms).
   - Even trivial shells rose 0.86 → 1.56 ms.
   - Host-side causes (efficiency cores, clock frequency, cache sharing behind the VM) are a hypothesis.
3. **400 ms model latency** (n16lat): agents lived 13.0 s and used 654 ms CPU, 9% more than n16 despite 60% less run-queue wait. The extra is waiting cost, which is the probe (≈14 ms per agent-second).
4. **Retries.**
   - One turn-level rhythm of 2 / 4 / 8 s, at most 3 retries (actual gaps: 503 → 2,010 / 4,013 / 8,013 ms; 429 → 2,008 / 4,006; 500 → 2,012 / 4,007 / 8,008).
   - The SDK is called with maxRetries 0, and provider retries are off by default.
   - Retry-After is ignored.
   - Before each retry the failed attempt is dropped from context, so retried bodies are equal-size (5,965 B, or 5,978 B for the 429 agent).
   - Every agent keeps one TCP connection through the backoffs.
   - The 500 × 8 case gave up after 4 attempts and exited 0 with an empty stderr. An orchestrator must parse `auto_retry_end.success` instead of trusting the exit code.
5. **Subagents** (now classified `agent:nested`):
   - Each of 4 children is a full pi process (`--mode json -p --no-session --tools read,bash`), spawned within 5 ms with `detached=0`: 393.5-405.8 ms CPU, 157.1-160.3 MiB, 12 threads, 1 TCP connection.
   - Children are 71% of the tree's CPU (1,599.6 of 2,245.5 ms).
   - Container peak 575.5 MiB. Against the parent alone (164.9 MiB), that is ≈102.7 MiB per child.
   - Subagents write no session file; their ≈24 KB of writes is probe output.
6. **The subagent progress stream is quadratic.**
   - The parent wrote 5.04 MB of JSON events from 346 KB of child output (14.6×).
   - tool_execution_update events grow linearly from 4,982 to 55,241 B each (+325.5 B per update, r² 0.989).
   - The model saw only a 226-B result.
7. **Lock contention** (20, 32 agents, shared vs private config dir):
   - 16 of 32 shared-dir agents had 1-4 failed lock attempts (29 extra mkdirat + statx pairs in their first second; 0 in the private-dir burst). Main-thread CPU rose 6-14 ms per failed attempt.
   - The same signature appears in 13: n16 had 9 of 16 agents affected, n16lat 14 of 16.
   - The average lock cost is ≈14 ms per agent (≈3%), about a third of the 496.5 vs 449.2 ms median gap. The rest is run order (the shared burst ran first).
   - The nanosleep counts in that comparison belong to runc (lab), not pi.

### 3.7 Kernel I/O and network

Runs: [05-loop-mixed-300](http://localhost:8765/explorer/#/h/pi/run/05-loop-mixed-300), [18-cache-warmer](http://localhost:8765/explorer/#/h/pi/run/18-cache-warmer), [08-cpu-child](http://localhost:8765/explorer/#/h/pi/run/08-cpu-child), [13-concurrency](http://localhost:8765/explorer/#/h/pi/run/13-concurrency).

1. **Requests are about 99% of pi's network traffic.** 05 sent 107.97 MB in 304 TCP writes and received 2.16 MB (50:1).
2. **One keep-alive connection, closed 4.00-4.01 s after the last response** (undici's default; pi does not set it).
   - 08: a 5.01 s tool forced a reconnect.
   - 18: 4 connections for 7 requests.
   - R1: 1 connection for 78.6 s.
   - No DNS cache: 16 looked up github.com twice.
   - In 13 the container recorded 42 retransmits and up to 69 TIME_WAIT sockets.
3. **Disk.**
   - read_bytes = 0 for all 211 agent processes, and no block-layer reads anywhere in the VM.
   - Block writes issued by pi threads are tiny (libuv-worker 163,840 B in 05, from write-tool file overwrites).
   - The large DS entries in block_io are kworker discards during container recreation.
4. **Syscall mix, 05 main thread** (per turn): write 53.3 (of which 11.1 are probe writes), epoll_pwait 15.3, futex 15.2, read 14.8, faccessat 12.7, close 12.2, setsockopt 10.0.
   - The 302 clones are ≈150 async spawns plus ≈150 spawnSync `--version` probes. Those probes also explain io_uring_setup 149, 149 opens of `/dev/null` and 2.48 socketpairs per fork.
   - V8Worker and libuv threads show 8.7 s and 8.8 s inside futex during a 2.4 s run. That is idle waiting: their non-wait syscall time is 1.8 ms and 15 ms.
5. **Faults and context switches.**
   - 04: 5,362 voluntary / 360 involuntary switches over 201 turns; 05: 10,154 / 560 over 301 turns. That is ≈27-34 voluntary switches per turn.
   - Under N=32, involuntary switches rose to 2,300 per agent, and the average write took 22 → 217 µs while the call counts stayed flat.
   - Streaming adds faults in proportion to bytes per unit time: a full-speed 40k json stream added ≈10.5k faults, a paced stream ≈0.5k.
6. **File-descriptor inventory is static.**
   - json/rpc: 3 eventpoll, 3 eventfd, 3 io_uring, 10-11 pipes and 1 TCP socket (absent while idle).
   - TUI: 7 tty fds.
   - +2 sockets per live child.

### 3.8 Real model

Runs: [R1-fix-tests-json](http://localhost:8765/explorer/#/h/pi/run/R1-fix-tests-json), [R2-sudo-task-json](http://localhost:8765/explorer/#/h/pi/run/R2-sudo-task-json), [R3-explore-tui](http://localhost:8765/explorer/#/h/pi/run/R3-explore-tui).

1. **The harness is off the critical path.**
   - R1: 78,405 of 78,811 ms (99.49%) was HTTP to openrouter.ai (TTFB 6.42%, streaming 93.06%). All local work came to 406 ms: startup 177, between turns 214, exit 15.
   - R2: 97.45% model. R3: 98.6% of its active time.
   - The task succeeded: the lab's re-run printed `Ran 4 tests … OK`.
2. **CPU follows streaming seconds.**
   - R1: 2,154.2 ms (2.73% of a core). By phase: startup 168.5 ms, TTFB waits 248.3, streaming 1,661.0 (78.5%), between turns 35.6.
   - Median per-turn CPU: 40.2 ms (R1), 61.5 ms (R2). The two long reasoning turns (656 + 362 ms) are 47% of R1's CPU.
   - The earlier claim of "0.6-0.9 ms per token event" was refuted. The measured cost is 0.17 ± 0.09 ms per token event plus about 18 ms/s while the stream is open.
3. **The probe floor** is 9.0 ms/s (median of samples fully inside TTFB waits) to 12.2-13.5 ms/s (idle means). That is 33-49% of R1's CPU, so pi's own cost is about 1.1-1.45 s for the session (estimate by subtraction).
4. **Reasoning tokens are re-sent.**
   - Prompts grew 1,752 → 9,650 tokens (558 tokens per turn).
   - After the 2,147-token reasoning reply in turn 9, the next prompt grew by 2,237 tokens for a 282-char tool result.
   - 71.6% of the 86,293 prompt tokens were provider cache reads; 80% of the 5,519 output tokens were reasoning.
5. **Network.** 350,823 B up / 702,710 B down (eth0 incl. headers: 408,377 / 771,361) over one TLS connection, with 0 retransmits and 2 DNS datagrams.
   - In R3 the OpenRouter socket closed 4.0 s after the last response while pi kept running. Any pause over 4 s therefore costs a new TCP + TLS handshake.
6. **TUI vs json with a real model** (different prompts, so indicative only):

   | Metric | R3 TUI | R1 json | R2 json |
   |---|---|---|---|
   | Streaming CPU, mean | 52.3 ms/s | 22.7 ms/s | 31.2 ms/s |
   | Streaming CPU, median | 40.7 ms/s | 14.6 ms/s | 20.6 ms/s |
   | CPU, exec → first byte | 328 ms | 226 ms | 214 ms |
   | V8 heap used, max | 43.4 MiB | 28.3 MiB | 27.9 MiB |
   | Peak RSS | 170.2 MiB | 156.2 MiB | 157.1 MiB |

   - The TUI start also made 4 pi.dev HTTPS calls, wrote a 408,607 B models-store.json, and read CHANGELOG.md (576,138 B) and a 967,584 B chunk.
7. **Memory is flat over a real session.** Peak RSS 156.2 / 157.1 / 170.2 MiB, PSS-anon 98.5 / 96.2 / 119.6 MiB. GC in R1: 59 minor, 2 major (max 2.32 ms), 2 incremental (relabelled).
8. **Tools are cheap.**
   - R1's 16 calls spawned exactly 11 processes, one per bash call, totalling 67.9 ms (3.2% of pi).
   - bash stays as a separate process only for lists, redirections and builtins.
   - The model issued exactly one tool call per message (29 tool turns), so parallel fan-out was never exercised.

---

### 3.9 Added after the report: everything used up to the first request, and a 2-vCPU burst

These were measured after the report, in throwaway `lab-agent-pi` containers with no lab probe. A small `--require` preload subscribes to undici's diagnostics channels and records `process.resourceUsage()` (exact, and including exited threads), `process.memoryUsage()`, `/proc/self/{io,status,smaps_rollup,net/dev}` and per-thread `schedstat` at four moments. Wall time counts from Node's process start, so it excludes runc (~2 ms). Each cell gives cache broken → cache working (Part 5.3, fix 4); medians of 9 runs. Syscall counts come from 5 `strace -f -c` runs killed when the request was sent.

| Metric | until the request is sent | until the first reply is complete | whole process |
|---|---|---|---|
| Wall | 146.6 → 115.6 ms | 151.5 → 120.2 ms | 156.7 → 124.8 ms |
| CPU, all threads | 192.0 → 161.3 ms | 196.3 → 165.4 ms | 206.8 → 175.5 ms |
| ↳ main / V8Worker ×4 / libuv ×4 | 146.6 / 43.6 / 0.8 → 114.9 / 43.2 / 0.8 ms | 150.7 / 44.2 / 0.8 ms | 155.7 / 49.8 / 0.8 ms |
| RSS | 124.8 → 111.4 MiB | 124.9 → 111.7 MiB | peak 134.9 → 121.7 MiB |
| ↳ private / shared file pages | 78.1 → 64.6 / 49.7 MiB | 78.5 → 65.2 / 49.9 MiB | 90.2 → 77.1 / 49.9 MiB |
| ↳ heap used / total, external | 22.2 / 37.0, 12.9 MiB | 22.4 / 37.2, 12.9 MiB | 23.9 / 38.0, 13.2 MiB |
| Minor / major faults | 19,107 → 16,227 / 0 | 19,175 → 16,325 / 0 | 21,736 → 18,787 / 0 |
| Voluntary / involuntary context switches | 477 / 0 | 493 / 0 | 523 / 0 |
| Syscalls | 5,022 → 5,146 | – | 5,514 → 5,653 |
| rchar | 4.81 → 7.14 MB (the cache files are read too) | 4.83 MB | 4.83 MB |
| Storage reads / writes | 0 / 0 | 0 / 0 | 0 / ≈8 KB (session file) |
| eth0 tx / rx | 272 B, 4 pkts / 140 B, 2 pkts (DNS + TCP handshake) | 6,855 B / 10,462 B | 6,855 B / 10,462 B |
| Threads alive / fds | 11 / 23 | 11 / 23 | 11 / 23 |

- About 93% of a one-turn chat's CPU and 91% of its syscalls are spent before the first request is sent. RSS is already about 85% of its peak at that moment.
- Top syscalls before the request: futex 1,359, mprotect 976, statx 883 (627 of them on `~/.pi/agent/auth.json`: the credential store re-checks the file's revision on every lookup, costing 0.7-1.6 ms in total), mmap 221, munmap 184, read 173, madvise 160, brk 150, faccessat 107, openat 101.

**Burst start on 2 vCPU** (`--cpuset-cpus=0,1`, compile cache working, model latency 6 s so all agents stay alive after sending):

| Started at once | All first requests sent after | CPU per agent | Container memory while waiting | Per agent |
|---|---|---|---|---|
| 1 | 0.12 s | 156 ms | 78 MiB | 78 MiB |
| 2 | 0.19 s | 178 ms | 136 MiB | 68 MiB |
| 4 | 0.34 s | 166 ms | 257 MiB | 64 MiB |
| 8 | 0.68 s | 163 ms | 552 MiB | 69 MiB |
| 16 | 1.41 s | 170 ms | 1,134 MiB | 71 MiB |
| 32 | 3.09 s | 175 ms | 2,288 MiB | 72 MiB |

- 2 vCPU sustain about 11-12 pi starts per second. Starting N at once, all are ready after about N × 0.09 s. Per-agent CPU rises only 1.05-1.14× under the burst.
- Memory charged per agent is about 70 MiB. The node binary's page cache (about 50 MiB, shared) was already charged elsewhere, so a fresh VM pays it once on top. Up to the first request, a 2-vCPU machine with about 3.5 GiB usable holds about 50 agents, or about 105 with about 7.5 GiB. Memory, not CPU, is the limit, and per-agent memory keeps growing with every turn (Part 2, Step 9).

## PART 4 — Predictions scorecard, bugs and security

### 4.1 Predictions (docs/ARCHITECTURE.md §10)

Totals: **5 confirmed, 9 partial, 1 untested, 0 refuted.** This is also written to `lab/explorer/predictions.json`.

| # | Prediction | Verdict | Evidence | Run |
|---|---|---|---|---|
| 1 | Bare offline run: near-zero network, single short-lived process, no threads besides main | partial | 1 process and 0 children. `--version` has 0 TCP/UDP. An offline one-turn chat makes 1 DNS lookup (25/48 B) and 1 provider connection (6,385/10,124 B); online is byte-identical. But there are 12 threads (8 for `--version`), and the V8Workers carry ~29% of CPU. | 01-startup |
| 2 | PI_TIMING shows createRuntime/createAgentSessionRuntime dominating cold start | partial | createAgentSessionRuntime is 17-18 of 21-25 ms (createRuntime 0). But PI_TIMING covers 10.6-12.9% of life; the bundle load before main() (~110 ms / ~144 ms CPU) dominates. Cold disk cache was not tested. | 01-startup |
| 3 | rpc mode makes one extra HTTPS catalog refresh that print mode does not | untested | Both rpc runs were PI_OFFLINE=1 with the mock provider. Related: json never contacts pi.dev; the online TUI fetched the catalog only with a built-in provider (R3). | 14-rpc-session |
| 4 | Per-turn non-network latency climbs with length; sawtooth after compaction; session and RSS ramp | confirmed | pre_request 0.77 → 1.34 ms (04) and 0.82 → 3.96 ms (14); 05 2.50 → 0.99 ms after compaction; 12 request sawtooth 153 → 56 KB with a small latency sawtooth (0.68/0.73/0.84 ms). Session only grows. RSS ratchets (full GCs return 10-28 MiB). The amplitude is milliseconds. Two verifiers said confirmed and two partial on narrower grounds. | 05-loop-mixed-300 |
| 5 | N tool calls in one message spawn N concurrent processes, no throttling | confirmed | 16 detached bash processes within 12.2-32.5 ms; 32 tool processes at once; 2 fds per child (33 sockets). The HTTP clause does not apply (1 keep-alive connection). | 06-parallel-tools |
| 6 | Timeout kills the whole process group instantly with SIGKILL, no SIGTERM grace | partial | Only SIGKILL, ~2,001-2,002 ms after spawn, with no SIGTERM. A root child in the same pgid was not signalled and survived. setsid and background jobs are never killed. The same-uid multi-member case was not run. grep uses SIGTERM. | 09-timeout-orphans |
| 7 | sudo without NOPASSWD fails fast (no TTY) rather than hang | confirmed | Failed calls of 4.5 ms ("a terminal is required") and 3.5 ms (`-n`); NOPASSWD succeeded; no hang. Separately, `sudo -S` with a piped password bypasses the barrier. The git-clone half was not tested. | 10-sudo |
| 8 | >50 KB / 2,000-line output spills to pi-bash-*.log files that accumulate | confirmed | 3 of 3 spill files (50,499,999 + 4,052,632 + 1,288,895 B) remained after exit (/tmp 1.42 → 57.26 MB). Long-run growth is inferred from that persistence. | 07-big-output |
| 9 | Missing rg/fd triggers one GitHub download, then cached | confirmed | One download (1,672.7 ms, ~1.43 MB), then 9.8 / 9.3 ms finds with no network. Corrections: 3 TLS connections, not 1; only fd tested; an incompatible fdfind prevents any download. | 16-fd-download |
| 10 | Cache warmer sends ~1-token requests every ~0.9×TTL while idle | partial | Every min(0.9·TTL, TTL−10 s) = 20 s for a 30 s TTL. Only with cacheWarming=idle and savings ≥ $0.05. Each replay re-uploads the full last request on a new TCP connection. Horizons untested. | 18-cache-warmer |
| 11 | 429/503 show two nested retry rhythms | partial | 2 / 4 / 8 s turn rhythm exact, max 3 retries; no fast transport layer by default; Retry-After ignored; exit 0 after giving up. | 19-retries |
| 12 | Shared config dir causes a lock busy-spin, gone with a private PI_CODING_AGENT_DIR | partial | 16/32 shared-dir agents had 1-4 failed lock attempts (0 with private dirs), costing ~14 ms per agent (~3%). Most of the 47 ms gap is run order. The analysts split; this follows the scale-resilience verifier's baseline-corrected analysis. | 20-lock-contention |
| 13 | TUI streaming CPU grows super-linearly vs ~linear in json/rpc | partial | Paced: the TUI rate rises 139 → 230 ms/s within a 12k message while json is flat, so the total is super-linear (n=1). Burst: linear (exponent 1.03). Large fenced code blocks untested. | 11-stream-modes |
| 14 | Resize causes a spike proportional to transcript size | partial | One sharp spike (58-134 ms CPU) and a full redraw (562-990 KB) per resize. Proportionality is untested (one transcript size). | 21-tui-scaling |
| 15 | TUI costs meaningfully more than print/json/rpc, which track each other | partial | TUI 4.8× json when paced; +24-27% at a 40k burst, +6% at 80k. print is 22% below json/rpc. | 11-stream-modes |

### 4.2 Bugs and issues

| Severity | Issue | Evidence | Run |
|---|---|---|---|
| High (bug) | **find is broken by an incompatible `fdfind` on PATH.** pi accepts Debian's fdfind 8.x because `--version` exits 0, then passes `--no-require-git`, which it rejects (exit 2). 89 of 89 calls failed, each still costing 3 processes; pi never falls back to downloading fd. | `tool_calls` find is_error 3/3, 50/50, 36/36 | 03, 05, 14 |
| High (bug/security) | **A timeout does not clean up the process tree.** The group SIGKILL cannot reach root-owned members (EPERM is silent because sudo itself was signalled). Background and setsid jobs from calls that returned normally are never tracked. Orphans outlive pi. | `signals` pi → sleep / pi → sudo sig 9 only; leftover `ps` shows root `sleep 302` | 09 |
| High (security) | **Password-gated sudo is defeated by `echo pw \| sudo -S`.** Logged like any allowed command. | 17.1 ms call returned /etc/shadow content | 10 |
| High (security) | **Full environment inheritance into every tool:** secrets, NODE_OPTIONS (code execution in any node child), PI_SESSION_FILE (read and write access to the live transcript). | `env` output of a tool child | 22 |
| Medium | **Retries ignore Retry-After** (it would retry too early with a large value). After giving up, pi exits 0 with an empty stderr. | mock gaps 2,008 / 4,006 ms; steps rc 0 | 19 |
| Medium | **Spill files are never deleted; session files never shrink or rotate.** /tmp grows by the full size of every large output. | `meta.json` disk_usage | 07, 12 |
| Medium | **The read tool loads entire files,** with a memory spike far above the 50 KB result. | peak 321.5 MiB for a 19.5 MB file | 07 |
| Medium | **Uncached, blocking `spawnSync('--version')` probes** on every grep/find call (1.5-2.9 ms each). | 05: 150 probes, 244.6 ms | 05, 14 |
| Medium | **Full-fork spawning, with cost growing with RSS** (and 5× higher in real-model runs). | clone 338 → 671 µs; R1 median 1.81 ms | 05, R1 |
| Medium | **Memory is never returned in long-lived sessions.** RSS ratchets across prompts; there is no GC and no shrink in idle gaps. | 14: 175.7 → 336.0 MiB | 14 |
| Medium | **json stdout volume** is 4-7× the tool output, and `agent_end` repeats the whole transcript. Consumers must absorb it. | 05: 6.64 MB vs a 935 KB session | 05, 12 |
| Low-Medium | **The subagent progress stream is quadratic** (5.04 MB from 346 KB). | tool_execution_update sizes | 15 |
| Low-Medium | **TUI streaming cost grows with message length** at realistic token rates. | 139 → 230 ms/s | 11 |
| Low | **The cache warmer replays the full last request** on a new connection each time. The default `streaming` mode never warms when idle. | 6,418 B per replay | 18 |
| Low | **Config-lock busy-spin** under a shared config dir (~14 ms per agent). | extra mkdirat + statx pairs | 20, 13 |
| Low | **100 ms grace per bash call** while a descendant holds stdout. | 104.2 / 104.6 ms calls | 09 |
| Info | **The online TUI contacts pi.dev at start** (report-install, latest-version, plus the catalog with built-in providers). json mode never does. | `http` table | 02, R3 |

**Measurement issues** (lab, not pi):
- Fixed: GC labels, truncated per-second tables in 04/09, and subagent role (see Part 5.3).
- Still open:
  - `tool_calls.child_procs` / `child_cpu_ms` use a ±5 ms window and double-count neighbouring calls.
  - Orphans reparented to docker-init are labelled `lab:harness`.
  - TUI startup helpers are labelled `tool:*`.
  - 3 agents in 13's N=32 burst have incomplete per-second syscall capture (statx 165 / 502 / 667 against ~920; minimum write coverage 82.7%), and 20 is ≥90.8%.
  - Per-second eBPF network sums undercount by about 1% (05/14) and by 12% for n16 in 13.
  - TUI-80k tcp_rx is exactly 64 KiB short of json-80k's.
  - eBPF file_io undercounts rpc stdout.

### 4.3 Security notes for a deployment

- **No permission gate.** pi has no tool-call permission gate and no privilege awareness (docs/ARCHITECTURE.md §8). Isolation is entirely the operator's job.
- **Keep secrets out of the environment.** Anything in pi's environment reaches every command the model runs. Use a broker or a filtered spawn hook (`BashSpawnHook`).
- **Restrict privilege escalation.** Set `no_new_privileges`, a seccomp profile, and per-command sudoers entries without passwords the model could know. Removing setuid sudo neutralises the `-S` path.
- **Bound processes at the container level, not through pi.** Use pids.max (budget 11.2 tasks per agent) and a cgroup kill on teardown. pi's own timeout cannot reap root children or detached jobs.
- **Session file permissions.** Session JSONL files use the default umask (commonly 0644), while auth.json is 0600. Tool children run as the same uid and can read or modify the live session file through `PI_SESSION_FILE`.
- **Cleartext tool traffic.** apt's traffic here was plain HTTP :80. Tool network traffic can dwarf pi's (9.29 MB vs pi's 70 KB in 10).

---

## PART 5 — Method and caveats

### 5.1 How each kind of number was measured

- **Exact** (eBPF at thread and process exit): CPU (sum_exec_runtime), run-queue wait, peak RSS (hiwater), rchar/wchar, storage bytes, faults, context switches, signals, TCP state changes, execs.
- **Per-second aggregates** (1 s granularity; a bucket's `t` marks its end): syscalls, file_io, opens, net_bytes, block_io. These cannot order events within one second. Sub-second ordering came from 17-strace, from µs probe timestamps and from mock timestamps. strace inflates wall time ~2.5× and CPU ~1.5×, so its timestamps were used only for ordering.
- **Sampled at 100 ms:** cgroup counters, /proc per process and per thread (utime/stime have 10 ms resolution), net_dev/snmp, VM. These miss short peaks: use memory.peak, not the sampled memory.current, for container peaks.
- **In-process probe:** 100 ms samples (CPU, ELU, loop lag at 5 ms resolution, heap), plus events for GC, spawn, http and agent events. heap_spaces is sampled only every 2 s.
- **Cross-checks:** eBPF CPU matches cgroup cpu.stat within 0-1.5% in 26 runs, and eBPF syscall counts match strace within +0.06%.

### 5.2 Caveats

- **Probe overhead.** Every agent ran with the in-process probe, except 17-strace and half of 23.
  - Busy runs: +4.7% CPU, +12% life, +0.9 ms per turn.
  - Waiting and idle: the probe dominates, at about 9-13 ms/s and about 200 wakeups/s from `monitorEventLoopDelay({resolution: 5})`. pi's own idle cost (likely about 0) is inferred, not measured.
  - The probe inflates main-thread runq_wait and nivcsw. Judge contention from tool children or probe-free runs.
  - It wraps stdout in json mode (a regex per line), which may account for part of the json-vs-print gap.
- **The mock streams at full speed.** Per-turn wall time in the mock loops is pure pi overhead, and the loops are CPU-bound (ELU 0.83-0.92). "Cores per agent" from those loops does not transfer to real use; per-turn CPU and memory do. Realistic per-chunk costs come from the paced runs (53-55 chunks/s delivered, not the nominal 60) and from R1-R3.
- **Small samples.** n=1 for most long scenarios (04, 05, 06, 07, 08, 09, 12, 14, 15, 16, 18, 19 each run once; R1-R3 once each, on a free model with variable latency). Medians with n>1 exist for startup (n=13), 23 (10 + 10) and the concurrency groups. Fits are within-run.
- **Container memory includes page cache.** In this warm VM, the node binary's file pages were charged to whichever cgroup first touched them, so container memory.current (e.g. 119.2 MiB idle TUI) is below process RSS (147.4 MiB). Plan on PSS/anon (≈96-100 MiB per agent), plus about 50 MiB of shared pages once per host. Tool I/O page cache does count against the container.
- **Fresh containers but warm VM page cache.** Every scenario started in a fresh container, but read_bytes was 0 everywhere, so a cold host would add real disk reads of the ≈4.8 MB bundle. Node's compile cache was broken in every lab run (Part 5.3, fix 4), so these startup numbers are what pi costs without it. Starts that follow ≥1.1 s of idleness cost 6-44 ms more for an unidentified reason.
- **One machine.** An 18-vCPU arm64 VM on an M5 Max. The ×1.4-1.5 concurrency inflation and the run-queue numbers may differ on x86 servers.
- **The find tool measured a failure path.** Debian fdfind was on PATH in every scenario except 16.
- **Units.** RSS and container memory are in MiB. Bytes on disk and the network are in decimal KB/MB. eBPF TCP bytes are payload (TLS records for HTTPS); eth0 adds IP/TCP headers.

### 5.3 Data fixes applied after the analyses (the database reflects them)

1. **GC kind labels.** Probe v1 mapped Node's `MAJOR=4` to "incremental" and `INCREMENTAL=8` to "weakcb". The gc table is now relabelled.
   - 05: 42 minor, 5 major, 5 incremental. 04: 30 / 2 / 2. 14: 36 / 6 / 6. R1: 59 / 2 / 2.
   - Durations were unaffected; only the meaning changed. Any earlier statement that "no full mark-compact GCs occurred" is withdrawn.
   - Evidence: `SELECT run, kind, COUNT(*), SUM(dur_ms) FROM gc WHERE harness='pi' GROUP BY run, kind` → `05-loop-mixed-300 major 5 12.19`, `minor 42 36.69`, `incremental 5 0.86`.
2. **Truncated per-second eBPF aggregates in 04 and 09** (the tracer block-buffered its output). Late rows have been re-attached, and the root cause is fixed in the monitor.
   - 04 agent write capture: 44.8% → 98.3%. 04 run syscalls: 51,516 → 74,504 (the agent pid alone now has 34,112). 04 agent tcp_tx is now 5,918,621 B against 5,887,591 B of request bodies.
   - 09: 76.3% → 99.7%, and both pi `kill()` syscalls are now present.
   - This report uses the refreshed values. The 04 per-turn syscall rate is now ≈139 across all threads (was ≈130 from the 123 captured turns). Zero fsync now covers the whole of 04 and 09.
3. **Subagents** in 15-subagents are now classified `agent:nested` (4 processes, 1,599.6 ms) instead of `agent:helper`.
4. **Node compile cache (found after the report was written).** pi's `cli.js` calls `enableCompileCache()`, but `npm install -g` ran as root at image build, which left `/tmp/node-compile-cache` owned by root. pi runs as `agent`, so the call failed silently in every lab run, and every pi start compiled its 4.5 MB bundle from scratch. Measured afterwards in throwaway containers (no probe, exact `wait4` rusage, medians of 15 warm runs):

   | | cache broken (all lab runs) | cache working | saved |
   |---|---|---|---|
   | `pi --version` | 152.6 ms CPU, 117.1 ms wall, 116.2 MiB | 122.8 ms, 87.6 ms, 101.8 MiB | −29.8 ms CPU (−20%), −14.4 MiB |
   | one-turn json chat | 227.9 ms CPU, 175.5 ms wall, 133.2 MiB | 202.0 ms, 149.1 ms, 119.3 MiB | −25.9 ms CPU (−11%), −26.4 ms wall, −13.9 MiB |

   - The first run with a working cache costs about 15 ms more, because it writes the cache (2.3 MB in 21 files).
   - Every startup figure in this report is therefore about 26-30 ms CPU and about 14 MiB RSS higher than a correctly installed pi. The memory-model floor drops by about 14 MiB.
   - This trap applies to any Node CLI in a Docker image that runs `npm install -g` as root and the tool as another user. Fix: set `NODE_COMPILE_CACHE` to a user-writable directory, remove `/tmp/node-compile-cache` at the end of the root build step, or run the tool once as the runtime user during the build so a warm cache ships in the image.

### 5.4 How to explore and reproduce

- **Live dashboard:** `http://localhost:8765`.
- **Explorer:** `http://localhost:8765/explorer/#/h/pi/run/<run>`. It is static, so it also works with `python3 -m http.server 8777 -d lab/explorer`. Prediction verdicts are in `lab/explorer/predictions.json`.
- **Database:** `lab/out/agentlab.sqlite`, documented in `docs/DATA.md`. Start from the views `v_cpu_by_role`, `v_thread_cpu`, `v_syscall_totals`, `v_file_totals`, `v_net_totals` and `v_tool_latency`.
- **Raw traces:** `lab/out/pi/runs/<scenario>/` (meta.json, events-*.jsonl, probe-*.jsonl, proc-events.jsonl, strace-*.txt).
- **Re-running:** `cd lab && ./lab.sh up pi && python3 run.py --harness pi all`, then `python3 analyze/build_db.py && python3 analyze/build_explorer.py` (see `lab/README.md`). Real-model scenarios need an OpenRouter key file (`OPENROUTER_KEY_FILES=… python3 run.py --harness pi real`).
- **pi internals** are in `docs/ARCHITECTURE.md`; §10 lists the 15 predictions.
- **pi vs Claude Code** under the same scenarios: `reports/HARNESS-COMPARISON.md`.

---

## Appendix A — Scenarios

| Run | What it does |
|---|---|
| 00-baseline | Idle container, no agent (floor: 0.41 ms CPU over 8 s, 11.4 MiB, 2 pids) |
| 01-startup | `--version` ×5; one-turn chat offline / online / PI_TIMING; TS extension ×3 |
| 02-idle-tui | Interactive TUI in tmux, idle 30 s (online) |
| 03-each-tool | One call of each tool (ls, write, read, edit, grep, find, shell), ×3 |
| 04-loop-200 | 200 sequential bash `echo` turns |
| 05-loop-mixed-300 | 300 turns cycling bash / read / ls / grep / write / find over a 589-file repo |
| 06-parallel-tools | One message with 16 tool calls (sleep 1 + python) × 3 rounds |
| 07-big-output | 50 MB stdout, 200k lines, 4 MB base64, a 19.5 MB file read ×3 |
| 08-cpu-child | 5 s python busy loop; 8 parallel sha256 of 200 MB |
| 09-timeout-orphans | sleep 30 with a 2 s timeout; background job; setsid escape; sudo sleep |
| 10-sudo | NOPASSWD, password without a TTY, `-S` piped password, apt-get update |
| 11-stream-modes | 40k-char stream at full speed in json / print / rpc / TUI; 12k chars at 60 chunks/s in json and TUI |
| 12-compaction | 64k-context model, ~48 KB of tool output per turn × 30 turns |
| 13-concurrency | N = 1 / 4 / 16 / 32 parallel 30-turn loops; 16 with 400 ms model latency |
| 14-rpc-session | One RPC process, 6 prompts × ~40 mixed turns |
| 15-subagents | Subagent extension, 4 child agents × 15 bash turns |
| 16-fd-download | find with no usable fd: download from GitHub |
| 17-strace | each-tool under `strace -f -c` and an exec/connect/open trace |
| 18-cache-warmer | 30 s TTL, cacheWarming=idle, RPC idle 75 s |
| 19-retries | 503 ×3, 429 ×2 (retry-after 2), 500 ×8 |
| 20-lock-contention | 32 agents sharing a config dir vs 32 with private dirs |
| 21-tui-scaling | TUI 10k / 20k / 40k / 80k streams, 4 resizes; json-80k control |
| 22-env-leak | What tool children receive: environment, fds, TTY, capabilities |
| 23-probe-overhead | 10 runs of a 20-turn loop with the probe, 10 without |
| R1-fix-tests-json | Real model fixes 3 failing unit tests (json) |
| R2-sudo-task-json | Real model: sudo apt-get install jq, password-gated read |
| R3-explore-tui | Real model explores the repo in the TUI |

## Appendix B — Queries run for this report (refresh after the data fixes)

All queries ran read-only against `lab/out/agentlab.sqlite`.

**B1. GC relabelling**

```sql
SELECT run, kind, COUNT(*), ROUND(SUM(dur_ms),2), ROUND(MAX(dur_ms),2) FROM gc
WHERE harness='pi' AND run IN ('04-loop-200','05-loop-mixed-300','14-rpc-session','12-compaction','R1-fix-tests-json','11-stream-modes')
GROUP BY run, kind;
```

→ 04: major 2 / 2.47, minor 30 / 21.09, incremental 2 / 0.26 · 05: major 5 / 12.19 / max 3.6, minor 42 / 36.69, incremental 5 / 0.86 · 14: major 6 / 16.74 / max 5.19 · R1: major 2 / 3.45, minor 59 / 30.09.

**B2. Subagent role**

```sql
SELECT role, COUNT(*), ROUND(SUM(cpu_ms),1) FROM procs WHERE harness='pi' AND run='15-subagents' GROUP BY role;
```

→ `agent 1 574.7`, `agent:nested 4 1599.6`, `tool:shell 60 71.2`.

**B3. 04 and 09 coverage**

`SUM(file_io write bytes)/procs.wchar` for each agent → 04: 7,313,494 / 7,439,205 = 98.3%; 09: 99.7%; 05: 99.0%.

```sql
SELECT SUM(count) FROM syscalls WHERE harness='pi' AND run='04-loop-200';                  -- 74504
SELECT thread, SUM(count) FROM syscalls WHERE harness='pi' AND run='04-loop-200' AND pid=75935 GROUP BY thread;
-- pi 25278, V8Worker 5702, MainThread 1306, libuv-worker 995, runc:[2:INIT] 544, node 257
```

net_bytes for pid 75935 → tcp_tx 5,918,621, tcp_rx 303,790.

**B4. No fsync anywhere**

```sql
SELECT run, name, SUM(count) FROM syscalls WHERE harness='pi'
AND name IN ('fsync','fdatasync','sync_file_range','syncfs','sync','msync') GROUP BY run, name;
```

→ no rows.

**B5. 09 kills**

`syscalls name IN ('kill','tgkill') AND thread='pi'` → 2 rows of 1 call each (buckets ...829098 and ...831102). `signals` → +2226.2 ms pi → sleep sig 9; +4460.2 ms pi → sudo sig 9; +4673.3 ms pkill → sleep sig 15 ×3.

**B6. Startup**

```sql
SELECT cpu_ms, life_ms, peak_rss_kb/1024.0 FROM procs WHERE harness='pi' AND run='01-startup' AND role='agent'
AND argv LIKE '%chat chars%' AND argv NOT LIKE '% -e%';
```

→ n=13, CPU median 246.52 (240.5-265.3), life 191.77, RSS 146.49 MiB (sorted 142.3 … 157.7), minflt 25,034.
`--version` → 160.9 / 163.5 / 164.5 / 165.3 / 181.6 ms, life 126.52, 116.45 MiB.
Threads (median) → pi 167.77, V8Worker 70.40, SignalInspector 2.53, MainThread 2.52, libuv-worker 1.13, DelayedTaskSche 0.20, runc 0.59.

**B7. Turn phases** (`turns`, idx>0)

| Run | pre | model | dispatch | tool | post | turn | mean turn |
|---|---|---|---|---|---|---|---|
| 04 | 0.96 | 0.46 | 0.56 | 1.74 | 0.25 | 4.09 | 4.26 |
| 05 | 1.73 | 0.91 | 0.63 | 1.86 | 0.23 | 6.84 | – |
| 14 | 2.59 | 1.02 | 0.63 | 1.86 | 0.25 | 7.62 | – |

05 pre_request bucket medians → 0.83, 1.20, 1.57, 1.99, 2.31, 2.50, 0.99 ms.

**B8. Concurrency** (procs grouped by start gap > 1.5 s)

| Group | CPU median | runq median | life median |
|---|---|---|---|
| N=1 | 431.39 | 9.62 | 352.27 |
| N=4 | 586.9 | 64.16 | 553.67 |
| N=16 | 600.21 | 379.18 | 806.07 |
| N=32 | 615.17 | 1063.84 | 1363.75 |
| N=16, 400 ms latency | 653.57 | 153.05 | 12979.76 |

cgroup peak → 3,271.6 MiB, 359 pids. 15-subagents peak 575.5 MiB, 58 pids.

**B9. Streaming, probe and lock contention**

- 11/21 agent CPU (ms) → json-40k 512.1, json-12k@60 2,167.7, print 400.2, rpc 512.6, TUI-40k 707.9, TUI-12k@60 10,437.6, TUI 10k / 20k / 40k / 80k 429.3 / 505.7 / 673.3 / 1,398.8, json-80k 831.3.
- 23 → probe median 348.04 ms (first run 390.7), life 266.15, 156.63 MiB; no probe 332.48 ms, 236.94 ms, 157.15 MiB.
- 20 → shared 496.54 / runq 870.89 / life 998.49; private 449.21 / 766.86 / 873.89 (32 agents each).

**B10. Warmer, retries, real model**

- 18 `mock_requests` → (0.0 s, 5,728 B, 8192) … (20.033, 6,418, 1), (40.041, 6,418, 1), (60.048, 6,418, 1).
- 19 → injected 503 at 183155 / 185165 / 189178, ok at 197191; steps rc 0 / 0 / 0.
- R procs → R1 2,154.2 ms / 78,811 ms / 156.2 MiB / runq 669.9; R2 680.6 / 12,586 / 157.1; R3 1,866.5 / 41,585 / 170.2.
- R http → R1 17 requests, 78,405 ms (TTFB 5,063).

**B11. Role CPU** (`v_cpu_by_role`)

- 08: tool:command 8,322.7, agent 307.3.
- 10: tool:command 608.9, tool:root 420.8, agent 319.7, tool:sudo 34.4.
- 06: tool:command 869.9, agent 458.5, tool:shell 71.4.

All other figures are as reproduced by the unit verifiers. Each finding in Parts 2-3 names the table and filter it came from (`procs`, `threads`, `turns`, `mock_requests`, `file_io`, `net_bytes`, `tcp_events`, `gc`, `probe_samples`, `cgroup_samples`, `runs.disk_usage`, and the raw `events-*.jsonl`, `stderr-*.txt` and `strace-*.txt` files under `lab/out/pi/runs/`).
