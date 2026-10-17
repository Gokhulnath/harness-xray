# pi on Vercel, three ways: measured

2026-10-15. One Next.js app on Vercel (Hobby, Fluid compute, region iad1), one coding task, three places to run pi:

| Variant | Where pi's loop runs | Where tools run | Code |
|---|---|---|---|
| **A**: pi entirely in Vercel | Vercel function (`@earendil-works/pi-coding-agent` 1.2.0 as a library) | the same Vercel function (pi's built-in bash/read/write/edit) | `app/app/api/chat`, `app/lib/tools-local.js` |
| **B**: Vercel + E2B | Vercel function (same pi) | E2B sandbox, via pi's pluggable tool operations | `app/lib/tools-e2b.js` |
| **C**: Vercel + Rivet + E2B | Rivet Actor on Rivet Compute (`@rivet-dev/pi` 0.5.3 = pi-durable 1.2.0) | E2B sandbox, via `@rivet-dev/sandbox-adapter/e2b` | `rivet-worker/`, `app/app/api/chatc` (proxy only) |

Task (all variants): a 3-file JavaScript project with two bugs; "run the tests with npm test, find the bugs in cart.js, fix
them, re-run until they pass". Model: `nvidia/nemotron-3-super-120b-a12b:free` via OpenRouter.

**Normal load: every run passed (15 sequential, 9 concurrent, 3 cold-start). Heavy and crash tests: section 3c.**

## 1. Where the compute went (sequential runs, medians)

| Per task | A: all in Vercel | B: Vercel + E2B | C: Rivet + E2B |
|---|---|---|---|
| Wall time | 48.3 s (32.2-76.8) | 52.8 s (45.3-69.3) | 32.6 s (24.2-41.8) |
| First token / first event | 994 ms | 1,126 ms | 2,042 ms (Rivet first call 915 ms + watch setup) |
| Tool calls / model requests | 9 / 10 | 8 / 9 | 5 / 8 |
| **Vercel CPU, warm instance** (function + tool processes) | **1,163 ms** (543 + 630) | **799 ms** | **207 ms** |
| **Vercel CPU, cold instance** | **3,619 ms** | **3,114 ms** | **449 ms** |
| Of which: loading pi on a cold instance | 1,494 ms | 1,451 ms | 0 (pi is not in Vercel) |
| Vercel function peak RSS | 181 MiB | 181 MiB | 115 MiB |
| Rivet worker CPU per task | - | - | 730 ms |
| Rivet worker RSS (idle → active) | - | - | 177-196 → 204 MiB |
| E2B sandbox create | - | 330 ms | 549 ms |
| E2B sandbox lifetime per task | - | 52.5 s (killed at the end) | 58.5 s (paused ~25 s after the task, when the actor sleeps) |
| E2B sandbox actual use | - | CPU 0.3% p50 (max 11.9%) of 2 vCPU; 60 MiB of 473 MiB | (same template) |
| Tokens in / out | 28.4k / 1.7k | 23.0k / 1.7k | 20.7k / 0.8k |

Reading it:

- **A puts everything on Vercel's meter.** Tool processes (npm, node --test) run inside the function and add ~630 ms CPU per task
  on top of pi's own ~540 ms. On a cold instance, loading pi alone costs ~1.5 s CPU (310 ms on a laptop: Vercel's 2.5 GHz Xeon
  share is slower, and pi is loaded as an external module without Vercel's bytecode cache).
- **B moves the tools out** (Vercel tool CPU = 0), but pi's loop, its cold load and token streaming stay on Vercel.
- **C moves pi out too.** The Vercel function is a proxy: ~200 ms CPU per task, 115 MiB, no pi load. pi's work shows up on
  the Rivet worker instead (730 ms per task), and its cold cost moves there (Rivet worker boot: 3,260 ms CPU).
- **The model dominates wall time in every variant** (87-99%). C's shorter wall times come from pi-durable's different prompt
  and tools (fewer tool calls, bash heredoc edits), not from the platform; compare per-component compute, not wall time.

## 2. Tool calls: local is fast, remote is ~150-200 ms per sandbox API call

| Tool | A: local in Vercel | B: E2B from Vercel | C: E2B from Rivet |
|---|---|---|---|
| read | **3 ms** | 415 ms (exists + read = 2 API calls) | 451 ms |
| edit | 6 ms | 521 ms (access + read + write) | - |
| write | 3 ms | 181 ms | 192 ms |
| bash (npm test, cat ...) | 294 ms | 539 ms | 619 ms |
| Tool time per task | 0.64 s (1.3% of wall) | 4.3 s (8.2%) | 4.2 s (12.9%) |

Rivet's post estimates ~10 ms extra per tool call when harness and sandbox share a datacenter. Here they don't (Vercel
iad1 / Rivet us-east-1 to E2B), and each E2B API round trip cost ~150-200 ms (B: 313 ms p50 per call including command run
time). Remote tools are ~7x slower in wall time; still small next to the model.

## 3. Concurrency (3 conversations at once)

| | A | B | C |
|---|---|---|---|
| Instances used | 3 Vercel instances (2 cold) | 3 Vercel instances | 3 Vercel proxies + **1 Rivet worker** (max-scale 1) |
| Vercel CPU per task | 2,269-2,476 ms (every instance paid the pi load) | 905-1,908 ms | 267-532 ms |
| Rivet worker | - | - | 3 sessions in one process: RSS 224 MiB (+~10 MiB per active session); ~1.1-1.2 s CPU over the shared window |
| Wall | 60-72 s | 48-175 s (one run wandered: 13 tool calls) | 49-52 s |

Vercel did not pack the concurrent requests into one instance (no in-function concurrency observed); it started new
instances, and each one that served an A/B request paid pi's ~1.2-1.5 s load. All routes share instances: one that had only
served the C proxy still had to load pi for its first A request. Rivet ran all three sessions in one worker process, which is
the whole point of actors: pi loaded once, ~0.5-10 MiB per extra session.

## 3b. Cold round (after ~17 min idle, A, B and C started at the same moment)

| Cold start | A | B | C |
|---|---|---|---|
| Vercel CPU | 2,726 ms + 680 ms tool processes | 3,062 ms | 467 ms |
| Loading pi on Vercel | 1,486 ms CPU, 1,425 ms wall, +68 MiB | 1,504 ms CPU, 1,439 ms wall, +68 MiB | none |
| First event at the client | 3.6 s | 3.8 s | 3.2 s |
| Rivet | - | - | new actor on a warm worker: first call 870 ms; E2B create 710 ms |

Each Vercel cold start was a separate new instance (3 instance ids). The Rivet worker was still up after the idle period
(28 min old, booted with 2,870 ms CPU), so this round did not catch a Rivet scale-from-zero; the one fresh worker we saw
(the first probe) answered its first call in 2.8 s, 35 s after booting with 3,260 ms CPU. Rivet actors sleep within
seconds-minutes; the worker process that hosts them stays up longer.

## 3c. Heavy and crash tests (free model; every claim below re-checked from the raw files by a verification workflow)

Raw data: `lab/out/vercel-pi/heavy/<scenario>/`. "Instance" = our per-process id (a Node process on Vercel), not a VM id.

| Test | A: pi + tools in Vercel | B: Vercel + E2B | C: Rivet + E2B |
|---|---|---|---|
| **CPU-heavy tool** (8M SHA-256; laptop 2.9 s) | 23.5 s in the function; **37,090 ms of tool CPU billed to Vercel** (incl. a first try killed by a model-set 10 s timeout) | 19.0 s in E2B, 0 ms on Vercel | 21.0 s in E2B, 0 ms on Vercel |
| **npm install + fix** | passed, but plain `npm install` failed (`ENOENT mkdir /home/sbx_user1051`: no writable home); the model retried with `HOME=/tmp` | passed | passed |
| **300-file repo grep** | 3/3 passed (mod_137.js, 23 importers) | 3/3 passed | **0/4 with no system prompt** (counted 24 files incl. mod_137, or 46-47 matching lines); **2/4 after adding pi's coding prompt** (see 3d) |
| **Out of memory** (alloc to 3 GB) | **instance killed** ("Vercel Runtime Error: instance was killed because it ran out of available memory"); stream cut, no answer; next turn on a new process with a new session: conversation lost | E2B killed the tool at 400 MB (exit -1); pi explained it; same session continued (414 MB free) | same as B (416 MB free); Rivet worker unaffected (197 MB) |
| **Blast radius** (innocent chat, then an OOM chat 8 s later) | **both streams ended 0.9 ms apart**, both logged as OOM-killed: the innocent conversation died with the other user's tool | - | - |
| **Disk full** (write 800 MB) | `ENOSPC` at 500 MB; `/tmp` 525 MB at 100%; **the next two normal A chats on that instance failed with HTTP 500 ENOSPC** (B and C requests on the same instance still worked) | 800 MB written (22 GB disk) | 800 MB written |
| **5.5-min command** (limit 300 s) | cut at 300.8 s; next turn: new process, new session; the model **inferred** "DONE checksum-2310" from the script source (its own test run printed checksum-7) | cut at 300.8 s; follow-up made 11 tool calls and was **also cut at 300.5 s**; the original command (pid 1520) and a second copy were still running orphaned in E2B | proxy cut at 300.5 s but **the actor kept running**; follow-up answered correctly in 34 s with 0 tool calls, and its stream carried the original command's full output |
| **5 turns, 2 sessions at once** | 1 of 2 lost memory: turn 2 landed on another instance (new session; code word forgotten, notes.txt missing "turn 1") | 1 of 2 lost memory the same way (and a fresh E2B sandbox) | 2 of 2 kept memory, even when the Vercel proxy instance changed between turns |
| **5 agents at once** | 5/5 passed; 4 instances (2 cold, each ~2.8-3.0 s CPU); two runs shared one instance concurrently | 5/5 passed; 3 instances | 5/5 passed; one Rivet worker for all 5 (2.1-3.7 s CPU over overlapping windows) |

What breaks "entirely in Vercel" under heavy load, in order of severity:
1. A tool's memory use kills the whole function instance, including **other users' conversations** on it.
2. A tool can fill the instance's `/tmp` and break **later** users routed to that instance (until it recycles).
3. Anything longer than the function limit (300 s Hobby, 800 s Pro) dies with the request, and the session is gone.
4. Follow-up turns can land on a different instance, which has no session: memory loss with as few as two users.
5. Heavy tool CPU is billed as Vercel Active CPU and eats the time budget (37 s of tool CPU for one task here).

B fixes 1, 2 and 5 (the sandbox absorbs memory/disk/CPU), but not 3 or 4: pi's loop and session still live in the Vercel
process, and its sandboxes are orphaned when the process or the request dies (3 orphaned B sandboxes found afterwards).
C fixes all five in these tests; its only exposure is the proxy request itself being cut at 300 s, which the durable actor
outlives.

## 3d. Two bugs in C that were mine, not the platform's (found and fixed after the heavy runs)

1. **The C proxy garbled the streamed text.** Pi Durable sends the first text of each assistant message inside
   `message_start` / `text_start` / `block` changes and only the later pieces as `text_delta`. The proxy only read
   `text_delta`, so it dropped the first piece of every message, then re-sent the whole message at `message_end`
   (text shown twice). It also closed the stream as soon as `prompt()` resolved, which can be before the final
   message's last events arrive, so the last answer could be cut off (repo c-01). Every C answer in sections 3 and 3c
   before 2026-10-15 13:40 PDT has this defect in the `answer` field. The tool calls, tool outputs, usage and timings
   are not affected. Fix (`app/api/chatc/route.js`): rebuild the message text from all change types, stream only the
   new suffix, and wait (up to 5 s) for `run_end` before closing. Verified: cart c-06..c-09 and repo c-03..c-08 have
   clean text, nothing doubled or cut.
2. **C ran with no system prompt.** In Pi Durable the system prompt is only what extensions add as `sections`, plus
   an optional `instructions` setting. `CodingTools` adds the four tools and no sections, and I set no instructions,
   so C's model saw only the tool schemas. A and B run pi-coding-agent, which always sends pi's coding prompt
   (role, tool list, rules such as "use read instead of cat", cwd). Same model, same task: repo grep was A 3/3 and
   B 3/3, but C 0/4 (Fisher exact p ≈ 0.005). Fix (`rivet-worker/index.mjs`): a `coding-prompt` extension with
   pi-coding-agent 1.2.0's preamble, tools, rules and cwd sections (the pi-docs section is left out: those files are
   not in the sandbox; `PI_PROMPT=none` turns it off). Image tag `prompt1`. After: C 2/4 on repo (vs 6/6, p ≈ 0.13,
   within noise at this sample size; the two misses counted matching lines instead of files) and 2/2 on cart.
   The crash results in 3c (OOM, disk, 5.5-min job, memory, concurrency) do not depend on the prompt.

Lesson for anyone moving pi into Pi Durable / Rivet: **you have to bring the system prompt yourself**, or the agent
runs noticeably dumber on the same model.

## 4. What each runtime actually is

| | Vercel function | Rivet Compute worker | E2B sandbox |
|---|---|---|---|
| Isolation | AWS Lambda (Firecracker), kernel 5.10 | **gVisor** (kernel 4.19.0-gvisor) | Firecracker microVM |
| OS / arch | Amazon Linux 2023, x86_64, Xeon 2.5 GHz | Debian (our image), amd64 | Debian (E2B base) |
| CPU / memory | 2 CPUs visible, 2,279 MiB | 2 CPUs visible, 1 GiB (we set 1 vCPU / 1 GiB) | 2 vCPU / 512 MiB (E2B's pricing page says 4 GiB; events and getMetrics say 512 MiB) |
| Disk | /tmp only, 525 MiB; app read-only | container fs | 22 GB |
| Shell tools | bash, sh, node, npm, curl, grep, sed; **no git, python3, rg, fd, tar** | (not used) | full Debian |
| Boot / idle | 466 ms CPU, 80-92 MiB before pi | 3,260 ms CPU, 196 MiB idle | create 156-703 ms |

## 5. Security: what a tool command can see

- **A**: every shell command pi runs inherits the function's environment. `/api/probe` found 52 variables, including
  **OPENROUTER_API_KEY, E2B_API_KEY, LAB_TOKEN and VERCEL_DEPLOYMENT_KEY**. A prompt-injected `env` or `curl` would leak them.
- **B**: commands run in E2B with only E2B's default environment; the keys stay in the Vercel function.
- **C**: same, and enforced in Rivet's adapter (`sandbox-env.ts` ignores `inheritEnv` because the worker env holds the keys).

## 6. Rough cost per task (list prices, 2026-10)

| | A | B | C |
|---|---|---|---|
| Vercel (Active CPU $0.128/h + 2 GB provisioned memory $0.0106/GB-h) | $0.00033 | $0.00040 | $0.00020 |
| E2B (2 vCPU + 0.5 GiB for the sandbox lifetime) | 0 | $0.0016 | $0.0010 (+ the ~25 s it stays up after the task) |
| Rivet Compute | 0 | 0 | not priced per second publicly (1 vCPU / 1 GiB pool, scale 0-1) |

The model tokens (free model here) would dominate all of these with a paid model.

## 7. Monitoring: what we could and couldn't get

- **Vercel**: per-invocation Active CPU / memory are dashboard-only (no API field). We measured inside the function instead:
  `process.cpuUsage()`, `/proc/self/status` (VmRSS/VmHWM) and `/proc/self/stat` (cutime/cstime = tool-process CPU), plus
  instance id, cold flag and in-flight count. `vercel logs --json` saved to `out/vercel-pi/vercel/vercel-logs.ndjson`.
- **Rivet**: a `labStats` action added to the pi actor reports the worker's process CPU, RSS, threads, host and boot cost
  (no cgroup files under gVisor). `rivet logs --json` saved (regions us-east-1 and us-west-1: actors are placed near the
  caller). Dashboard Inspector: each chat is an actor (asleep when idle); its SQLite holds 11 Rivet + 13 pi-durable tables,
  240 KiB for one session (17 transcript entries = 19.8 KB, 15 durable tasks). See `rivet-inspector.json`.
- **E2B**: lifecycle events API (create/kill/pause with execution time, vCPU, memory) and `getMetrics()` samples (5 s).

## 8. Problems hit (and fixes)

1. Next.js 15.3.4 has CVE-2025-66478; deployed 15.5.27 instead.
2. `vercel project add` left the framework unset ("No Output Directory named public"); fixed with `vercel.json` `framework: nextjs`.
3. **e2b on Vercel**: as a server-external package, Next `require()`s e2b's CJS build, which `require()`s ESM-only chalk;
   Vercel's runtime rejects that (local Node 24 allows it). Fix: let webpack bundle e2b.
4. **rivetkit 2.3.27 pulls agentOS** (`@rivet-dev/agent-os-core`): node_modules 2.6 GB, of which ~1.4 GB are agentOS
   sidecars and bundled agent CLIs. Pruned to 774 MB (E2B is used for tools, Rivet Cloud is the engine).
5. **Rivet's registry timed out committing one large layer** (`timeout awaiting response headers`, also seen in the earlier
   hello-rivet deploy). Fix: split node_modules into 8 layers of <=145 MB (`split-node-modules.cjs`).
6. Rivet logs show "SQLite transaction coordinator is closed / failed to sync scheduled actor alarm" when an actor slept.
7. Chrome's `scrollIntoView()` returns a Promise; an arrow-function `useEffect` returned it and crashed React (fixed earlier).

## 9. Caveats

- Small samples (5 + 3 per variant), one task, one free model; wall times vary 2-5x run to run with the model's choices.
- C runs pi-durable (Rivet's harness) while A/B run pi-coding-agent: same pi family, different prompts and tool behaviour.
- Rivet worker CPU windows overlap in the concurrent runs (one process), so per-session CPU there is not separable.
- Vercel Active CPU is our in-function measurement, not Vercel's billing meter.

## 10. Data and reproduction

- Raw runs: `lab/out/vercel-pi/vercel/` (sequential), `lab/out/vercel-pi/vercel-par3/` (concurrent) and
  `lab/out/vercel-pi/vercel-cold/` (cold round); each `<variant>-NN.json`
  has every event with client receive times and the server's `done` record. `summary.json` per folder from `analyze.py`.
- Also there: `api-probe-*.json` (Vercel runtime), `e2b-events.json`, `e2b-sandboxes.json`, `rivet-logs.ndjson`,
  `vercel-logs.ndjson`, `rivet-inspector.json`.
- Reproduce: `python3 bench.py --base <url> --variant a|b --runs N --label X`, `--variant c --path /api/chatc`, `--parallel 3`;
  then `python3 analyze.py X`.
