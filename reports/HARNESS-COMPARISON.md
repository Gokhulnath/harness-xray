# Harness comparison: pi 0.87.1 vs Claude Code 2.1.299

How two coding-agent harnesses use CPU, memory, processes, network and disk when driven by the same
scripted scenarios in the same lab. This report is for someone choosing or designing a harness. The
question it answers is which design choices drive resource cost, and how much.

| | pi | Claude Code |
|---|---|---|
| Package | npm `@earendil-works/pi-coding-agent` 0.87.1 | npm `@anthropic-ai/claude-code` 2.1.299 |
| Runtime actually executed | Node.js (V8), `node /usr/local/bin/pi` | **Bun 1.4.3 single-file native binary** (JavaScriptCore + mimalloc), `/usr/local/bin/claude` → `bin/claude.exe` |
| Model protocol (mock) | OpenAI chat-completions | Anthropic Messages |
| Tools the mock maps intents onto | bash, read, write, edit, grep, find, ls | Bash, Read, Write, Edit; ls/grep/find **fall back to Bash** (2.1.299 ships no LS/Grep/Glob) |
| Lab adapter | `lab/harnesses/pi/harness.json` | `lab/harnesses/claude-code/harness.json` |

**Setup.** Docker Desktop VM, 18 vCPU, 11.6 GB, arm64, cgroup v2. Every scenario ran in a fresh
container recreated from the harness image. A privileged monitor recorded eBPF per-process and
per-thread CPU, run-queue wait, peak RSS, I/O, syscalls, file I/O, opens, sockets, TCP, signals, plus
100 ms `/proc`, cgroup, network and VM samples (see `docs/DATA.md`). A scripted mock LLM drives both
harnesses. Claude Code skipped the scenarios that need rpc mode, pi extensions, a small-context model or
a config dir (12, 14, 15, 16, 18, 20), and all real-model runs (R1–R3).

**Notation.**
- Memory is in MiB (KiB/1024), written "MiB". Request, file and network sizes are decimal bytes (1 KB = 1000 B).
- `*` marks a pi number that includes the in-process Node probe. The probe never loaded in Claude Code
  (see §1), so Claude Code figures are probe-free everywhere. Probe-free pi numbers exist only in
  `23-probe-overhead`, where the probe added +15.5 ms CPU, +29 ms process life, +53 ms wall and ±0.5 MiB
  RSS per 20-turn session (median, n=10 per arm).
- "Harness process" means lab role `agent`. Tool children (`tool:*`), harness helpers (`agent:helper`)
  and lab machinery (`lab:*`, `container:init`, tmux) are reported separately. The lab labels Claude
  Code's shell-snapshot and IDE-detection children as `tool:*`. In this report they count as harness
  overhead, not model tool calls.
- n is the number of agents or sessions behind a median. Most scenarios ran once per harness (n=1 run);
  only `01-startup` (n=5 per phase) and `23-probe-overhead` (n=10 per arm) have replicates.
- Run links: `http://localhost:8765/explorer/#/h/<harness>/run/<run>`.
- Queries are against `lab/out/agentlab.sqlite` unless another file is named.

---

## TL;DR

- **Claude Code 2.1.299 is not a Node app in this lab. It is a Bun/JavaScriptCore native binary, and
  that choice explains most of the differences.** `--version` costs almost nothing (8.7 ms CPU, 38 MiB
  vs 165 ms, 117 MiB*). Claude Code also sends its first request sooner (130 vs 163* ms) and runs a
  one-turn chat for less CPU (172 vs 247* ms). But it holds about 2x pi's memory: 226 vs 146* MiB for
  one turn, 319 vs 157 MiB for 20 turns (both probe-free), 409 vs 210* MiB after 200 turns.
- **Per turn, Claude Code's harness process burns about 2.6x pi's CPU.** On an identical 200-turn
  shell-echo loop it uses 12.9–13.2 vs 4.9–5.0* ms per turn. The median gap between model calls is
  8.25 vs 4.06* ms, and it makes about 363 vs 72 syscalls per turn. Its JIT compiler thread alone takes
  22–27% of its CPU. It also builds and serialises a request about 20x larger on every turn.
- **The tool schemas set the request size, not the conversation.** Every Claude Code request repeats
  101.5 KB of fixed payload: 24 tool schemas (74 KB) and a 27 KB system prompt. That is the floor; it
  rises to 119.5 KB with 27 tools when nonessential traffic is allowed. pi's fixed payload is 5.4 KB
  (4 tools). Over 200 turns Claude Code sent 30.2 MB against pi's 5.89 MB, and 67.5% vs 18.5% of it
  was the fixed part.
- **Shell execution.** In steady state both spawn one process per simple command (0.98 vs 0.82 ms CPU
  per `echo`). Claude Code differs in three ways:
  - It wraps every command in `bash -c 'source <snapshot> … eval …'`, so an external command costs
    2 processes.
  - It builds that snapshot on the first Bash call of a session: 26 processes, about 10 ms CPU, about
    30 ms of added latency.
  - A 20-turn session has 51 descendant processes vs pi's 20.

  It also ran 16 parallel tool calls one at a time (50.0 s); pi ran them all at once (3.4 s).
- **Scale.** With 32 concurrent agents and an instant mock, Claude Code used 1,247 vs 615* ms CPU per
  agent and finished 11.3 vs 20.4 sessions/s. With 400 ms model latency, memory becomes the limit, not
  CPU. Claude Code needs about 135 MiB of anon memory per agent, pi about 99 MiB. On this 18-vCPU,
  11.6 GB VM that fits about 74 vs about 98 agents (about 4.1 vs 5.4 per vCPU), extrapolated from 16
  agents.
- **Timeouts kill differently, and both leave gaps.**
  - pi SIGKILLs the tool's process group at the deadline (4.7 s run). It left 3 orphans, one of them a
    root `sleep` started via `sudo`.
  - Claude Code finds children with `ps` and SIGTERMs them. It turned the `sudo` command into a
    background task and, in `-p` mode, waited 302 s for it before exiting (304.5 s run).
  - Neither harness cleans up `setsid` escapes, or children left by commands that exited normally.
- **Retry policies are opposite.**
  - pi: fixed 2/4/8 s waits, 3 retries. It ignores the Retry-After value, gives up after about 14 s,
    and still exits 0.
  - Claude Code: waits double from 0.5 s with random jitter. The base caps at 32 s; the longest wait
    seen was 38.4 s. It honours Retry-After exactly, allows 10 retries, and rode out eight 500s in 104 s.
- **TUI rendering and idle.** pi's TUI appends the whole transcript to terminal scrollback, so its cost
  per chunk grows with message length. At 60 chunks/s it rose from 133 to 235 ms/s; the totals were
  10.4 vs 5.9 s of CPU. Every pi resize replays the transcript (0.56–0.99 MB). Claude Code repaints only
  the visible screen, about 5 KB per stream. Both idle at about 12–15 ms CPU/s in the TUI, but half of
  Claude Code's goes to GC and allocator threads, and pi's figure is inflated by the probe.

---

## Master side-by-side table

| Dimension | pi 0.87.1 | Claude Code 2.1.299 | Scenario / note |
|---|---|---|---|
| Runtime | Node/V8; probe loaded | Bun 1.4.3 / JavaScriptCore native binary; probe never loads | 01, 02, 23 |
| `--version`: CPU / life / peak RSS (n=5) | 164.5 ms / 126.5 ms / 116.5 MiB* | 8.7 ms / 7.7 ms / 38.2 MiB | 01 |
| One-turn chat, offline (n=5): CPU / peak RSS | 246.5 ms* / 145.8 MiB* | 172.3 ms / 226.3 MiB (+6 helper children, 9.8 ms) | 01 |
| …exec → first model request | 162.6 ms* | 130.4 ms | 01 |
| 20-turn loop, no probe (n=10): CPU / life / peak RSS | 332.5 ms / 236.9 ms / 157.1 MiB | 534.1 ms / 371.3 ms / 318.9 MiB | 23 |
| Threads (excluding 5 runc threads) | 12, fixed in every scenario | 10–11 one-turn → 17.3 (20 turns) → 18 (200+ turns) → 20 (idle TUI) | 01, 23, 04, 02 |
| Idle TUI CPU, 10–29 s after start | 12.6 ms/s* (upper bound; probe timer) | 14.7–15.3 ms/s (about half GC/allocator threads) | 02 |
| Idle TUI RSS, steady / peak | 146.6 / 161.8 MiB* | 272.9 / 327.6 MiB | 02 |
| Waiting in headless mode | about 16 ms/s* (probe-shaped) | 46.6 ms/s for about 4 s, then 3.4 ms/s (19); 8.0 ms/s holding a background task (09) | 19, 09 |
| Harness CPU per turn, identical echo loop | 4.9–5.0 ms* | 12.9–13.2 ms | 04 (and 04−23) |
| Median gap between model calls (`harness_ms`) | 4.06 ms* (20-turn no probe: 3.30) | 8.25 ms (20-turn: 8.94) | 04, 23 |
| Marginal syscalls per turn | about 72 (futex about 11) | about 363 (futex about 204) | (04−23)/180 |
| Fixed payload per request (tool schemas + system) | 5,419 B (4 tools) | 101,487 B (24 tools); 119.5 KB online json, 112.6 KB TUI (27 tools) | 04, 01, 11/21 |
| Request bytes: first / growth per turn / mean, 200-turn loop | 5,727 B / 237 B / 29 KB | 112,047 B / 385 B / 150 KB | 04 |
| Total request bytes, 201 requests | 5.89 MB (18.5% fixed) | 30.2 MB (67.5% fixed) | 04 |
| Processes per tool call: `echo` / `uname -a` | 1 / 1 | 1 / 2 | 04, 03 |
| First shell call of a session | 1 process, 0.9 ms | 26 processes, 9.6–10.9 ms CPU, about +30 ms latency | 04, 05, 06, 08 |
| Descendant processes per 20-turn session | 20 | 51 (20 tool shells + 31 setup) | 23 |
| 16 parallel tool calls in one message | 16 concurrent, 3.4 s | 1 at a time, 50.0 s | 06 |
| Peak RSS after 200 turns / growth turn 25→200 | 210.1 MiB* / +0.35 MiB per turn | 409.4 MiB / +0.52 MiB per turn | 04 |
| Per-agent memory at 16+ concurrent agents (PSS) | 95.4 MiB* (anon 92.6) | 131.7 MiB (anon 125.2; 116 MiB binary pages shared) | 13 |
| Agents per box at 400 ms latency (memory-bound) | about 98–100 (about 5.4 per vCPU) | about 74 (about 4.1 per vCPU) | 13, extrapolated |
| Cores per agent at 400 ms latency | 0.053* | 0.107 | 13 |
| Throughput, 32 agents, instant mock | 20.4 sessions/s* | 11.3 sessions/s | 13 |
| Timeout kill | SIGKILL to the process group, exactly at the deadline, no grace | `ps -A` tree walk, then SIGTERM to each descendant | 09 |
| Command that outlives its timeout | killed; a root grandchild survives (EPERM) | `sudo …` moved to a background task; `-p` waits for it, then runs 1 extra model turn | 09 |
| Processes alive after the harness exits | 3 (`(cmd &)` child, `setsid` child, root `sleep`) | 0, but only because they finished during the 302 s wait | 09 |
| Retry schedule | 2 / 4 / 8 s fixed, maxAttempts 3, no jitter | min(0.5 s × 2^n, 32 s) + 0.6–20% jitter, max_retries 10 | 19 |
| 429 with `Retry-After: 2` | ignores the value: waits 2 s, then 4 s | waits 2.0 s, then 2.0 s | 19 |
| After giving up | `auto_retry_end success:false`, exit code 0, empty stderr | not observed (8 failures injected; succeeded on attempt 9) | 19 |
| Tool stdout transport | Unix socket pair (Node/libuv `pipe` stdio) | file `/tmp/claude-1001/…/tasks/<id>.output`, read back | 22, 07 |
| TUI rendering | appends to scrollback; resize replays the transcript | repaints the visible screen only | 11, 21 |
| Session transcript, one-turn chat | 7.1 KB | 209 KB | 01 |

---

## 1. Runtime and startup

Runs: [cc/01-startup](http://localhost:8765/explorer/#/h/claude-code/run/01-startup) ·
[pi/01-startup](http://localhost:8765/explorer/#/h/pi/run/01-startup) ·
[cc/23-probe-overhead](http://localhost:8765/explorer/#/h/claude-code/run/23-probe-overhead) ·
[pi/23-probe-overhead](http://localhost:8765/explorer/#/h/pi/run/23-probe-overhead)

**Claude Code runs as a Bun binary, not Node.** The lab executes `/usr/local/bin/claude`, a copy of
`node_modules/@anthropic-ai/claude-code/bin/claude.exe`, and no `node` process starts. Five things show
it is Bun:
- The `/api/hello` request carries the user agent `Bun/1.4.3`.
- The threads are JavaScriptCore and Bun threads: `HeapHelper`, `JITWorker`, `JSCWarmUp`,
  `Bun Pool 0-5`, `mi-scavenger` and `HTTP Client`.
- `NODE_OPTIONS=--require /lab/probe/probe.cjs` is ignored, so `probe_procs`, `probe_samples`,
  `heap_spaces`, `gc` and `http` have no Claude Code rows.
- In the probe on/off control (23), Claude Code's numbers are identical: 534.0 vs 534.1 ms agent CPU,
  n=10 each.
- `lab/harnesses/claude-code/harness.json` still declares `"runtime": "node"` and `"probe": "node"`,
  which is why the lab injected a probe that cannot load.

The consequence for this report: Claude Code has eBPF and `/proc` data only. Nothing in this dataset
shows its JS heap or GC behaviour.

**Startup speed favours Claude Code.**
- *`--version`.* The binary answers without starting the JS app: 8.7 ms CPU, 7.7 ms life, 38.2 MiB,
  one thread. pi starts Node and reads its JS from disk first: 164.5 ms*, 126.5 ms* and 116.5 MiB*.
- *One-turn chat, offline (n=5).*
  - Exec → first model request: 130.4 vs 162.6* ms.
  - Agent CPU: 172.3 vs 246.5* ms. Process life: 142.9 vs 189.2* ms.
  - `docker exec` wall time is the same, 0.239 vs 0.240 s.
  - Claude Code also spawns 6 helper children, 9.8 ms CPU in total: `git` ×3, `claude.exe --files` ×2
    (its embedded ripgrep listing `/workspace`) and `claude.exe --version`. pi spawns none.
- *Probe overhead.* In the no-probe control pi's first request comes 156.1 ms after exec, against
  135.1 ms for Claude Code. The roughly 15 ms that the probe adds does not close the gap.
- *Code on disk vs in the binary.* pi reads 4.83 MB of JS from disk per start; Claude Code reads
  0.61–0.80 MB, because its JS bundle is embedded in the binary.

**After the first turn, Claude Code costs more.** The 20-turn loop without the probe (n=10) compares
like with like: 534.1 vs 332.5 ms CPU, 371.3 vs 236.9 ms life, 0.458 vs 0.294 s wall.

**The first request is about 20x larger:** 112,022 B (24 tools, 74,162 B of schemas, a 27,325 B system
prompt) vs 5,702 B (4 tools, 2,841 B of schemas, a 2,578 B system prompt). That costs JSON build and
serialisation work on every turn (§4).

**Turning on nonessential traffic makes Claude Code slower to start.** With it on, first-turn latency
and process life grow a lot (§8). One-turn online figures (n=5):

| | pi | Claude Code |
|---|---|---|
| Agent CPU | 247.5 ms* | 285.1 ms |
| Process life | 191.8 ms* | 805 ms |
| Peak RSS | 155.3 MiB* | 265.6 MiB |
| Exec → first request | 166.3 ms* | 346.6 ms |
| Exit delay after the last reply | 26.9 ms* | 421 ms |

The online first request is 130,051 B with 27 tools.

**Session file per one-turn chat:** 209,295 B (238,557 B online) vs 7,125 B.

Evidence:
- `SELECT comm, file, substr(args,1,60), count(*) FROM execs WHERE harness='claude-code' AND run='01-startup' GROUP BY file, substr(args,1,60)`
  → `runc:[2:INIT]|/usr/local/bin/claude|-p --output-format stream-json --verbose|10 ; claude|…/claude-code/bin/claude.exe|--no-config --files --hidden /workspace|10` (pi: `pi|/usr/local/bin/node|/usr/local/bin/pi --mode json …|13`)
- `SELECT harness, path, user_agent, count(*) FROM mock_requests WHERE run IN ('01-startup','02-idle-tui') GROUP BY harness, run, path, user_agent`
  → `claude-code|/api/hello|Bun/1.4.3|5 ; claude-code|/v1/messages|claude-cli/2.1.299 (external, sdk-cli)|10 ; pi|/v1/chat/completions|pi (linux 7.0.14-linuxkit; arm64)|16`
- `SELECT harness, count(*) FROM probe_procs GROUP BY harness` → `pi|203`. There are no claude-code rows, and `lab/out/claude-code/runs/*/` contains no `probe-*.jsonl`.
- Medians of `procs` (role `agent`) in 01, split by phase → CC version cpu 8.7, life 7.7, RSS 38.2 MiB; offline cpu 172.3, life 142.9, RSS 226.3; online 285.1 / 805 / 265.6. pi version 164.5 / 126.5 / 116.5; offline 246.5 / 189.2 / 145.8; online 247.5 / 191.8 / 155.3.
- First `/v1/*` request time minus `procs.start_ms`, median → 01 offline CC 130.4 ms (126.8–158.9), pi 162.6 (159.3–168.6); 23 no-probe CC 135.15, pi 156.05.
- `SELECT wall_s FROM steps WHERE run='01-startup' AND tag LIKE 'chat-offline%'` → CC .244/.241/.238/.239/.236; pi .294/.244/.235/.240/.240.
- `SELECT harness, step, req_bytes, n_tools, tools_bytes, sys_bytes FROM mock_requests WHERE run='01-startup' AND step=0 AND path LIKE '/v1/%' GROUP BY harness, n_tools`
  → `claude-code|0|112022|24|74162|27325 ; claude-code|0|130051|27|92164|27369 ; pi|0|5702|4|2841|2578 ; pi|0|7507|5|4646|2578`
- `procs` in 23, role `agent`, first 10 = probe and last 10 = no-probe (median) → CC 534.0 vs 534.1 ms; pi 348.0 vs 332.5 ms.
- `file_io` JOIN `procs` (role `agent`) in 01, ftype `file`, op `read` → CC 607,351–796,957 B; pi 4,827,105–4,827,199 B.

---

## 2. Memory

Runs: [cc/02-idle-tui](http://localhost:8765/explorer/#/h/claude-code/run/02-idle-tui) ·
[pi/02-idle-tui](http://localhost:8765/explorer/#/h/pi/run/02-idle-tui) ·
[cc/04-loop-200](http://localhost:8765/explorer/#/h/claude-code/run/04-loop-200) ·
[pi/04-loop-200](http://localhost:8765/explorer/#/h/pi/run/04-loop-200) ·
[cc/05-loop-mixed-300](http://localhost:8765/explorer/#/h/claude-code/run/05-loop-mixed-300) ·
[pi/05-loop-mixed-300](http://localhost:8765/explorer/#/h/pi/run/05-loop-mixed-300) ·
[cc/13-concurrency](http://localhost:8765/explorer/#/h/claude-code/run/13-concurrency) ·
[pi/13-concurrency](http://localhost:8765/explorer/#/h/pi/run/13-concurrency)

**Claude Code holds about 1.5–2x pi's memory at every stage.** The one exception is `--version`.

| Stage (peak RSS of the harness process) | pi | Claude Code |
|---|---|---|
| `--version` (n=5) | 116.5 MiB* | 38.2 MiB |
| One-turn chat, offline (n=5) | 145.8 MiB* | 226.3 MiB |
| 20-turn loop, no probe (n=10) | 157.1 MiB | 318.9 MiB |
| Idle TUI, steady / peak (n=1, online) | 146.6 / 161.8 MiB* | 272.9 / 327.6 MiB |
| 200-turn echo loop (n=1) | 210.1 MiB* | 409.4 MiB |
| 300-turn mixed loop (n=1) | 372.7 MiB* (compacted at turn 291) | 625.1 MiB |

**What the memory is made of.** These are `mem_detail` readings from the idle TUI, 20 samples taken
more than 10 s after start.
- *Claude Code:* RSS 274.5, PSS 273.4 = anon 150.5 + file-backed 122.9 MiB (VmExe 67.7 MiB). The
  file-backed part is the memory-mapped Bun binary with its embedded JS. It stays flat at about
  117–122 MiB through every loop.
- *pi:* RSS 147.6, anon 96.2, so about 51 MiB is resident and file-backed. Of that, 42.8 MiB is
  shared-clean node-binary pages. This is a lab artefact: the mock LLM also runs `node` from the same
  `node:24-bookworm-slim` layer and shares those pages. The mock showed 35.6 MiB shared-clean in pi's
  run vs 1.5 MiB in Claude Code's. pi's solo PSS (125.9 MiB) is therefore about 21 MiB lower than a real
  single-agent deployment would show.
- *Like-for-like solo comparison:* use RSS, 274.5 vs 147.6 MiB.
- *Container level:* `memory.current` was 182.8 vs 110.8 MiB idle, against an 11.4 MiB baseline.
- *pi's JS heap is small:* idle heap_used 29.8, heap_total 41.3, external 12.6 MiB. Most of pi's RSS is
  runtime and code, not heap.

**With many agents running, Claude Code's binary pages are shared.** At 16 or more concurrent agents
(13), per-agent PSS was:
- Claude Code: 131.7 MiB = anon 125.2 + file 6.5, with 116.3 MiB shared-clean (n=309 samples).
- pi: 95.4 MiB = anon 92.6 + file 2.8, with 49.5 MiB shared-clean (n=241).

So the marginal cost of one more agent is roughly its anon memory: about 125 vs 93 MiB. The mock-sharing
artefact does not matter here (1 extra sharer among 16+).

**Growth over long sessions.** The 200-turn echo loop (04) is the clean comparison: both harnesses use
the same shell tool.
- RSS from turn 25 to turn 200: Claude Code 306 → 398 MiB (+0.52 MiB per turn); pi 148 → 210 MiB
  (+0.35 MiB per turn).
- The mixed loop (05) grew 343 → 568 MiB (+1.13 MiB per turn) vs 173 → 336 MiB (+0.81 MiB per turn),
  measured from turn 50 to 250. That run is confounded by the mock mapping. Claude Code's ls/grep/find
  run as Bash and return more text, so by turn 250 its transcript was 1.47 MB (1.20 MB of tool results)
  vs pi's 632 KB (469 KB).
- Per MB of transcript growth, pi's RSS actually grew faster: about 338 vs 214 MiB per MB.
- In both harnesses RSS grows hundreds of times faster than the transcript, so the growth is not
  retained conversation.
  - For pi the probe shows it is GC headroom: in 05, heap_total peaked at 198.8 MiB, new_space reached
    128 MiB, and old_space was only 25 MiB.
  - For Claude Code only PSS is visible: anon grew 234 → 453 MiB while file-backed stayed flat. Whether
    that is retained memory or headroom cannot be told from this data.

Evidence:
- `SELECT avg(rss_kb), avg(pss_kb), avg(pss_anon_kb), avg(pss_file_kb), avg(shared_clean_kb), avg(vm_exe_kb) FROM mem_detail JOIN procs(role='agent') WHERE run='02-idle-tui' AND t-start_ms>10000` → CC 274.5 / 273.4 / 150.5 / 122.9 / 1.7 / 67.7 MiB; pi 147.6 / 125.9 / 96.2 / 29.7 / 42.8 / 97.9.
- `SELECT max(peak_rss_kb) FROM procs WHERE run='02-idle-tui' AND role='agent'` → CC 335,412 KB = 327.6 MiB; pi 165,728 KB = 161.8 MiB.
- `mem_detail` for the mock LLM process in 02 → pi run (pid 74797): shared_clean 35.6 MiB; CC run (pid 98899): 1.5 MiB. Both images are `node:24-bookworm-slim` (see `lab/mockllm/Dockerfile` and `lab/base/Dockerfile`).
- `mem_detail` in 13, agents bucketed by how many ran at once, bucket 16+ → CC n=309: rss 241.5, pss 131.7, anon 125.2, file 6.5, shared_clean 116.3; pi n=241: rss 142.4, pss 95.4, anon 92.6, file 2.8, shared_clean 49.5.
- `cgroup_samples` for container `lab-agent` from t0+15 s to t0+28 s (02) → CC mem 182.8 MiB, pi 110.8 MiB; 00-baseline 11.4 MiB for both.
- `proc_samples.rss_kb` of the agent pid at each request's timestamp → CC 04: turn 25 306.2, turn 200 397.5; pi 04: 147.8 / 209.9. CC 05: turn 50 343.1, turn 250 568.2; pi 05: 172.9 / 335.8.
- `SELECT harness, run, max(peak_rss_kb) FROM procs WHERE run IN ('04-loop-200','05-loop-mixed-300') AND role='agent' GROUP BY 1,2` → CC 04 409.4 MiB, pi 04 210.1, CC 05 625.1, pi 05 372.7. (Re-run for this report: `r04|claude-code|…|409.4`, `r04|pi|…|210.1`.)
- `SELECT harness, step, req_bytes, tool_result_bytes FROM mock_requests WHERE run='05-loop-mixed-300' AND step IN (50,250)` → CC 250: 1,471,546 / 1,197,536; pi 250: 632,226 / 468,565.
- `heap_spaces` and `probe_samples` for pi in 05 → new_space 20.8 used of 128.0 MiB, old_space 19.5 of 25.1, heap_total max 198.8.
- `probe_samples` for pi in 02, last sample → rss 148.6, heap_used 29.8, heap_total 41.3, external 12.6 MiB.

---

## 3. Threads and idle cost

Runs: [cc/02-idle-tui](http://localhost:8765/explorer/#/h/claude-code/run/02-idle-tui) ·
[pi/02-idle-tui](http://localhost:8765/explorer/#/h/pi/run/02-idle-tui) ·
[cc/09-timeout-orphans](http://localhost:8765/explorer/#/h/claude-code/run/09-timeout-orphans) ·
[cc/19-retries](http://localhost:8765/explorer/#/h/claude-code/run/19-retries) ·
[pi/19-retries](http://localhost:8765/explorer/#/h/pi/run/19-retries)

**Thread inventory.** Counts below exclude the 5 `runc:[2:INIT]` threads of a docker-exec'd agent.
- *pi:* 12 threads in every scenario: main, 4 `V8Worker`, 4 `libuv-worker`, `SignalInspector`,
  `MainThread` and `DelayedTaskScheduler`.
- *Claude Code:* 10–11 for a one-turn offline chat (avg 10.4, n=5), 17.3 for the 20-turn no-probe loop
  (17–18, n=10), 18 for the 200- and 300-turn loops, and 20 in the idle TUI. The extra threads appear
  once the heap gets busy: 7 `HeapHelper` GC threads, a `Bun Pool` that grows to 5–6 threads, and in
  the TUI a "Heap Collector Thread". Over the 304 s of 09, Claude Code created 70 short-lived
  HeapHelper threads.
- *Core count:* the pool sizes (7 HeapHelper, 4 V8Worker) were seen on 18 vCPU and probably scale with
  core count. That was not tested.

**CPU split by thread.**

| Run | Claude Code | pi |
|---|---|---|
| One-turn chat (01, median) | main 81%, JIT 9%, JSCWarmUp 5% (of 172.3 ms) | main 68%, V8Worker 29% (of 246.5 ms*) |
| 200-turn loop (04) | main 58%, JITWorker 27%, HeapHelper 7%, Bun Pool 6% | main 66%, V8Worker 33% |
| 300-turn loop (05) | main 62%, JITWorker 22%, HeapHelper 8%, Bun Pool 5% | main 70%, V8Worker 28%, libuv ≤1.3% |

**Idle in the TUI (02, n=1, online), 10–29 s after start.**
- *Claude Code:* 14.7–15.3 ms CPU/s; the range depends on which 100 ms sample brackets the window. By
  thread: main 5.8, HeapHelper ×7 3.7, mi-scavenger 2.1, Bun Pool ×6 1.6 ms/s, so about half is GC and
  allocator work.
  - Wake-ups: 24.5–24.7 voluntary context switches/s.
  - Syscalls: HeapHelper futex 288/s, mi-scavenger madvise 16.8/s.
  - The main thread polls `~/.claude/ide` (2.0 opens/s) and `/proc/self/statm` (1.35/s).
  - RSS drifts down from 293.8 to 272.9 MiB as mimalloc returns pages.
- *pi:* 12.6 ms/s*, all of it on the main thread, with 188.7 context switches/s and 353 `epoll_pwait`/s.
  Most of that is the probe: `monitorEventLoopDelay({resolution: 5})` is a 5 ms timer, and 100 ms
  sampling reads `/proc/self/stat` (19.8/s) and calls `getrusage` (20.8/s) (`lab/probe/probe.cjs`
  lines 66 and 120). In the no-probe control, pi's run-queue wait was 0.07 vs 6.34 ms with the probe.
  pi's true idle cost is below 12.6 ms/s; no probe-free idle run exists to measure it. RSS stays flat
  at 146.6 MiB.
- *Whole run:* 1,198.9 vs 742.2* ms over 30.9 s.
- *TUI premium over headless:* compared with online `-p`, the idle TUI peak is about 62 MiB higher for
  Claude Code (327.6 vs 265.6) and about 7 MiB higher for pi (161.8 vs 155.3). Part of Claude Code's
  premium is the plugin-marketplace download (§8).

**Waiting in headless mode.**
- *Retry backoff (19).* Claude Code's background CPU is front-loaded: 46.6 ms/s for the first 3.7 s,
  then 3.4 ms/s for the remaining 95.7 s of its 104 s wait. pi measured 16 ms/s* during its backoff,
  and its syscalls in that window were probe-shaped: 4,216 `epoll_pwait`, 354 reads of
  `/proc/30/stat`, 154 writes to `probe-30.jsonl`.
- *Holding a background task (09).* Claude Code used 8.0 ms/s over 290 s: main 990 ms, mi-scavenger
  800 ms, JITWorker 170 ms.
- *Under a model latency loop (13).* Claude Code's CPU grows with wall time when work is spread out.
  Going from 16 agents with an instant mock to 16 agents with 400 ms latency, its agent CPU rose
  1,061 → 1,391 ms for the same 31 requests (+330 ms). HeapHelper went 43 → 154 ms, mi-scavenger
  7 → 33 ms, main +88 ms, JIT +29 ms. pi rose 600 → 654* ms.

Evidence:
- `SELECT harness, run, avg(c), min(c), max(c) FROM (SELECT harness, run, pid, count(DISTINCT tid) c FROM threads WHERE comm NOT LIKE 'runc%' GROUP BY 1,2,3) JOIN procs(role='agent', not --version)` → CC 01 offline 11, 10, 10, 10, 11; 02: 20; 04: 18; 05: 18; 23 no-probe 17.3 (17–18); pi 12 everywhere.
- `SELECT comm, sum(cpu_ms) FROM threads WHERE run='04-loop-200' AND pid=<agent> GROUP BY comm` → CC claude 1668.4, JITWorker 771.9, HeapHelper ×7 210.4, Bun Pool 0-4 158.3; pi pi 825.0, V8Worker ×4 408.3, libuv-worker ×4 5.0.
- `thread_samples` in 02, per-thread CPU delta from 10 to 29 s → CC claude 110, HeapHelper 70, mi-scavenger 40, Bun Pool 30, JITWorker 10, Collector 10 ms; pi main 240 ms, all other threads 0.
- `SELECT thread, name, sum(count)/20.0 FROM syscalls JOIN procs(role='agent') WHERE run='02-idle-tui' AND t in [start+10s, start+30s)` → CC HeapHelper futex 288.45/s, claude futex 60.4/s, mi-scavenger madvise 16.75/s, epoll_pwait2 14.8/s, getrusage 5.05/s; pi epoll_pwait 353.4/s, read 29.9/s, getrusage 20.8/s, openat 19.85/s.
- `proc_samples` for the CC agent pid 4579 in 09, CPU delta from 10 s to 300 s → 2,320 ms over 290 s = 8.00 ms/s.
- `proc_samples` in 19, from the first injected request +300 ms to the resuming request −300 ms → CC r500 570 ms over 103.6 s (170 ms in the first 3.7 s, 330 ms over the next 95.7 s); pi r503 220 ms over 13.4 s.
- `threads` per agent in 13, n16 vs n16lat → CC claude 702 → 790, JITWorker 269 → 298, HeapHelper 43 → 154, mi-scavenger 7 → 33 ms.

---

## 4. Turns, requests and tool results

Runs: [cc/04-loop-200](http://localhost:8765/explorer/#/h/claude-code/run/04-loop-200) ·
[pi/04-loop-200](http://localhost:8765/explorer/#/h/pi/run/04-loop-200) ·
[cc/05-loop-mixed-300](http://localhost:8765/explorer/#/h/claude-code/run/05-loop-mixed-300) ·
[pi/05-loop-mixed-300](http://localhost:8765/explorer/#/h/pi/run/05-loop-mixed-300) ·
[cc/07-big-output](http://localhost:8765/explorer/#/h/claude-code/run/07-big-output) ·
[pi/07-big-output](http://localhost:8765/explorer/#/h/pi/run/07-big-output) ·
[cc/11-stream-modes](http://localhost:8765/explorer/#/h/claude-code/run/11-stream-modes) ·
[cc/21-tui-scaling](http://localhost:8765/explorer/#/h/claude-code/run/21-tui-scaling) ·
[pi/12-compaction](http://localhost:8765/explorer/#/h/pi/run/12-compaction)

### 4.1 A fixed payload on every request

Both harnesses send one model request per scripted turn: 201 requests for 200 turns in 04. Each
request carries the full conversation plus a fixed prefix.

| Configuration | Tools | Tool-schema bytes | System-prompt bytes | Fixed per request |
|---|---|---|---|---|
| pi, default | 4 | 2,841 | 2,578 | 5,419 B |
| pi, `--tools all` | 7 | 4,971 | 2,680 | 7,651 B |
| Claude Code, `-p`/json, lab offline env (every json scenario) | 24 | 74,162 | 27,325 | 101,487 B |
| Claude Code, `-p`/json, online (01 chat-online) | 27 (+DesignSync, Monitor, PushNotification) | 92,164 | 27,369 | 119,533 B |
| Claude Code, TUI (11, 21) | 27 (+AskUserQuestion, EnterPlanMode, ExitPlanMode) | 85,129 | 27,444 | 112,573 B |

The 24 Claude Code tools include Agent, Cron*, EnterWorktree, Workflow, Task*, WebFetch and WebSearch.
None of them was used in these scenarios.

**Effect over 200 turns (04):**

| Metric (04) | pi | Claude Code |
|---|---|---|
| First request (step 0) | 5,727 B | 112,047 B |
| Growth per turn (mean; median) | 237 B (238) | 385 B (341; 806 every 10th turn) |
| Last request (step 200) | 53,091 B | 189,111 B |
| Total sent | 5.89 MB (18.5% fixed) | 30.2 MB (67.5% fixed) |

In 05 the fixed share was still 30.55 MB of 276.76 MB for Claude Code, against 2.30 MB of 108.65 MB
for pi.

The mock does no prompt caching and does not count tokens. The "input tokens" Claude Code reports
(7,550,303 in 04, 434,878 in 05) are the mock's own `ceil(req_bytes/4)` estimate passed back
(`lab/mockllm/server.mjs:524`), not a Claude Code measurement. On a real API most of the fixed prefix
would be served from cache, which cuts token cost. The bytes still travel, and the JSON is still built
and parsed on every turn.

### 4.2 Harness CPU per turn

Two independent estimates agree for the identical echo loop (04):
- Interpolating 100 ms `/proc` CPU samples at request timestamps gives 13.2 vs 4.9* ms per turn.
- Subtracting the 20-turn run (23) from the 200-turn run (04) gives 12.9 vs 5.0* ms per turn.

pi's figure includes about 0.8 ms per turn of probe overhead. Claude Code has none, so the real gap is
slightly larger than shown.

| Metric | pi | Claude Code |
|---|---|---|
| `harness_ms` median, 04 (n=200) | 4.06 ms* (p90 4.91) | 8.25 ms (p90 12.4–12.7) |
| `harness_ms` median of medians, 23 no-probe (n=10) | 3.30 ms | 8.94 ms |
| `harness_ms` by turn: 0 / 1–10 / 11–50 / 151–199 (04) | 14.1 / 5.18 / 3.71 / 4.41 ms* | 39.1 / 9.60 / 8.57 / 8.35 ms |
| Agent CPU per turn, 05 (n=300, mapping-confounded) | 7.3 ms* | 16.7 ms |
| Marginal syscalls per turn, (04−23)/180 | about 72 (futex about 11) | about 363 (futex about 204) |

`harness_ms` is the time from one model reply to the next request, so it includes tool execution.

Shape over the session:
- Claude Code is slow on early turns while the JIT warms up, then flat at about 8 ms from turn 11 on.
- pi rises slowly with history: 3.7 → 4.4 ms.
- In 05, with requests up to 1.7 MB, both grow by a similar absolute amount: pi 4.39 → 8.31 ms, Claude
  Code 10.49 → 14.30 ms.

Where Claude Code's extra time goes:
- The JIT and GC threads are a third of its CPU in 04: JITWorker 772 plus HeapHelper 210, of 2,857 ms.
- Per turn it opens `/proc/self/statm` 3.6 times, `/proc/self/stat` 2.0 times, and
  `~/.claude/.credentials.json` 2.0 times. The credentials file does not exist (API-key auth), so every
  open is an ENOENT.
- Its stream-json timestamps split a turn into about 4 ms from tool_use to tool_result and about 4 ms
  from tool_result to the next request.
- pi's own phase medians: pre-request 0.96, dispatch 0.56, tool 1.74, post-tool 0.25 ms.

### 4.3 Tool-result policies

| Case | pi | Claude Code |
|---|---|---|
| Oversized Bash stdout (07: 50 MB, 1.2 MB, 3.9 MB) | 50 KB inlined in every later request (51,209 / 14,088 / 51,273 B) | output saved to `~/.claude/projects/…/tool-results/`; a 2 KB preview inlined (2,224 / 2,304 / 2,229 B) |
| Read of a 19.5 MB file, 3 times (07) | file read in full each time (rchar 60.5 MB, peak RSS 321 MiB, 25–32 ms per read); 51,200 B inlined | refused at 256 KB after reading about 512 KB (rchar 2.1 MB, 243 MiB, 8–16 ms); 457 B inlined |
| Repeat Read of an unchanged file (05) | full 267 B again | 93 B stub "Wasted call — file unchanged since your last Read…" (49 of 50 calls) |
| Small output (04 echo) | 8 B per result | 96 B per result: 6–8 B of output plus 87 B of extra text, and 547 B extra once every 10 tool results |

The mock logs only lengths, so what Claude Code's extra 87 B and 547 B text blocks say is unknown.
Excluding tool-result text, one turn adds about 245 B (Claude Code) vs about 230 B (pi) of message
framing.

### 4.4 Compaction and auxiliary requests

- *Compaction.* pi compacted at step 291 of 05: requests fell from 727,074 B (583 messages) to 97,427 B
  (65 messages), via one 235,561 B summary request. It also trimmed max_tokens from 16,384 to 14,186
  as the context filled. pi sent 10 summary requests in all (1 in 05, 9 in 12).
- *Claude Code never compacted.* In 05 its requests reached 1,739,512 B (601 messages), and the mock
  reported usage above Claude Code's 200,000-token context window. The mock does not enforce context
  limits, so this lab cannot show what Claude Code does at a real limit.
- *Claude Code's auxiliary requests.* One Haiku title request per TUI session that received a prompt
  (6 of 6): 4,087 B, no tools. No `count_tokens` requests. `GET /api/hello` appears only when
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is unset (01 chat-online ×5, 02 ×1). The idle TUI (02)
  sent no model request at all.

### 4.5 Run 05 is confounded by the mock mapping

Claude Code has no LS, Grep or Glob tool, so the mock maps those intents to Bash: `ls -la`,
`rg … | head -n 200` and `find … | head -n 1000`. pi uses its native tools, and pi's `find` failed on
every call: fdfind 8.x rejects `--no-require-git`.

Tool-result bytes per call (median):

| Intent | pi | Claude Code |
|---|---|---|
| grep | 10,438 B | 24,926 B (Bash fallback) |
| ls | 64 B (names only) | 504 B (`ls -la`) |
| find | 252 B (an error message) | 2,282 B (34 KB result saved to file, 2 KB preview) |

For intents with a native tool in both harnesses, the comparison is valid. Claude Code's `harness_ms` is
7.83 vs 3.65 ms for read, 14.07 vs 8.83 for write and 10.61 vs 5.36 for shell: a 1.6–2.1x factor, close
to 04's 2.0x.

Evidence:
- `SELECT harness, step, req_bytes, n_messages, n_tools, tools_bytes, sys_bytes, tool_result_bytes FROM mock_requests WHERE run='04-loop-200' AND step IN (0,200)` → `claude-code|0|112047|1|24|74162|27325|0 ; claude-code|200|189111|401|24|74162|27325|28290 ; pi|0|5727|1|4|2841|2578|0 ; pi|200|53091|401|4|2841|2578|1690`
- `SELECT harness, run, COUNT(*), SUM(req_bytes)/1e6, SUM(tools_bytes+sys_bytes)/1e6 FROM mock_requests WHERE aux=0 GROUP BY harness, run` → 04 CC 201 requests, 30.20 MB, fixed 20.40 MB (67.5%); pi 201, 5.89 MB, fixed 1.09 MB (18.5%); 05 CC 276.76 / 30.55 MB; pi 108.65 / 2.30 MB. (Re-run for this report: `mr04|claude-code|201|30.2 ; mr04|pi|201|5.89`.)
- `SELECT n_tools, tools_bytes, sys_bytes, count(*) FROM mock_requests WHERE harness='claude-code' GROUP BY 1,2,3` → 24/74,162/27,325 in json runs; 27/92,164/27,369 in 01 chat-online; 27/85,129/27,444 in the TUI sessions of 11 and 21.
- `SELECT SUM((req_bytes+3)/4) FROM mock_requests WHERE harness='claude-code' AND run='04-loop-200'` → 7,550,303, identical to the `input_tokens` in the result event of `lab/out/claude-code/runs/04-loop-200/events-loop200.jsonl`.
- Interpolating `proc_samples` (utime+stime) of the agent at `model_turns.t` for idx 0 and 200 → 04 pi loop 981.1 ms = 4.91 ms per turn; CC 2,636.9 ms = 13.18 ms per turn; 05 pi 7.29, CC 16.66.
- `syscalls` per agent: 04 total minus the 23 median session, divided by 180 → pi (22,280 − 9,325)/180 = 72.0; CC (77,327 − 11,957)/180 = 363.2.
- `model_turns.harness_ms` medians by idx bucket → 04 pi 14.10 / 5.18 / 3.71 / 3.89 / 3.98 / 4.41; CC 39.08 / 9.60 / 8.57 / 8.05 / 8.16 / 8.35.
- `mock_requests` tool_result_bytes deltas in 07 → pi bash 51209, 14088, 51273, 17; pi read 51200 ×3; CC Bash 2224, 2304, 2229, 104; CC Read 457 ×3. `lab/out/claude-code/runs/07-big-output/events-*.jsonl` contains `<persisted-output> Output too large (48.2MB). Full output saved to: …/tool-results/btp2v8zzt.txt … Preview (first 2KB)` and `File content (512KB) exceeds maximum allowed size (256KB)`.
- `SELECT harness, run, path, aux, summary, model, req_bytes, n_tools FROM mock_requests WHERE aux=1` → CC 11 ×2 and 21 ×4: claude-haiku-4-5-20251001, 4,087 B, 0 tools; pi 05 ×1 (235,561 B) and 12 ×9 summaries. `SELECT COUNT(*) FROM mock_requests WHERE path LIKE '%count_tokens%'` → 0.
- `SELECT step, req_bytes, n_messages, max_tokens FROM mock_requests WHERE harness='pi' AND run='05-loop-mixed-300' AND step>=286` → 291: 727,074 / 583 / 14,186; 292: 97,427 / 65 / 16,384.
- `SELECT json_extract(reply_json,'$.mapped'), count(*) FROM mock_requests WHERE run='05-loop-mixed-300' GROUP BY harness, 1` → CC ls→shell 50, grep→shell 50, find→shell 50; pi ls 50, grep 50, find 50. `lab/out/pi/runs/05-loop-mixed-300/events-mixed300.jsonl`: find → `error: Found argument '--no-require-git' which wasn't expected … Usage: fdfind`, isError true, 50 of 50.

---

## 5. Tool execution and processes

Runs: [cc/03-each-tool](http://localhost:8765/explorer/#/h/claude-code/run/03-each-tool) ·
[pi/03-each-tool](http://localhost:8765/explorer/#/h/pi/run/03-each-tool) ·
[cc/04-loop-200](http://localhost:8765/explorer/#/h/claude-code/run/04-loop-200) ·
[pi/04-loop-200](http://localhost:8765/explorer/#/h/pi/run/04-loop-200) ·
[cc/06-parallel-tools](http://localhost:8765/explorer/#/h/claude-code/run/06-parallel-tools) ·
[pi/06-parallel-tools](http://localhost:8765/explorer/#/h/pi/run/06-parallel-tools) ·
[cc/08-cpu-child](http://localhost:8765/explorer/#/h/claude-code/run/08-cpu-child) ·
[pi/08-cpu-child](http://localhost:8765/explorer/#/h/pi/run/08-cpu-child) ·
[cc/22-env-leak](http://localhost:8765/explorer/#/h/claude-code/run/22-env-leak) ·
[pi/22-env-leak](http://localhost:8765/explorer/#/h/pi/run/22-env-leak)

### 5.1 How a shell command runs

- **pi** calls `spawn('/bin/bash', ['-c', cmd], {detached: true})`: one process group per call. bash
  execs simple commands in place, so `uname -a` is one process, and `sudo -n id` is a direct child of
  pi.
- **Claude Code** runs `bash -c 'source ~/.claude/shell-snapshots/snapshot-bash-*.sh … && eval …'`.
  bash cannot exec straight into the program, so an external command costs one extra process
  (`uname -a` = bash + uname). On every call that shell re-reads the snapshot (4.8 KB), `~/.bashrc` and
  `/etc/bash.bashrc`, and writes `pwd` to `/tmp/claude-XXXX-cwd`.

Steady-state cost per `echo` in 04 (n=200): 0.98 ms CPU and 13,226 B rchar (Claude Code) vs 0.82 ms*
and 4,320 B (pi). For trivial commands the tool child is a small share of the turn, 0.8–1.0 ms of
5–14 ms; the harness process dominates.

### 5.2 First Bash call of a session and per-session setup

On its first Bash call, Claude Code builds the snapshot. In one-turn chats with no Bash call there is no
snapshot. The build does the following:
- 4 fork+exec attempts at `zsh --version` (`/bin/zsh`, `/usr/bin/zsh`, `/usr/local/bin/zsh`,
  `/opt/homebrew/bin/zsh`). They fail because zsh is not installed.
- `bash -c env`.
- A login shell, `bash -c -l SNAPSHOT_FILE=…`, with 19 children: 13 programs (id, head ×3, cut,
  grep ×2, awk, sed ×2, cat ×4) and 6 subshell forks.

That is 26 tool-window processes and 9.6–10.9 ms CPU (04, 05, 06, 08). It adds about 30 ms to the
first call's `harness_ms` (39 vs about 8.5 ms in 04).

In 03 the first call was `ls`, which took 38 ms (Claude Code) vs 8.3 ms (pi). About 8 ms of that
difference is the Bash-fallback penalty seen in steady state (05), so the snapshot accounts for about
26 ms.

A 20-turn session (23 no-probe) has 51 descendants for Claude Code vs 20 for pi:
- Claude Code: 20 per-call shells plus 31 setup processes. The setup processes are 3 git, 3 claude.exe
  (2 `--files` listings and 1 `--version`), 4 zsh forks, env, and the login shell with its 19 children.
  Median setup CPU is 20.4 ms; all descendants total 40.6 ms.
- pi: 20 bash processes, 16.4 ms.

### 5.3 Processes per call when the mapping differs (05)

| Intent | pi | Claude Code |
|---|---|---|
| ls | 0 processes (in-process) | 2: bash + ls, 1.76 ms |
| grep | 2 rg (one is `rg --version`), 4.91 ms | 3: bash + `/usr/bin/rg` + head, 6.77 ms |
| find | 3 (pi + 2 fdfind, failing), 2.13 ms | 4: bash ×2 + head + `claude.exe -S dfs -regextype findutils-default` (the embedded bfs), 5.68 ms |
| read / write | 0 | 0 |
| shell | 1, 0.93 ms | 1, 0.99 ms |

Claude Code's snapshot rewrites a literal `grep` typed inside a Bash command to the embedded ugrep
(`claude.exe -G --ignore-files …`, seen in 22). In 05 the grep intent ran the system `rg` instead.

**Two hidden per-call costs:**
- *pi does not cache binary checks.* Before every grep and find call it runs a blocking
  `spawnSync(<tool>, ['--version'])`: rg 1.97 ms, fd and fdfind 1.46 ms each
  (`pi-src/coding-agent/src/utils/tools-manager.ts`, called from `grep.ts:119` and `find.ts:172`).
  Node's PATH walk adds failed execve attempts: 8 per grep call and 14 per find call, including 50
  failed `fd` spawns before falling back to `fdfind`.
- *Claude Code watches the workspace.* Its `fs.watch` thread reacted to a tool writing a 200 MB file in
  `/workspace` (08) with 23,364 inotify reads (751 KB). That step cost the agent 115.7 ms of CPU in
  0.86 s, against 6.9 ms for pi. During the earlier 5 s python step Claude Code used 26.4 ms/s
  (including its first-call snapshot work; about 20 ms/s in steady state per 06) against 11.9 ms/s* for
  pi.

### 5.4 Parallel tool calls (06)

One model message held 16 shell calls (`sleep 1; python3 …; echo`), three rounds in all. Both harnesses
received identical shell-only calls, so no mapping effect is involved.
- **pi** ran all 16 at once: max 16 concurrent, about 1.05 s per round, 3.4 s wall. pi runs tool calls
  with `Promise.all` and no concurrency cap (`docs/ARCHITECTURE.md` §3, "A turn, end to end").
- **Claude Code** ran them one at a time: max 1 concurrent, about 16.6 s per round, 50.0 s wall.

Claude Code's agent used 1,607 vs 458.5* ms of CPU, or 29.2 vs 4.1 ms per call. That comes from 16x the
wall time at about 20 ms/s, not from a higher burn rate. The children cost a little more under Claude
Code: python3 averaged 21.7 vs 17.7 ms, and the per-call bash had a median of 2.29 vs 1.24 ms.

The protocols also add different amounts per round: pi +17 messages (OpenAI tool messages), Claude Code
+2 (Anthropic content blocks).

Whether Claude Code parallelises read-only Bash commands or other concurrency-safe tools was not tested.

### 5.5 Tool output transport (22, 07)

- **pi:** `ls -l /proc/self/fd` from a tool shows fd1 → `socket:[591147]` and fd2 → `socket:[591149]`.
  This is how Node/libuv implements `stdio: 'pipe'`, so it is a runtime default, not a pi-specific
  choice.
- **Claude Code:** fd1 and fd2 both point to `/tmp/claude-1001/-workspace/<sid>/tasks/<id>.output`. A
  Bun pool thread reads that file back.

For 50 MB of output (07), Claude Code writes the 55.8 MB twice: once to `tasks/*.output`, then again to
`tool-results/` under `$HOME`. pi writes it once, to `/tmp/pi-bash-*.log`. Both keep one full copy on
disk after the run.

File-backed output makes backgrounding and crash recovery straightforward. Socket pairs cost file
descriptors per running tool: in 06 pi's agent peaked at 33 sockets, 11 pipes and 3 io_uring fds, while
Claude Code peaked at 3 sockets, 4 pipes, 1 pidfd and 1 inotify fd.

Evidence:
- `SELECT pid, comm, file, args FROM execs WHERE harness='claude-code' AND run='04-loop-200' AND pid BETWEEN 665 AND 690` → zsh `--version` ×4 (`/bin`, `/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`); `/bin/bash -c env`; `/bin/bash -c -l SNAPSHOT_FILE=/home/agent/.claude/shell-snapshots/snapshot-bash-1791961919017-l164iq.sh`; id, head, cut, grep … cat ×4; pid 690 `bash -c source …snapshot… && shopt -u extglob …`.
- `SELECT fn, file, args, COUNT(*), AVG(dur_ms) FROM spawns WHERE harness='pi' AND run='04-loop-200'` → `spawn | /bin/bash | -c echo turn-0 … | 200 | 1.51 ms | detached=1`.
- `tool:*` processes per tool-call window → 04 CC first call 26 processes, 10.53 ms; later 199 calls 1 process, 0.97 ms; pi first 1 process, 0.91 ms; later 0.80 ms.
- `SELECT avg(cpu_ms), avg(rchar) FROM procs WHERE harness='claude-code' AND run='04-loop-200' AND argv LIKE 'bash -c source%'` → n=200, 0.982 ms, 13,226 B; pi bash n=200, 0.822 ms, 4,320 B.
- Descendants of each no-probe agent in 23 (`procs` ppid tree), median → pi 20, 16.4 ms, 0 setup; CC 51, 40.6 ms, 31 setup, 20.4 ms.
- `SELECT run, fn, file, args, COUNT(*), AVG(dur_ms) FROM spawn_sync WHERE harness='pi' GROUP BY 1,2,3,4` → 05: fd `--version` 50 × 1.46 ms; fdfind `--version` 50 × 1.46; rg `--version` 50 × 1.97.
- `SELECT comm, op, ftype, SUM(ops), SUM(bytes) FROM file_io WHERE harness='claude-code' AND run='08-cpu-child' AND pid=4152` → `fs.watch read anon-inode 23,364 ops, 751,360 B`.
- Overlap sweep of `procs` with comm `sleep` and role `tool:%` in 06 → pi 48 processes, max concurrent 16; CC 48, max concurrent 1. `SELECT harness, wall_s FROM runs WHERE run='06-parallel-tools'` → pi 3.412, CC 50.004 (re-run for this report).
- `SELECT harness, role, COUNT(*), SUM(cpu_ms) FROM procs WHERE run='06-parallel-tools' GROUP BY 1,2` → agent CC 1,607.1 ms, pi 458.5 ms.
- `lab/out/*/runs/22-env-leak/events-env.jsonl`, tool result of `ls -l /proc/self/fd` → pi `1 -> socket:[591147]`; CC `1 -> /tmp/claude-1001/-workspace/a50b0647-…/tasks/bpk741n05.output`.
- `SELECT role, op, ftype, file, sum(bytes) FROM file_io JOIN procs … WHERE run='07-big-output' GROUP BY … HAVING sum(bytes)>500000` → pi tool unix write 55,841,543; agent writes `/tmp/pi-bash-*.log` 55.84 MB. CC fold writes `tasks/btp2v8zzt.output` 50,499,999; agent reads `tasks/*.output` 55,956,119; agent writes `tool-results/*` 55,841,526.
- `SELECT kind, max(count) FROM fd_types JOIN procs … WHERE role='agent' AND run='06-parallel-tools' GROUP BY kind` → pi socket 33, pipe 11, io_uring 3; CC socket 3, pipe 4, pidfd 1, inotify 1.

---

## 6. Timeouts, kill semantics and orphans (09)

Runs: [cc/09-timeout-orphans](http://localhost:8765/explorer/#/h/claude-code/run/09-timeout-orphans) ·
[pi/09-timeout-orphans](http://localhost:8765/explorer/#/h/pi/run/09-timeout-orphans)

The scenario runs `sleep 30` and `sudo sleep 302` with a 2 s timeout, plus a `(sleep 300 &)` subshell
escape and a `setsid sleep 301` escape.

| | pi | Claude Code |
|---|---|---|
| Wall time | 4.68 s (step 4.60 s) | 304.45 s |
| Agent CPU | 373.6 ms* | 2,898.9 ms |
| `sleep 30` at 2 s | SIGKILL (9) to the process-group leader, +2.00 s after spawn | `ps -A -o pid= -o ppid=` at 2,208 ms, then SIGTERM (15) to bash and to sleep at 2,217 ms; result "Exit code 143" |
| `sudo sleep 302` at 2 s | SIGKILL to sudo at 4,460 ms. The root child gets nothing: sudo cannot forward SIGKILL, and an unprivileged agent cannot signal a root process | no signal. The tool result says it was "moved to the background (ID: bar07qi2k)… If it is still running after 10m … it will be stopped" |
| What happens next | returns at once | `-p` mode waits for the background task. It ends at 304,257 ms, then 1 extra model turn (origin `task-notification`, step 5, 114,999 B) and exit at 304,345 ms |
| Processes alive after exit | 3: `sleep 300` (ppid 1, still in the tool's pgid 48), `setsid sleep 301`, root `sleep 302` | 0. All three ended on their own at 302.2, 303.2 and 304.3 s while Claude Code waited |
| Model requests | 5 | 6 |

What this shows:
- **pi kills the process group only on timeout or abort**, with SIGKILL and no grace period
  (`process.kill(-pid, 'SIGKILL')`). The `(sleep 300 &)` orphan stayed in pi's tool group. It survived
  because that command exited normally, and pi does not clean up a group after a normal exit.
- **Claude Code walks the tree with `ps` and SIGTERMs what it finds.** That costs a fork and exec of
  `ps` (1.6 ms CPU) per kill. It gives children a chance to clean up, and sudo does forward SIGTERM. It
  also auto-backgrounds long commands. `sleep 30` was killed but `sudo sleep 302` was backgrounded
  under the same 2 s timeout. A rule exempting commands that start with `sleep` would explain this, but
  that is inferred from one unrepeated run, not confirmed from source.
- **In `-p` mode, a run's wall time is bounded by its slowest backgrounded child, not by the model
  loop.** Waiting cost Claude Code 8.0 ms/s for 290 s. The server closed the idle keep-alive connection
  at +70 s, and a new one was opened for the notification turn.
- **Neither harness reaches `setsid` escapes, processes owned by another uid, or children of commands
  that exited normally.** Only a cgroup kill, or a PID namespace per tool, catches everything.

Evidence:
- `SELECT round(s.t-r.t0), sender_comm, target_pid, target_comm, sig FROM signals s JOIN runs r USING(harness,run) WHERE run='09-timeout-orphans'` → pi `2226 pi→78905 sleep 9 ; 4460 pi→78911 sudo 9`; CC `2217 claude→4641 bash 15 ; 2217 claude→4642 sleep 15 ; 302228 sleep 4653→docker-init SIGCHLD ; 303243 sleep 4656→docker-init SIGCHLD ; 304257 sleep 4659→sudo SIGCHLD`. (Re-run for this report: pi signals `sleep|9, sudo|9`; claude signals `bash|15, sleep|15`.)
- `SELECT round(e.t-r.t0), comm, file, args FROM execs WHERE run='09-timeout-orphans' AND harness='claude-code'` → `2208 claude /usr/bin/ps -A -o pid= -o …`.
- `lab/out/*/runs/09-timeout-orphans/meta.json`, step 2 (`ps` after the harness exits) → pi `50 1 48 48 agent sleep 300 / 52 1 52 52 agent sleep 301 / 54 1 53 53 root sleep 302`; CC only docker-init and `sleep infinity`.
- `lab/out/claude-code/runs/09-timeout-orphans/events-timeout.jsonl` → the "moved to the background (ID: bar07qi2k)" tool result; `task_updated` status completed; `task_notification`; a second init; the second result with origin `{kind: task-notification}`.
- `SELECT t, step, n_messages FROM mock_requests WHERE run='09-timeout-orphans'` → CC steps 0–4 by +4.26 s, step 5 at +304.3 s (11 messages); pi 5 requests, last at +4.57 s.
- `tcp_events` in 09 for CC → connection :43822 established at 177 ms, CLOSE_WAIT → LAST_ACK at 70,262 ms; new connection :57554 SYN_SENT at 304,334 ms.

---

## 7. Files, environment and privileges

Runs: [cc/17-strace](http://localhost:8765/explorer/#/h/claude-code/run/17-strace) ·
[pi/17-strace](http://localhost:8765/explorer/#/h/pi/run/17-strace) ·
[cc/22-env-leak](http://localhost:8765/explorer/#/h/claude-code/run/22-env-leak) ·
[pi/22-env-leak](http://localhost:8765/explorer/#/h/pi/run/22-env-leak) ·
[cc/10-sudo](http://localhost:8765/explorer/#/h/claude-code/run/10-sudo) ·
[pi/10-sudo](http://localhost:8765/explorer/#/h/pi/run/10-sudo) ·
[cc/23-probe-overhead](http://localhost:8765/explorer/#/h/claude-code/run/23-probe-overhead)

### Opens and syscalls per 20-turn session (23 no-probe, n=10 medians)

| | pi | Claude Code |
|---|---|---|
| eBPF syscalls / futex | 9,030 / 2,939 | 11,882 / 4,548 |
| Harness-only syscalls (minus about 530 runc init syscalls) | about 8,500 | about 11,350 (1.34x) |
| Opens | 119 | 741 |
| Main contributors to opens | session JSONL about 41 (append per entry), settings.json 4, models.json 4 | `/proc/self/statm` 164, CA files under `/etc/ssl/certs` 156 (154 distinct; pi opens 1), missing `.credentials.json` 48, `/proc/self/stat` 40, `managed-settings.d` 34, `/workspace` 28, `/tmp/claude-XXXX-cwd` 20 |
| Syscalls by thread (median) | main 4,009; V8Worker 2,589; MainThread 1,332; libuv-worker 268 | main 5,403; Bun Pool 3,627; JITWorker 1,028; HeapHelper 875; HTTP Client 234 |

### Writes

- **Config writes.** Claude Code rewrites its config atomically, but mostly only on the first launch in
  a fresh HOME. In 23, session 1 did 13 fsync and 14 renameat: 11 `.claude.json.tmp.*` rewrites plus 2
  `settings.json.tmp` writes. Sessions 2–20 did 0 fsync and 1 renameat each. Every single-run scenario
  (09, 17, 22) is a first launch, because each scenario gets a fresh container. The strace census in 17
  shows fsync 15 and renameat 16, but it covers one 8-request first-launch session, and 2 of those
  renames are the Write/Edit tools' own atomic tmp-plus-rename writes. pi showed 0 fsync and 0 renameat.
- **Session transcripts.** Claude Code's start at about 220 KB and grow faster:

  | Scenario | pi | Claude Code |
  |---|---|---|
  | 09 | 10.6 KB | 223 KB |
  | 10 | 14.4 KB | 230 KB |
  | 22 | 12.6 KB | 226 KB |
  | 04 (201 turns) | 175,635 B | 718,723 B |
  | 05 | 0.95 MB | 5.50 MB |

  For a json-mode answer, Claude Code's transcript is about 210 KB fixed plus about 1 B per answer
  character (222 KB for 12k characters, 251 KB for 40k).
- **Other files Claude Code writes per session:** `~/.claude/sessions/<pid>.json` (a pid registry), an
  fswatch probe file, the shell snapshot, one `tasks/<id>.output` and one cwd file per Bash call. pi
  writes its session JSONL, plus `auth.json` and `models-store.json` (2 B each).

### Environment and privileges (22, 10)

- **Neither harness filters the environment passed to tools.** The canary `LAB_CANARY_API_KEY` reached
  tool children in both. `NODE_OPTIONS` (the probe) leaked into both as well.
- **pi adds 8 variables:** AI_AGENT=pi, PI_CODING_AGENT, PI_MODEL, PI_PROVIDER, PI_REASONING_LEVEL,
  PI_SESSION_FILE, PI_SESSION_ID and PI_SKIP_VERSION_CHECK. It also puts `~/.pi/agent/bin` first in
  PATH: in 09, `setsid` looked for `sleep` there first.
- **Claude Code adds 10:** AI_AGENT, CLAUDECODE, CLAUDE_CODE_{CHILD_SESSION, ENTRYPOINT, EXECPATH,
  MESSAGING_SOCKET, MESSAGING_TOKEN, SESSION_ATTENDED, SESSION_ID} and CLAUDE_PID. Any tool can
  therefore reach the harness over the Unix socket `/tmp/cc-socks/<pid>.sock` with the token.
- **API keys:** `ANTHROPIC_API_KEY` is visible to Claude Code's tools because the adapter passes it in
  the environment. pi reads its key from `~/.pi/agent/models.json`, which the same uid can read.
- **No sandbox in either.** Tools run with CapEff 0, NoNewPrivs 0, Seccomp 0, no controlling TTY, and
  each tool shell leads its own session.
- **sudo behaves identically.** Without a TTY, password-requiring calls fail at once (twice in each
  sudo.log diff). Both runs had 8 sudo processes, 15 root processes and the same 14 log lines, and
  tool-side CPU was 1,066 vs 1,027 ms. Only the wrappers differ: 3 vs 19 `tool:shell` processes.

Evidence:
- `SELECT file, sum(count)/10.0 FROM opens WHERE run='23-probe-overhead' AND pid IN (<no-probe agents>) GROUP BY file ORDER BY 2 DESC` → CC `/proc/self/statm 164.3, .credentials.json 48.1, /proc/self/stat 40.2, managed-settings.d 34.0, /workspace 28.3, /tmp/claude-XXXX-cwd 19.7`; CA files 156 opens (median) vs 1 for pi.
- `lab/out/claude-code/runs/17-strace/strace-trace.txt` → `openat(…/.claude/.credentials.json, …) = -1 ENOENT` ×18 (+5 unfinished); `strace-summary.txt` → CC fsync 15, renameat 16, fchmod 15, ftruncate 6; pi none.
- eBPF `syscalls` per agent in 23 by session order → CC session 1: fsync 13, renameat 14; sessions 2–20: fsync 0, renameat 1. `file_io` → `.claude.json.tmp` / `settings.json.tmp` writes only in session 1.
- `lab/out/*/runs/*/meta.json` `disk_usage` session files → 09 pi 10,550 vs CC 223,014; 10: 14,417 vs 230,329; 04: 175,635 vs 718,723; 05: 948,235 vs 5,495,320.
- `lab/out/*/runs/22-env-leak/events-env.jsonl` → pi `AI_AGENT=pi, LAB_CANARY_API_KEY=canary-value-123, PI_SESSION_FILE=…`; CC `ANTHROPIC_API_KEY=sk-ant-lab-mock-key-…, CLAUDE_CODE_MESSAGING_SOCKET=/tmp/cc-socks/30.sock, CLAUDE_CODE_MESSAGING_TOKEN=e57bded2…, LAB_CANARY_API_KEY=canary-value-123`; both `CapEff 0000000000000000, NoNewPrivs 0, Seccomp 0`, `not a tty`.
- `SELECT role, n, cpu_ms FROM v_cpu_by_role WHERE run='10-sudo'` → tool:sudo 8/8, tool:root 15/15, tool:shell 3 vs 19; the sudo.log diffs in `meta.json` are identical (14 lines).

---

## 8. Network

Runs: [cc/01-startup](http://localhost:8765/explorer/#/h/claude-code/run/01-startup) ·
[cc/02-idle-tui](http://localhost:8765/explorer/#/h/claude-code/run/02-idle-tui) ·
[pi/02-idle-tui](http://localhost:8765/explorer/#/h/pi/run/02-idle-tui) ·
[cc/13-concurrency](http://localhost:8765/explorer/#/h/claude-code/run/13-concurrency) ·
[cc/23-probe-overhead](http://localhost:8765/explorer/#/h/claude-code/run/23-probe-overhead)

- **Connection reuse is the same.** Each agent process holds one HTTP keep-alive connection: 201
  requests on 1 connection in 04, 2,139 requests on 69 connections for 69 agents in 13.
- **Bytes per 20-turn session (23 no-probe):**

  | | pi | Claude Code |
  |---|---|---|
  | tcp_tx | 182,287 B | 2,512,904 B (13.8x; the mock received 2.43 MB vs 168 KB) |
  | tcp_rx | 32 KB | 26 KB |
  | UDP | 25 B out, 48 B in (one DNS lookup) | same |

  Claude Code's real unix_rx is about 44.5 KB, mostly its `claude.exe --files` helper streaming the
  file listing back. pi's 7.5 KB is tool stdout over socket pairs.
- **Online one-shot `-p` (01, n=5).** Claude Code calls `/api/hello` on the base URL, then opens 4–6
  TLS connections per start to 160.79.104.10:443 (22 over 5 starts, presumed Anthropic) and 1 to
  34.149.66.165:443. Over offline that adds about +100 KB tx and +180 KB rx, plus an 18 KB larger model
  request. Online `sh -c uname` and `ps aux | grep -E "code|cursor|windsurf…"` (IDE detection) also run;
  the lab labels them `tool:*`. pi in json mode made no outside connections.
- **Online idle TUI (02).** Claude Code opened 8 TLS connections and held them until exit at 30.9 s:
  5 to 160.79.104.10, and 1 each to 185.199.108.133 (a GitHub range), 35.190.46.17 and 34.149.66.165
  (at 15.4 s).
  - Its main thread received 8.53 MB in-process within the first 2 s. No git process did the transfer.
  - It wrote 15.6 MB to 516 files under `~/.claude/plugins/marketplaces/claude-plugins-official/`
    (mermaid.min.js 3.57 MB, images, hook scripts) plus a 1 MB changelog.md.
  - The dataset cannot attribute bytes to individual remote addresses, so "downloaded from GitHub" is
    inferred from the file paths and the one GitHub-range IP.

  pi made 2 HTTPS GETs to pi.dev (`/api/latest-version` 200, `/api/report-install?version=0.87.1` 204):
  12.6 KB rx and 3.9 KB tx, with sockets closed 4.6–4.7 s after start.
- **Offline switches.** Claude Code has CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, DISABLE_TELEMETRY and
  DISABLE_ERROR_REPORTING; pi has PI_OFFLINE. With them set, Claude Code talked only to the mock in
  json mode. In TUI mode it still opened 1 connection to 35.190.46.17:443, but only on the first TUI
  launch in each fresh container (11: tui-fast; 21: tui-10k).
- **First-launch plugin files.** That same first offline TUI launch wrote about 13.2–14.3 MB of
  plugin-marketplace files (mermaid.min.js, pane-live.gif, PNGs) and cost 140–280 ms more CPU than later
  launches: 1,221 vs 944 ms at 40k characters, 970 vs 827 ms at 10k vs 20k. Later launches wrote about
  0.27 MB. Whether these files came over that connection or out of the binary was not determined. A
  design that creates a fresh container per task pays this every time unless HOME is pre-seeded. pi's
  TUI runs wrote 31–239 KB.

Evidence:
- `tcp_events`: SYN_SENT to :8080 per run vs `mock_requests` → 04 201 requests / 1 connection for both; 13 2,139 / 69 for both; 23 420 / 20 for both.
- `net_bytes` per no-probe agent in 23 (values above 2^31 taken modulo 4,294,967,285) → pi tcp_tx 182,287, tcp_rx 32,269, unix_rx 7,469; CC 2,512,904 / 26,377 / 44,549.
- `SELECT harness, phase, remote, new_state, count(*) FROM tcp_events WHERE run='01-startup' AND remote NOT LIKE '172.18.0.3%' GROUP BY …` → CC online 160.79.104.10:443 SYN_SENT 22 (6, 4, 4, 4, 4), 34.149.66.165:443 5; CC offline and all pi: mock :8080 only.
- `tcp_events` in 02 relative to agent start → CC 65 ms `/api/hello`; SYN to 160.79.104.10 at 101, 383, 402, 402 and 470 ms; 185.199.108.133 at 133; 35.190.46.17 at 505; 34.149.66.165 at 15,398; all 8 FIN_WAIT1 at 30,930. pi SYN to 172.67.221.13 at 194 and 288 ms, closed at 4,624 and 4,720 ms.
- `net_bytes` 02 totals → CC rx 8,532,824 (claude main thread 8,509,494) / tx 74,674; pi rx 12,604 / tx 3,876. `file_io` 02 writes → CC 516 files, 15,606,858 B; pi 4 files, 101,584 B.
- `SELECT * FROM http WHERE harness='pi' AND run='02-idle-tui'` → `https://pi.dev GET /api/latest-version 200 ; GET /api/report-install?version=0.87.1 204`.
- `file_io` write totals per CC TUI agent → pid 5928 (11, first TUI) 13,156,230 B; pid 15763 (21, first TUI) 14,284,884 B; pid 15940 (a later launch) 272,707 B.

---

## 9. Concurrency and capacity (13)

Runs: [cc/13-concurrency](http://localhost:8765/explorer/#/h/claude-code/run/13-concurrency) ·
[pi/13-concurrency](http://localhost:8765/explorer/#/h/pi/run/13-concurrency) ·
[cc/04-loop-200](http://localhost:8765/explorer/#/h/claude-code/run/04-loop-200) ·
[pi/04-loop-200](http://localhost:8765/explorer/#/h/pi/run/04-loop-200)

Each agent runs a 30-turn bash loop (31 requests). The batches are 1, 4, 16 and 32 agents with an
instant mock, then 16 agents with 400 ms model latency (n16lat). The bash intent mapped to Bash in all
2,070 tool turns for both harnesses, so there are no fallbacks. Values are per-agent medians for the
harness process (role `agent`).

| Metric | pi 0.87.1* | Claude Code 2.1.299 |
|---|---|---|
| CPU ms: n01 / n04 / n16 / n32 / n16lat | 431 / 587 / 600 / 615 / 654 | 690 / 796 / 1,061 / 1,247 / 1,391 |
| Run-queue wait ms: n01 / n04 / n16 / n32 / n16lat | 9.6 / 64 / 379 / 1,064 / 153 | 0.4 / 4.9 / 956 / 2,761 / 283 |
| Life ms: n01 / n16 / n32 / n16lat | 352 / 806 / 1,364 / 12,980 | 503 / 1,240 / 2,528 / 13,387 |
| `harness_ms` median (p90): n01 / n32 / n16lat | 4.3 / 23.1 (40) / 7.4 | 9.2 / 49.0 (118) / 19.3 |
| Peak RSS MiB: n01 / n32 / n16lat | 156 / 159 / 159 | 320 / 284 / 255 |
| PSS MiB: n32 / n16lat | 101 / 101 | 159 / 145 |
| cgroup anon per agent MiB: n01 / n04 / n16 / n32 / n16lat | 96 / 98 / 100 / 99 / 99 | 196 / 195 / 185 / 150 / 135 |
| Container memory peak: n16 / n32 | 1,641 / 3,262 MiB | 3,000 / 4,866 MiB |
| Container cores used: n32 / n16lat | 13.6 / 0.85 | 15.0 / 1.72 |
| Throughput at n32 | 20.4 sessions/s (span 1.57 s) | 11.3 sessions/s (span 2.84 s) |
| Tool and helper children per agent at n32 | 30 bash, 48.5 ms | 58.3 ms in `tool:*` (30 tool-call shells + snapshot shells and coreutils) + 15.1 ms `agent:helper` |

**CPU.** Claude Code uses 1.6–2.1x pi's CPU per agent, and the ratio widens under contention:
- From n01 to n32, Claude Code went 690 → 1,247 ms (+81%) and pi 431 → 615 ms (+43%).
- At n32, Claude Code's main thread doubled (410 → 822 ms) and its JITWorker took 286 ms per agent.
  pi's main thread rose 298 → 450 ms and its V8Worker threads 126 → 158 ms.
- The marginal harness CPU per turn, derived from 04 against n01, is 12.8 vs 4.8 ms.
- Run-queue wait reaches roughly the agent's own CPU at about 16 agents for Claude Code (wait/CPU 0.90).
  For pi it happens between 16 and 32 agents (0.63, then 1.73).
- Uncontended, pi waits longer (9.6 vs 0.4 ms at n01; 435 vs 4 ms in the single-agent 60 chunks/s
  stream). That may come from its wake-up pattern or the probe, so compare run-queue wait only from n16
  up.

**Memory, and what limits the agent count.**
- Claude Code's anon memory per agent falls as agents spend more wall time idle: 196 MiB at n01, 135 MiB
  at n16lat. That is consistent with JSC's GC catching up only when there is idle time. pi holds at
  96–100 MiB.
- Peak RSS overstates Claude Code: about 110 MiB of it is shared binary pages (RSS minus PSS). Use PSS or
  cgroup anon for capacity planning.
- **At 400 ms latency, CPU is not the limit.** The 16 latency agents used 0.85 vs 1.72 cores, or 0.053
  vs 0.107 cores per agent. By CPU alone that allows about 340 vs 167 agents on 18 vCPU.
- **Memory is the limit.** The VM had about 9.5–10.1 GB available before the batches, which caps pi at
  about 98–100 agents (about 5.4 per vCPU) and Claude Code at about 74 (about 4.1 per vCPU).
- These capacity figures are extrapolated from 16 agents. Real models respond in seconds, not 400 ms,
  which makes memory even more clearly the limit.
- At zero latency, throughput is the useful measure: 20.4 vs 11.3 sessions/s at 32 agents.

**Per-session overhead behind the gap:**
- Requests are 112–124 KB (Claude Code) vs 5.7–12.8 KB (pi). A 31-turn session sent 3,557 KB vs 277 KB.
- Claude Code runs 6 helpers per session (2 `claude.exe --files`, 1 `claude.exe --version`, 3 git) and
  a login-shell snapshot.
- Tool children stay small next to the harness process either way.

Evidence:
- `SELECT cpu_ms, runq_wait_ms, life_ms FROM procs WHERE run='13-concurrency' AND role='agent'`, bucketed by run marks (start/n04/n16/n32/n16-latency/end), median → pi n32 (n=32) 615.2 / 1,063.8 / 1,363.8; CC n32 1,247.0 / 2,760.5 / 2,527.7; n01 pi 431.4, CC 689.5; n16lat pi 653.6, CC 1,390.7. (Re-run: 69 agent rows per harness.)
- `cgroup_samples` for container `lab-agent`: max(mem), max(anon) per batch window minus the last sample before the batch → pi anon per agent 95.5 / 97.9 / 99.6 / 99.5 / 99.5 MiB; CC 195.6 / 195.1 / 184.5 / 149.8 / 134.6. Cores = cpu_us delta divided by agent span → pi n16lat 0.85, CC 1.72.
- `SELECT max(pss_kb) FROM mem_detail JOIN procs(role='agent')` per pid, median per batch → pi n32 101.1 MiB; CC n32 158.5, n16lat 145.1.
- `SELECT min(mem_available_kb)/1024, max(mem_available_kb)/1024 FROM vm_samples WHERE run='13-concurrency' GROUP BY harness` → pi 6,998–10,021 MiB; CC 5,606–10,135 (total 11,943). Just before n32 and n16lat: pi 9,510 / 9,508, CC 9,672 / 10,001.
- `threads` JOIN `procs` (role `agent`), per agent → CC n32 claude 821.9, JITWorker 285.9, HeapHelper 73.4 ms; pi 449.9, V8Worker 157.6.
- `SELECT json_extract(reply_json,'$.mapped'), count(*) FROM mock_requests WHERE run='13-concurrency' GROUP BY harness, 1` → `['shell']` in 2,070 of 2,070 tool turns for each harness.

---

## 10. Output modes and TUI rendering (11, 21)

Runs: [cc/11-stream-modes](http://localhost:8765/explorer/#/h/claude-code/run/11-stream-modes) ·
[pi/11-stream-modes](http://localhost:8765/explorer/#/h/pi/run/11-stream-modes) ·
[cc/21-tui-scaling](http://localhost:8765/explorer/#/h/claude-code/run/21-tui-scaling) ·
[pi/21-tui-scaling](http://localhost:8765/explorer/#/h/pi/run/21-tui-scaling)

Agent CPU from eBPF. Every figure in this section comes from a single run.

| Mode | pi* | Claude Code |
|---|---|---|
| json, 40k characters at full speed | 512 ms | 473 ms |
| print, 40k | 400 ms | 491 ms |
| rpc, 40k | 513 ms | n/a |
| json, 80k | 831 ms | 634 ms |
| stdout for json 40k | 2.68 MB in 10,050 writes (per-token events) | 88 KB in 3 writes (whole messages) |
| TUI 10k / 20k / 40k (21) | 429 / 506 / 673 ms | 970 (first launch) / 827 / 944 ms |
| Bytes written to the TTY, 10k / 20k / 40k | 137 / 265 / 526 KB | 5 / 5 / 5 KB |
| TUI 80k, includes 4 resizes and about 11 s of extra life | 1,399 ms, 4.2 MB to the TTY | 1,504 ms, 19 KB |
| TUI 80k before the first resize | about 0.89 s CPU, 1,024,199 B to the TTY | about 1.03 s CPU, 4,685 B |
| Per terminal resize on the 80k transcript | 562–990 KB written, 90–160 ms | 2–4 KB, 50–150 ms |
| json at 60 chunks/s (12k characters, 3,000 chunks, 55 s) | 2,168 ms | 2,331 ms |
| TUI at 60 chunks/s | 10,438 ms | 5,930 ms |
| tmux server CPU during the 60 chunks/s TUI | 312 ms | 80 ms |
| TUI CPU rate across the 55 s stream, in 6 slices | 133 → 155 → 171 → 195 → 212 → 235 ms/s | 147 → 107 → 108 → 91 → 91 → 74 ms/s |
| Peak RSS at 40k, json vs TUI (11) | 185 vs 258 MiB | 322 vs 396 MiB |

**pi writes the transcript into terminal scrollback.**
- TTY bytes grow linearly with length, about 13 B per character at full speed.
- Every resize replays the whole transcript.
- At a slow, steady stream, its cost per chunk grows with the length so far, so total cost is quadratic
  in length. The CPU rate climbed 133 → 235 ms/s, and 8,965 ms of the 10,438 ms ran on the single main
  event-loop thread (which also hosts the probe).
- At full speed pi merges updates into fewer renders, costing only +160–196 ms over headless.

**Claude Code repaints only the visible screen.**
- It writes about 5 KB whatever the length, and 2–4 KB per resize.
- It carries a fixed cost of about +470 ms over headless at full speed (944 vs 473 ms).
- At 60 chunks/s its rate falls over time. The terminal pays less too: tmux used 80 vs 312 ms.

**Headless modes are not like-for-like.** pi `--mode json` emits one `message_update` event per token
chunk, about 268 B each, which costs about 112 ms per 40k characters compared with print mode. Claude
Code's adapter runs stream-json without `--include-partial-messages`, so it emits only whole messages,
and its json and print costs match. The extra CPU per 1k characters is about 8 ms for pi and about 4 ms
for Claude Code. Claude Code's TUI also sends one extra 4 KB Haiku title request.

Evidence:
- `SELECT cpu_ms, argv FROM procs WHERE role IN ('agent','tui:tmux') AND run IN ('11-stream-modes','21-tui-scaling')` → pi tui-60tps agent 10,437.6, tmux server 311.7; CC 5,930.2 / 80.2; pi json-fast 512.1, print 400.2, rpc 512.6, json-80k 831.3; CC 472.6 / 491.1 / 634.3.
- `proc_samples` utime+stime deltas over 6 equal slices from request t to t_end → pi TUI 133.3 / 155.2 / 170.5 / 194.5 / 212.0 / 235.0 ms/s; CC TUI 147.2 / 107.3 / 108.4 / 90.7 / 90.7 / 74.1.
- `SELECT ftype, SUM(bytes), SUM(ops) FROM file_io WHERE op='write' AND pid=<agent> GROUP BY ftype` → pi tui-60tps char-dev 1,907,810 B in 9,729 writes; CC 485,247 B in 237. TUI 10k/20k/40k: pi 136,830 / 264,868 / 526,319 B; CC 5,229 / 5,141 / 5,116 B. json-fast: pi fifo 2,682,915 B in 10,050 writes; CC 88,266 B in 3.
- Window from each resize mark −100 ms to +2,400 ms → pi 160 / 90 / 110 / 110 ms with 638,510 / 990,367 / 562,376 / 990,367 B; CC 50 / 90 / 150 / 130 ms with 3.1 / 4.4 / 2.1 / 4.2 KB.
- `threads` for the 60 chunks/s TUI pids → pi main 8,964.9 ms, V8Worker 1,453.1; CC claude 3,120.1, JITWorker 1,755.2, HeapHelper 560.4.
- `SELECT harness, tag, wall_s FROM steps WHERE run='11-stream-modes'` → tui-60tps pi 60.308 s, CC 59.809 s; json-60tps 56.735 / 55.158 s (re-run for this report).

---

## 11. Retries (19)

Runs: [cc/19-retries](http://localhost:8765/explorer/#/h/claude-code/run/19-retries) ·
[pi/19-retries](http://localhost:8765/explorer/#/h/pi/run/19-retries)

The gaps between attempts are taken from mock request timestamps.

| Case | pi | Claude Code |
|---|---|---|
| 503 ×3 | 2,010 / 4,012 / 8,013 ms; wall 14.3 s | 578 / 1,135 / 2,022 ms; wall 4.07 s |
| 429 ×2 with `Retry-After: 2` | 2,008 / 4,006 ms: ignores the header value and keeps its fixed schedule, so the second wait is twice what was asked | 2,005 / 2,007 ms: waits exactly as asked; wall 4.28 s |
| 500 ×8 | 2,011 / 4,007 / 8,008 ms, then gives up; wall 14.3 s | 526 / 1,034 / 2,391 / 4,289 / 8,726 / 16,450 / 38,430 / 32,373 ms, then succeeds on attempt 9; wall 104.5 s |
| Policy | maxAttempts 3; delayMs 2,000 / 4,000 / 8,000; no jitter | max_retries 10; min(0.5 s × 2^n, 32 s) plus 0.6–20% jitter on top (max seen 38.4 s) |
| After giving up | `auto_retry_end success:false`, `agent_settled`, last stopReason `error`, **exit code 0**, empty stderr | not observed |
| Agent CPU: r503 / r429 / r500 | 558 / 388 / 508 ms* | 477 / 430 / 841 ms |

- Each failed attempt produced exactly one request in both harnesses. No hidden retries at the HTTP
  client level showed up, which contradicts hypothesis 11 in `docs/ARCHITECTURE.md` ("two nested retry
  rhythms").
- A pipeline that checks only pi's exit code will treat a failed task as a success.
- pi's waits are identical on every run, so many pi agents hitting the same outage would retry in
  lockstep. That is inferred from the schedule, not tested.
- Claude Code's give-up time is a projection: two more waits of about 32–40 s would put it at about
  170–185 s.

Evidence:
- Gaps from `LAG(t)` over `mock_requests` in 19, per harness (re-run for this report) → CC `577.8, 1135.2, 2022.1, 2004.7, 2006.9, 526.4, 1034.1, 2391.1, 4288.9, 8725.8, 16450.1, 38430.0, 32373.2`; pi `2009.9, 4012.5, 8012.8, 2007.5, 4006.4, 2011.4, 4007.3, 8008.1`.
- `grep auto_retry lab/out/pi/runs/19-retries/events-r500.jsonl` → `auto_retry_start` attempts 1/2/3, maxAttempts 3, delayMs 2000/4000/8000; then `auto_retry_end success:false, finalError '500: … injected 500'`.
- `api_retry` events in `lab/out/claude-code/runs/19-retries/events-r*.jsonl` → max_retries 10; r429 retry_delay_ms 2000, 2000; r500 516, 1006, 2383, 4282, 8715, 16444, 38418, 32362; result `end_turn`, duration_api_ms 104,259.
- `SELECT tag, rc, wall_s FROM steps WHERE run='19-retries'` → pi r500 rc 0, 14.317 s (`stderr-r500.txt` is 0 bytes); CC r500 rc 0, 104.496 s.

---

## 12. What this means if you build on a harness

The two harnesses differ by 2–20x on most axes. The data points to these design choices as the cause.

1. **Tool schema size sets a per-turn floor.** Claude Code's 24 tools (74 KB of schemas) and 27 KB
   system prompt add 101.5 KB to every request, whether or not the tools are used. Over 200 turns that
   was two-thirds of the 30.2 MB sent. pi's 4 tools cost 5.4 KB. Prompt caching on a real API cuts the
   token bill, but not the bytes, the JSON build and serialise work, or the request-parse cost. A
   harness should advertise only the tools a task needs, keep schema descriptions tight, and load
   rarely used tools on demand. Note that Claude Code's set grows with mode (27 tools in the TUI and
   when online).

2. **Every turn resends the whole conversation, so tool-result policy decides growth.** Both harnesses
   resend history on each turn (stateless APIs).
   - Claude Code's policies keep big results out of context: a persisted file with a 2 KB preview, a
     256 KB Read limit, and a stub for repeat reads. With them, a 50 MB output adds about 2.2 KB to
     every later request instead of 51 KB.
   - Small per-message additions add up: Claude Code's 87 B on every tool-result message made its
     echo-loop requests grow faster than pi's (385 vs 237 B per turn).
   - Compaction is the other lever. pi compacted at 727 KB down to 97 KB. Claude Code never compacted
     under a mock with no context limit.

3. **The runtime shapes memory, startup and observability more than harness logic does.**
   - A single-file Bun binary starts fast (8.7 ms `--version`, first request at 130 ms). Its roughly
     120 MiB of binary pages are shared across agents.
   - But its anon memory is larger (about 125 vs 93 MiB marginal per agent), and it runs JIT and GC
     helper threads (JITWorker 22–27% of agent CPU; 7 HeapHelper threads on 18 vCPU).
   - Node gave pi a fixed 12-thread profile and in-process instrumentation. With Bun, Claude Code's
     heap cannot be inspected at all from this lab.
   - If agents-per-box matters, measure anon/PSS memory at your real model latency. Memory, not CPU,
     capped both harnesses at 400 ms latency.

4. **A shell wrapper per command costs per call.** A bare `bash -c` (pi) lets bash exec straight into
   simple commands. Claude Code's snapshot wrapper preserves the user's functions, aliases and options,
   at these costs:
   - one extra process for external commands;
   - three rc and snapshot reads per call (13.2 vs 4.3 KB);
   - about 0.16 ms CPU per call;
   - a one-time 26-process, about 10 ms snapshot build that adds about 30 ms of first-call latency.

   Also watch for hidden per-call work: pi's uncached `spawnSync --version` probes (8–14 execve
   attempts per grep or find call), and Claude Code's inotify watcher (23,364 events when a tool wrote
   200 MB in the workspace).

5. **Thread usage and syscalls follow the runtime.** In the echo loop Claude Code made about 5x pi's
   marginal syscalls per turn (363 vs 72), almost all of them futex waits from GC and pool threads. It
   also polls `/proc/self/statm` and a missing credentials file several times per turn. If you embed a
   harness in a dense fleet, these wake-ups show up as run-queue contention: at 32 agents the median
   wait was 2.8 s for Claude Code vs 1.1 s for pi.

6. **Choose deliberately between parallel and serial tool calls.** Unbounded `Promise.all` (pi) turned
   16 calls into 16 concurrent processes and 33 sockets, in 3.4 s. Serial execution (Claude Code)
   capped resource use at 1 child but took 50.0 s. A bounded pool, with read-only tools marked
   concurrency-safe, sits between the two.

7. **TUI rendering strategy decides whether long answers are cheap.** Appending to scrollback (pi) makes
   the cost per chunk grow with transcript length, makes every resize replay the transcript (up to
   about 1 MB per resize at 80k characters), and loads the terminal (tmux 4x). Repainting the visible
   screen (Claude Code) has a higher fixed cost but stays flat. Headless modes avoid all of this. Choose
   per-token or whole-message JSON events based on what the consumer needs: per-token events cost pi
   about 112 ms and 2.6 MB of stdout per 40k characters.

8. **Kill semantics need a container-level backstop.**
   - A process-group SIGKILL (pi) is one syscall and final. It misses `setsid` escapes and processes
     owned by another uid, as with sudo.
   - A `ps` tree walk with SIGTERM (Claude Code) is gentler but also misses re-parented escapes.
   - Claude Code's auto-backgrounding means a headless run lasts as long as its slowest background task:
     302 s here.
   - Neither harness cleans up after commands that exit normally.

   If you need guarantees, run each tool in its own cgroup or PID namespace and kill that.

9. **Retry policy is a fleet-level decision.**
   - Use jitter, so a fleet does not retry in lockstep.
   - Honour Retry-After.
   - Set a give-up budget that matches the task.
   - Exit non-zero when the task did not complete. pi's exit 0 after giving up hides failures from
     orchestration.

10. **Startup side effects are paid in every fresh container.** With nonessential traffic on, Claude
    Code calls home and downloads the plugin marketplace: 8.5 MB received and 15.6 MB written in the
    idle TUI. A one-shot `-p` run lives 805 ms instead of 143 ms. Even offline, its first TUI launch per
    container writes about 14 MB, and its first session rewrites config with fsync. A container-per-task
    design should pre-seed HOME and set the offline switches. pi's equivalents are small: 2 pi.dev
    requests in the TUI, and nothing in json mode.

11. **Treat tool environment as untrusted by default.** Both harnesses pass the full environment
    (including secrets) to tools, run them unsandboxed, and add their own markers. Claude Code adds a
    messaging socket and token that any tool can use. If tools run untrusted code, filter the
    environment and drop privileges in the harness or the container.

---

## 13. Caveats

- **Mock mapping.** Claude Code 2.1.299 has no LS, Grep or Glob tools, so ls/grep/find intents become
  Bash commands with larger outputs and extra processes (`mock_requests.reply_json` `mapped`). This
  confounds 03, 05 and 17. pi's `find` failed on every call (fdfind 8.x rejects `--no-require-git`), and
  only pi compacted in 05. Use 04, 06, 09, 10, 13, 22 and 23 for like-for-like comparisons.
- **Runtime premise.** The brief describes both harnesses as Node.js. Claude Code 2.1.299 in this image
  is a Bun binary, so it has no probe data: no JS heap, GC, HTTP or loop-lag figures. Its memory
  analysis rests on `procs`, `proc_samples` and `mem_detail` only. The lab adapter still declares
  `runtime: node`.
- **Probe on pi only.** pi's numbers in 01–22 include the probe (`*`). It adds about 15.5 ms CPU per
  20-turn session and about 0.8 ms per turn, and its 5 ms timer inflates pi's idle CPU, wake-ups and
  run-queue wait. Probe-free pi figures exist only for 23. Every pi-vs-Claude-Code gap where Claude Code
  costs more is therefore slightly understated.
- **Versions.** pi 0.87.1 and Claude Code 2.1.299 only. Claude Code's tool set, auto-background rule and
  startup traffic are version-specific.
- **Single machine, mostly single runs.** One 18-vCPU arm64 Docker Desktop VM. Thread-pool sizes may
  scale with core count. 02, 04, 05, 06, 07, 09, 11, 13, 19 and 21 ran once per harness; medians there
  are across turns, calls or agents within one run. Capacity figures extrapolate from 16 agents.
- **Mock limits.** No prompt caching, no context-limit enforcement and instant replies, except the
  n16lat batch (400 ms) and the 60 chunks/s streams. Token figures that Claude Code reports are the
  mock's `ceil(bytes/4)`.
- **First-launch effects.** Each scenario runs in a fresh container, so single-session scenarios show
  Claude Code's first-launch work (config rewrites with fsync, plugin files in the TUI). Steady-state
  sessions in 23 show 0 fsync.
- **Online vs offline.** 02 ran online for both harnesses. All other scenarios used the offline env,
  except the online phase of 01. Claude Code's idle-TUI CPU, RSS and disk figures include the
  marketplace download.
- **Lab artefacts excluded or corrected:**
  - `net_bytes.unix_rx` for every Claude Code process is corrupted, for example 115,964,188,397 B in 02,
    about 55.8 GB per process in 04, 22 and 23, and 79 samples in 13. Each value is k × 2^32 plus the
    real bytes: a 32-bit −EAGAIN passes the `(int64)retval > 0` filter in `lab/monitor/proctrace.bt`. Values here
    are taken modulo 4,294,967,285.
  - Claude Code's 4 failed `zsh --version` forks show about 225–232 MiB peak RSS. That is inherited from
    the parent before exec, not real memory.
  - The lab labels Claude Code's snapshot, IDE-detection and zsh-probe processes `tool:*`. They are
    harness overhead.
  - `procs.threads` includes 5 runc threads for docker-exec'd agents; they are subtracted here.
  - Agent syscall totals include about 530 runc init syscalls.
  - The per-second eBPF aggregates lose the final partial second of a run (pi 04 ends 389 ms after its
    last aggregate), so exact per-process counters from `procs` are preferred.
  - `proc_samples` CPU has 10 ms tick resolution.
- **Remote endpoints.** These are identified by IP only. 160.79.104.10 is presumed to be Anthropic and
  185.199.108.133 GitHub. 34.149.66.165 and 35.190.46.17 are Google Cloud addresses whose service was
  not identified.
- **Untested.** Whether Claude Code parallelises read-only tools; what Claude Code does at a real
  context limit or after exhausting its retries; probe-free idle cost for pi.
