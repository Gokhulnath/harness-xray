# pi coding-agent (v0.87.1) — Architecture for Load Testing in Docker

Synthesized from 9 verified subsystem reviews (reader + adversarial verifier) of the recovered
TypeScript source at `pi-src/` (TypeScript recovered locally from the npm package's source maps; not included in this repository — see lab/tools/extract_pi_sources.py) (packages:
`coding-agent/`, `pi-agent-core/`, `pi-ai/`, `pi-tui/`, `chord/`, `pi-telemetry/`) and the
installed package at `the installed npm package (`@earendil-works/pi-coding-agent`) `
(`docs/*.md`, `dist/bundle/`). All `path:line` references are relative to `pi-src/` unless
marked `docs/...md` (installed package docs). Where a verifier **corrected** a reader's claim,
this document uses the corrected version and notes the correction. Claims the verifier could
not settle are flagged `[unconfirmed]` and repeated in §11.

---

## 1. Overview

pi is a single Node.js (or Bun-compiled) process, single-main-thread by default. On `pi <args>`,
one process reads CLI args, loads config/extensions, builds an `AgentSession` wrapping a
pi-agent-core `Agent`, and runs a request/tool-call loop against an LLM provider until the model
stops. There is **no persistent background worker pool**, **no sudo anywhere in pi's own
source**, and (for the code path the shipped CLI actually executes) **no separate IPC/transport
layer** — RPC mode is a second `node` process talking newline-delimited JSON over stdio, not a
socket server. The codebase contains two dormant, fully-built, *unused* alternate engines
(`pi-agent-core/src/harness/*` incl. an "AgentHarness"/Lane state machine, and an experimental
`pico3` actor kernel) which the shipped CLI never imports — confirmed by zero hits for
`AgentHarness` / `pico3` in `dist/bundle/cli.js` and zero imports of `./harness/*` from
`coding-agent/src` (`pi-agent-core/src/agent.ts`, `agent-loop.ts` import only sibling
non-harness files). Treat anything under `pi-agent-core/src/harness/` as **not on the live path**
unless a citation below says otherwise.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ OS process: `pi` (node or bun-compiled binary)                          │
│                                                                           │
│  cli.js → cli-runtime.js (setupCli + main bundle, 7 eager chunks)        │
│       └─ main.ts: args → mode select → migrations → AgentSessionServices │
│                                                                           │
│  ┌─────────────── mode dispatch (mutually exclusive per process) ─────┐ │
│  │ interactive (TUI)   print (-p text/json)   rpc (stdin/stdout JSONL)│ │
│  └──────────────────────────┬────────────────────────────────────────┘ │
│                              │                                           │
│                      AgentSession (coding-agent/src/core/agent-session.ts)│
│                 session persistence, compaction, extensions, retry       │
│                              │ wraps + monkey-patches hooks on           │
│                              ▼                                           │
│                pi-agent-core Agent + agent-loop.ts (the REAL loop)       │
│        turn_start → prepareRequest → stream LLM → tool calls → turn_end  │
│                              │                                           │
│              ┌───────────────┴────────────────┐                         │
│              ▼                                 ▼                        │
│     pi-ai provider adapters              built-in tools (coding-agent)  │
│   (openai-completions / anthropic-       bash, grep, find, read, write, │
│    messages / openai-codex-responses /    edit, ls, powershell          │
│    bedrock / google / ...) — HTTP(S)            │                       │
│    or, for Codex, a cached WebSocket            ▼                       │
│              │                        node:child_process.spawn()        │
│              ▼                         (shell, rg, fd, git, npm...)     │
│      LLM provider endpoint                      │                       │
│      (api.anthropic.com, etc.)                  ▼                       │
│                                        OS-level: new PID(s), possibly    │
│                                        a detached process group; a      │
│                                        shell-run `sudo` inside is just   │
│                                        another child of that shell.     │
└─────────────────────────────────────────────────────────────────────────┘

 Dormant, NOT on this path: pi-agent-core/src/harness/{agent-harness,session,
 runtime,pico3}/* — a parallel Lane/Drive durable-execution engine + an
 experimental actor kernel, fully implemented, zero live callers.
```

Process identity for `ps`/cgroup monitoring: every pi process sets `process.title = "pi"`
(or `"pi-rpc"` for the RPC entrypoint) and exports two env vars onto itself and **every child
process it spawns**: `AI_AGENT=pi` and `PI_CODING_AGENT=true`
(docs/environment-variables.md "Process Marker" section; `coding-agent/src/cli/setup.ts:5-7`,
`coding-agent/src/rpc-entry.ts:6-8`). This is the single cleanest way to separate "pi and its
descendants" from everything else in a container's process tree.

---

## 2. Process model: processes, threads, timers, sockets

### 2.1 What the pi process itself creates

| Kind | What | When | Evidence |
|---|---|---|---|
| worker_thread | One-off image-resize worker (Photon WASM), spawned per call, terminated in `finally` | Any image attachment read that needs resizing | `coding-agent/src/utils/image-resize.ts:21-76` (falls back to in-process `resizeImageInProcess` if the worker fails to load, `image-resize.ts:105-109`) |
| child_process | Shell for the **bash/powershell tool** — `spawn(shell, [...args, cmd])`, `detached: platform!=='win32'` (setsid, so it's its own process group leader), `stdio:['ignore'\|'pipe','pipe','pipe']` | Every bash/powershell tool call issued by the model | `coding-agent/src/core/tools/bash.ts:96-102` |
| child_process | Shell for a user-typed **`!`/`!!`** command — separate code path, no timeout param exists | Interactive mode only, on `!cmd` | `coding-agent/src/core/agent-session.ts:3458-3490`; no `timeout` field in its options (`agent-session.ts:3458-3461`) |
| child_process | `ripgrep` (`rg`) and `fd` — real external binaries, not pure-JS pattern matching | Every **grep** / **find** tool call | `coding-agent/src/core/tools/grep.ts:168`, `coding-agent/src/core/tools/find.ts:216` |
| child_process | `git symbolic-ref` (sync first read, async debounced refresh) | Interactive-mode footer, only when HEAD is a detached/`.invalid` ref, debounced 500ms after the `.git/HEAD` fs.watch fires | `coding-agent/src/core/footer-data-provider.ts:51-81,201-237`; constructed only from `interactive-mode.ts:611` (one call site) |
| child_process | `fd` for **@-path autocomplete** while typing in the editor | Every keystroke that could start/continue an attachment-path pattern, debounced 20ms (0ms once a request is already pending) | `pi-tui/src/autocomplete.ts:174`; `pi-tui/src/components/editor.ts:2300-2312,2351-2358` — **missed by the subsystem reader**, a real per-keystroke subprocess cost in interactive mode |
| child_process | `which`/`where`/`/bin/bash` existsSync probe to resolve the bash tool's shell | Lazily, first time the bash tool resolves its shell config (not at startup) | `coding-agent/src/utils/shell.ts:27-56,100-124` |
| child_process | `npm`/`pnpm`/`yarn`/`bun`, `git clone`/`ls-remote`/`rev-parse` | Only for configured `npm:`/git **packages** (extensions/skills) resolution/update, or `pi update` self-update — never on a plain invocation | `coding-agent/src/core/package-manager.ts:1511-1530,1538-1562`; `coding-agent/src/config.ts:194-254,320-331` |
| child_process | A whole second **`pi` OS process**, `--mode json -p --no-session`, up to 4 concurrent / 8 total per call | Only if the (example, opt-in) `subagent` extension is installed and used | `pi-coding-agent/examples/extensions/subagent/index.ts:33-34,249-262,344-421` |
| OS-level | **Detached process group** (`setsid`) so `killProcessTree()` can `SIGKILL` the whole group with one `-pid` | Every bash/powershell spawn on non-Windows | `coding-agent/src/core/tools/bash.ts:98`; kill at `coding-agent/src/utils/shell.ts:216-247` (the `process.kill(-pid,'SIGKILL')` call itself is at line 237, **not** 197-209 as one reader draft mis-cited) |
| other | **No sudo anywhere in pi's own TypeScript source** (repo-wide grep for `sudo\|setuid\|seteuid\|getuid\|geteuid\|gid\|setgid` across every `*.ts` file in `pi-src`: zero real hits, only false positives like `trackingId`) | n/a — negative finding, re-confirmed by the adversarial pass | n/a |

**What a tool call looks like at the OS level.** `pi` (PID 1 of the container, typically) →
`spawn()`s `/bin/bash -c "<command>"` as a new, detached process-group leader → bash execs/forks
further children for pipelines. If the model's command is `sudo <x>`, `sudo` is simply another
child of that bash, inheriting the same `stdio` configuration pi gave bash. Because pi's bash
tool spawns with `stdio[0]` not connected to a real TTY for the normal (non-legacy-WSL) transport
— see §8 — `sudo` cannot read a password from a prompt and will fail immediately rather than
hang, **unless** the sudoers entry is `NOPASSWD` or credentials are cached. One reader claimed
`stdio[0]` is unconditionally `'ignore'`; the verifier corrected this: `getBashShellConfig` can
set `commandTransport:'stdin'` for a legacy-WSL-bash case, in which case stdin is a real `'pipe'`
the code writes the command into — the ordinary macOS/Linux `/bin/bash -c` path (the one you'll
hit in a standard Docker image) does use `'ignore'` (`coding-agent/src/utils/shell.ts:20-21` vs.
`coding-agent/src/core/tools/bash.ts:95-102`).

### 2.2 Timers / intervals that exist regardless of mode

| Timer | Period | Scope | Evidence |
|---|---|---|---|
| **Cache warmer** — re-sends a real `maxTokens:1` LLM request to keep a provider's prompt cache warm | Fires once per session at ~90% of the model's cache TTL, self-reschedules; stops after 1h (streaming) / 30min (idle) safety window | Any session, any mode, once `cacheWarming` setting ≠ `off` | `coding-agent/src/core/cache-warmer.ts:154,252-255,291-298,300,328-355`; **this is a real, un-ref'd `setTimeout` loop that generates genuine outbound network traffic while the agent is idle, and it was independently flagged as missed by 4 of the 9 subsystem reviews (agent-loop, providers-network, session-memory, extensions-resources) — treat it as the single most important hidden background cost in the whole system.** |
| Bash timeout (optional) | Only if the model/caller supplies one; **no default** | Per bash tool call | `coding-agent/src/core/tools/bash.ts:22-23,40,116-121` (`MAX_TIMEOUT_MS=2147483647`) |
| `waitForChildProcess` idle grace timer | 100ms, re-armed on any late stdout/stderr data after the child has exited | Every spawned shell child | `coding-agent/src/utils/child-process.ts:41,84-97` |
| Model-catalog / version-check network timeouts | 15s (model scope), 4s/provider (catalog), 10s (version check) | Startup, `--list-models`, RPC-mode background refresh | `coding-agent/src/main.ts:162-163,740,795,869,924-927`; `coding-agent/src/core/remote-catalog-provider.ts:7` |
| Trust/settings file-lock busy-wait | **Synchronous CPU-spin**, up to 10 × 20ms = 200ms real CPU burn (not a sleep) per contention event | Any read/write of `~/.pi/agent/trust.json` or `settings.json`/`auth.json` under multi-process contention | `coding-agent/src/core/trust-manager.ts:137-167`; `coding-agent/src/core/settings-manager.ts:241-266`; `coding-agent/src/core/auth-storage.ts:69-94` — **if you run many containers sharing one `$HOME`, this shows up as real CPU%, not idle wait; give each container its own `PI_CODING_AGENT_DIR` to avoid it** |
| Interactive-only: spinner (80ms default), countdown (1000ms), easter-egg animations (30-60fps), drag-autoscroll (50ms), taskbar-progress keepalive (1000ms), bash-output-update throttle (100ms), bash elapsed-time redraw (1000ms, only while a bash call is in flight), git-HEAD watcher debounce (500ms) + reftable poll (250ms, unconditional) + WSL fallback poll (1000ms), SIGCONT keep-alive (`2**30`ms, Ctrl+Z only) | various | Interactive mode only | `pi-tui/src/components/loader.ts:77-86`; `coding-agent/src/modes/interactive/components/countdown-timer.ts:21-30`; `pi-tui/src/terminal.ts:8,525-533`; `coding-agent/src/core/footer-data-provider.ts:91-93,307-381`; `coding-agent/src/core/tools/renderers/bash.ts:18,145-154`; `coding-agent/src/modes/interactive/interactive-mode.ts:4278-4310` |

**print/json/rpc modes construct none of the interactive-only timers above** — they never build a
pi-tui Component tree at all (`coding-agent/src/modes/print-mode.ts`, `.../rpc/rpc-mode.ts`: no
`pi-tui` import). The cache warmer, bash timeout, idle-grace timer, and lock busy-wait apply to
**every** mode.

### 2.3 Sockets / network connections

See §7 for the full outbound-host table. At the transport level: a single process-wide `undici`
`EnvHttpProxyAgent` is installed once per process (`coding-agent/src/core/http-dispatcher.ts:81-113`,
called from `main.ts:588,855`, `rpc-entry.ts:11`, `cli/setup.ts:12`) and used by every `fetch()`
call except **AWS Bedrock**, which routes through the AWS SDK's own `NodeHttp2Handler`/
`NodeHttpHandler` and bypasses pi's dispatcher entirely (`pi-ai/src/api/bedrock-converse-stream.ts:25,220-229`
— a correction the verifier caught). The OpenAI-Codex/ChatGPT backend additionally opens genuine
**WebSocket** connections, cached per session/account in a process-global `Map`, with their own
idle-expiry timers (`pi-ai/src/api/openai-codex-responses.ts:884,1070,1127-1200,1368-1376`) —
this is the one provider path that is actually stateful across requests (see §6).

---

## 3. A turn, end to end

1. User/UI/RPC command calls `AgentSession` → `agent.prompt(text)`
   (`pi-agent-core/src/agent.ts:368-442`), which normalizes input and calls `runAgentLoop(...)`.
2. `runAgentLoop` (`pi-agent-core/src/agent-loop.ts:101-125`) appends the prompt, computes which
   tools changed by scanning **the entire message transcript** (`declareToolChanges` →
   `getCurrentTools`, `agent-loop.ts:332-348`; `pi-ai/src/utils/transcript.ts:58-66`), emits
   `agent_start`/`turn_start`, enters `runLoop`'s outer `while(true)` / inner
   `while(hasMoreToolCalls || pendingMessages.length>0)` (`agent-loop.ts:178,182`).
3. Before the request, `config.prepareNextTurn` → coding-agent's
   `_installAgentNextTurnRefresh` (`coding-agent/src/core/agent-session.ts:687-724`) rebuilds the
   **entire session projection from scratch** via `sessionManager.buildSessionProjection()`
   (`coding-agent/src/core/session-manager.ts:543-573`) and checks the auto-compaction threshold.
4. `config.prepareRequest` → coding-agent's `_installAgentRequestProjection`
   (`agent-session.ts:608-633`) does the **same full rebuild a second time**, unconditionally, on
   every request including retries. `buildSessionProjection` itself redundantly walks the
   parent-pointer chain **twice** internally (`session-manager.ts:543-550` calls
   `buildSessionPath` directly, then `buildContextEntries` which calls `buildSessionPath` again at
   line 481) — so one ordinary turn costs **at least 3 full-transcript/branch passes**
   (`declareToolChanges`'s scan + the two `buildSessionProjection` calls, each itself a double
   walk) before a single token is sent.
5. `streamAssistantResponse` (`agent-loop.ts:380-466`) converts `AgentMessage[]` → provider
   `Message[]` (the **only** such conversion boundary, `agent-loop.ts:394`), resolves the API key,
   and calls the provider's `streamFn`. The full conversation transcript and full tool
   JSON-schemas are **re-serialized on every single request** for the default HTTP/SSE providers
   (OpenAI-completions, Anthropic) — `pi-ai/src/api/openai-completions.ts:811,1185-1220,1474-1509`
   — this is **not** an incremental/stateful API for those providers. (Exception: the
   OpenAI-Codex/ChatGPT WebSocket transport genuinely sends incremental deltas via a connection-
   scoped `previous_response_id` once a cached connection exists —
   `pi-ai/src/api/openai-codex-responses.ts:1412-1449,1493-1510` — a correction the verifier made
   to a reader's blanket "pi never does incremental" claim.)
6. Streamed deltas are pushed into `context.messages` and emitted as `message_update` events,
   **synchronously awaited through every subscribed listener and every extension handler, in
   order, once per delta** (`pi-agent-core/src/agent.ts:561-608`;
   `coding-agent/src/core/agent-session.ts:894-974,1062-1139`) — no `try/catch` wraps this
   particular await in `Agent.processEvents`, so a throwing instrumentation listener can abort a
   run (`agent.ts:605-607`).
7. If `stopReason==='length'` (truncated), all tool calls in that message are failed immediately
   without execution (`agent-loop.ts:262-269,475-500`). Otherwise tool calls dispatch via
   `executeToolCalls` → **parallel by default, `Promise.all`, with no concurrency limiter and no
   cap on how many OS processes spawn at once** (`agent-loop.ts:505-520,583-657`); sequential
   mode only applies if `config.toolExecution==='sequential'` or a tool itself declares
   `executionMode:'sequential'` (no built-in tool does).
8. Each tool call: `tool_execution_start` → `config.beforeToolCall` (extensions' `tool_call` hook,
   can block/rewrite args, **not** wrapped in try/catch — `coding-agent/src/core/extensions/runner.ts:1134-1152`)
   → `tool.execute()` → `config.afterToolCall` (extensions' `tool_result` hook) →
   `tool_execution_end`. (One reader mis-cited the bracketing line numbers from an atypical
   truncated-message fallback path; the real bracketing for ordinary calls is
   `agent-loop.ts:538-544` (sequential) / `agent-loop.ts:593-599` (parallel) for `start`, and
   `agent-loop.ts:870-878` for `end`.)
9. `config.finishTurn` → coding-agent's `finishTurn` (`agent-session.ts:675-685`) runs extension
   `turn_end` boundaries and decides continue/end; `getSteeringMessages`/`getFollowUpMessages` are
   polled to decide whether another turn starts immediately.
10. Compaction (if the token-estimate threshold is crossed) costs **one extra full LLM completion
    call** (two for a split-turn), using a **character-length heuristic, not a real tokenizer**
    (`pi-agent-core/src/harness/compaction/compaction.ts:215-267,270`, `ESTIMATED_IMAGE_CHARS=4800`
    per image) layered on top of the real provider `usage` figure when available.

Retry on a failed/aborted turn is **not** inside `agent-loop.ts` — the low-level loop just ends
the run with `stopReason:'error'|'aborted'` (`agent-loop.ts:244-255`); `AgentSession` implements
its own abortable, exponential-backoff sleep-then-resume (`agent-session.ts:3379-3419`, default 3
retries, 2s base, 60s cap).

---

## 4. Tools in detail

### 4.1 bash / powershell

- Spawn: `spawn(shellConfig.shell, args, {cwd, detached: platform!=='win32', env, stdio:['ignore'|'pipe','pipe','pipe'], windowsHide:true})` — `coding-agent/src/core/tools/bash.ts:96-102`.
- Env: full `...process.env` (`coding-agent/src/utils/shell.ts:138-150`) plus `PATH` prepended
  with pi's own bin dir, plus (unless `exposeSessionEnvironment:false`) `PI_SESSION_ID`,
  `PI_SESSION_FILE`, `PI_PROVIDER`, `PI_MODEL`, `PI_REASONING_LEVEL`
  (`coding-agent/src/core/tools/bash.ts:170-196`). A constructor-time `BashSpawnHook` (not a
  `pi.on()` event) lets a host app rewrite `{command,cwd,env}` immediately before spawn
  (`bash.ts:162-196`) — this is the only place to intercept/rewrite a command before it hits the
  OS, and it's where the (opt-in example) `sandbox`/`permission-gate` extensions attach.
- Kill: timeout and abort both call `killProcessTree(pid)` → `process.kill(-pid,'SIGKILL')` on the
  whole process group, **no SIGTERM grace period** on this path
  (`coding-agent/src/utils/shell.ts:216-247`, kill call at line 237). The separate
  `coding-agent/src/core/exec.ts` helper (used by extensions, not the bash tool) instead does
  SIGTERM then SIGKILL after a hard-coded 5000ms (`exec.ts:52-62`) — **two different kill
  policies exist in the codebase depending on which code path a tool/extension uses.**
- Output: `OutputAccumulator` keeps a rolling in-memory tail bounded to 2× the 50KB/2000-line
  limit (`DEFAULT_MAX_BYTES=50*1024`, `DEFAULT_MAX_LINES=2000`,
  `coding-agent/src/core/tools/truncate.ts:11-13`); once exceeded, full output spills to a temp
  file under `os.tmpdir()` with a `pi-bash-*.log` prefix — **not** `pi-output-*.log`, which is
  only an unused constructor default never actually hit by any real caller (verifier correction:
  both the bash tool and the remote `bash-executor.ts` path explicitly override the prefix to
  `pi-bash`/`pi-powershell`) (`coding-agent/src/core/tools/output-accumulator.ts:19-22,61,211-221`;
  `coding-agent/src/core/tools/bash.ts:390`). **No `unlink` call for these spill files was found
  anywhere in the subsystem** — under sustained load they may accumulate in `/tmp` until
  container teardown `[unconfirmed — not traced past this subsystem]`.
- A **second** independent 100ms-throttled `setTimeout` (`scheduleOutputUpdate`,
  `BASH_UPDATE_THROTTLE_MS=100`) re-arms itself while output keeps streaming, purely for UI
  update cadence — up to ~10 timer firings/sec per long-running bash call
  (`coding-agent/src/core/tools/bash.ts:285-297`; `coding-agent/src/core/tools/renderers/bash.ts:19`).
- No concurrency limiter anywhere: N tool calls in one assistant turn means N simultaneous child
  processes (`agent-loop.ts:583-654`, confirmed no `p-limit`/semaphore in the whole tools
  directory or agent-loop.ts).
- **grep/find have no timeout mechanism at all** (unlike bash) — only a match/entry-count limit,
  so a pathological pattern or huge tree can run indefinitely until the caller's `AbortSignal`
  fires (`grep -n timeout coding-agent/src/core/tools/{grep,find}.ts` → zero hits).

### 4.2 grep / find

Thin wrappers spawning `rg`/`fd` with streamed JSON/line output
(`coding-agent/src/core/tools/grep.ts:119,168`; `find.ts:172,216`). If the binaries aren't already
present, pi **downloads them from GitHub release assets over the network** on first use
(`coding-agent/src/utils/tools-manager.ts:14-17,349-396`) — a one-time network+disk+chmod spike
that will contaminate "cold start" numbers unless controlled with `PI_OFFLINE=1`. grep additionally
keeps a per-call in-memory `fileCache: Map<path, string[]>` holding the **full** line-split
contents of every matched file (for context-line rendering), independent of the final truncated
output text (`coding-agent/src/core/tools/grep.ts:147-160`).

### 4.3 read / write / edit

- `read` loads the **entire file** into memory (`ops.readFile` → full `Buffer` →
  `.toString('utf-8')` → `.split('\n')`) even when `offset`/`limit` ask for only a few lines — no
  size pre-check (`coding-agent/src/core/tools/read.ts:114,134-136`). Reading a multi-GB file
  fully allocates ~2-3× its size in RAM regardless of what's displayed.
- `write`/`edit` serialize concurrent mutations to the **same resolved (realpath'd) file** through
  an in-memory `Map`-based queue (`coding-agent/src/core/tools/file-mutation-queue.ts:16-61`), so
  two parallel tool calls editing the same file never race, but edits to *different* files proceed
  fully in parallel.
- `edit` computes diffs via the `diff` package's Myers algorithm, run up to twice per call
  (fuzzy-match + final patch) — cost scales with file size × edit distance
  (`coding-agent/src/core/tools/edit.ts:185-211`, `edit-diff.ts:300-362,376-499`).
- Code-block syntax highlighting (`cli-highlight`) runs **uncached** on every render of a read/write
  tool result in the TUI, not just during assistant-message streaming
  (`coding-agent/src/modes/interactive/theme/theme.ts:1078-1097`;
  `coding-agent/src/core/tools/renderers/read.ts:127`, `write.ts:31,38,54,113`).
- `ls` does one `fs.stat` per directory entry **sequentially** (awaited in a for-loop), not
  parallelized (`coding-agent/src/core/tools/ls.ts:112-130`).

### 4.4 Images

Format conversion to PNG (non-png/jpeg/gif/webp) runs Photon WASM synchronously on the **main
thread** (`coding-agent/src/utils/image-convert.ts:4-23`); the actual resize step spawns a fresh
`worker_thread` per call (no pool, no WASM-instance reuse — full worker-spawn + WASM-init cost
every time) and falls back to in-process execution only if the worker fails to load
(`coding-agent/src/utils/image-resize.ts:21-76,105-109`).

---

## 5. Memory model

### 5.1 What's held in RAM, and how it grows

- **Full conversation tree, forever.** `SessionManager.fileEntries` (array) + `.byId` (Map) hold
  *every* entry ever created in the session — user/assistant/tool-result messages, every
  compaction summary, every branch summary — for the life of the process. **Nothing is ever
  evicted, including entries compaction has already summarized away from the LLM-visible
  context** (`coding-agent/src/core/session-manager.ts:994-995,1189-1194`). Compaction only
  changes what `buildContextEntries` *selects* for the next request — it never shrinks the
  resident tree or the on-disk file.
- Tool results (bash/read output) are stored **verbatim and uncompressed**, both in memory and on
  disk; the compaction subsystem's 2000-char truncation (`TOOL_RESULT_MAX_CHARS`,
  `coding-agent/src/core/compaction/utils.ts:89,95-98,144`) applies **only** to the text fed into
  the summarization LLM prompt, never to what's persisted or kept resident.
- Each `CompactionEntry` additionally embeds a **full copy of the current system prompt and tool
  declarations** (`systemMessage` field, computed via
  `getCurrentSystemMessage(this.buildSessionProjection().messages)`) every time one is written —
  a cost not accounted for by the character/token-estimate heuristic
  (`coding-agent/src/core/session-manager.ts:1268,1281`). (A `retainedTail` field that would
  *duplicate* recent messages inside the compaction entry exists only in the **unused**
  pi-agent-core harness engine — `pi-agent-core/src/harness/session/types.ts:36` — coding-agent's
  own, actually-shipped `CompactionEntry` has no such field, only a pointer
  (`firstKeptEntryId`), per the verifier's correction of that specific claim.)
- `loadEntriesFromFile()` reads and parses the **entire** historical session JSONL file into
  memory in one synchronous pass on every resume — peak startup memory/time for a resumed session
  scales with the file's full lifetime size, not the compacted context size
  (`coding-agent/src/core/session-manager.ts:627-663`).
- **The chat-transcript Component tree in interactive mode is also unbounded**: `chatContainer`
  only grows via `addChild()` for the life of a session, cleared in full only on `/clear`,
  session navigate/fork, or compaction rebuild (`coding-agent/src/modes/interactive/interactive-mode.ts:590-594`
  + ~6 `clear()` call sites). `TuiMainScreen` additionally keeps its own unbounded
  `previousLines: string[]` snapshot of the last full rendered frame, duplicating as flat ANSI
  strings what the Component tree already holds structured (`pi-tui/src/tui-main-screen.ts:126,315`)
  — a second, independent unbounded-growth structure, missed by the original TUI review.
- **RPC client** (if you drive pi via `RpcClient` rather than raw stdio) accumulates **all**
  subprocess stderr into one never-truncated string for the client's lifetime, interpolated into
  most error paths — a host-side memory-growth vector proportional to total child stderr volume
  (`coding-agent/src/modes/rpc/rpc-client.ts:103`, unbounded across `start()`/`stop()` cycles).
- Undici's connection pool has **no explicit per-origin cap** (`connections` left unset →
  unlimited concurrent Client sockets per origin) — relevant under many concurrent/parallel tool-
  call-triggered requests to the same provider host
  (`coding-agent/src/core/http-dispatcher.ts:86-99`) `[verified against vendored undici source,
  not pi-src itself — confirm empirically]`.

### 5.2 Sessions on disk

Append-only JSON-lines, one entry per line, **synchronous** `appendFileSync`/`writeFileSync`, **no
`fsync`/`fdatasync` call anywhere in either engine that actually runs**
(`coding-agent/src/core/session-manager.ts:1124-1134,1160-1187`). (A third, wholly separate
`JsonlStorage` implementation inside the dormant `pico3` kernel does default `fsync:true` with
real `fsyncSync()` calls — `pi-agent-core/src/harness/pico3/jsonl.ts:66-76,252,305` — but nothing
outside `harness/pico3/` itself references it, so it never executes; don't be misled by grepping
for `fsync` and concluding the live path uses it.) `~/.pi/agent/auth.json` is written with
explicit `mode:0o600` (parent dir `0o700`), but session transcripts under
`~/.pi/agent/sessions/*.jsonl` — which pi's own docs say can contain credentials exposed during
the conversation (`docs/security.md:91`) — are written with **no mode argument**, i.e. default
umask permissions (`coding-agent/src/core/auth-storage.ts:25,59,65` vs.
`coding-agent/src/core/session-manager.ts:1129,1166,1178,1185,1853`).

### 5.3 Compaction

Default thresholds: `enabled:true`, `reserveTokens:16384`, `keepRecentTokens:20000`
(`coding-agent/src/core/compaction/compaction.ts:148-152`). Trigger:
`contextTokens > contextWindow - reserveTokens`, checked once per turn boundary
(`_compactBeforeNextAssistantResponse`, `agent-session.ts:587-606`) plus an emergency "overflow"
path after a provider context-length error. Compaction costs one (or, for a split user-message
span, two) full LLM completion call(s) and one extra full branch-chain walk
(`appendCompaction` → `buildSessionProjection()` again, on top of the per-request double walk —
`session-manager.ts:1268`). Token estimation is hybrid: real provider `usage` from the last
assistant message, plus a char/4 heuristic for everything after it (images flat-costed at 4800
chars) — `pi-agent-core/src/harness/compaction/compaction.ts:206-267,270-310`. Per-model overrides
for `reserveTokens`/`keepRecentTokens` exist in `settings.json`; `enabled` stays global
(docs/compaction.md:439-463).

---

## 6. Compute model

### 6.1 Per-turn, O(n)-in-transcript-size costs (the main scaling risk)

| Operation | Scaling | Runs | Evidence |
|---|---|---|---|
| `declareToolChanges` → `getCurrentTools` full scan | O(total messages) | Every turn | `pi-agent-core/src/agent-loop.ts:332-348`; `pi-ai/src/utils/transcript.ts:58-66` |
| `buildSessionProjection()` ×2 (request + next-turn), each internally walking the branch twice | O(entries since last compaction) × ~4 | Every turn, every request incl. retries | `coding-agent/src/core/agent-session.ts:608-633,687-724`; `session-manager.ts:543-573` |
| Token-estimate (char/4 heuristic) | O(message count + content length since last real usage) | At least once per turn boundary | `pi-agent-core/src/harness/compaction/compaction.ts:214-310` |
| Full transcript + tool-schema re-serialization into the request body | O(total transcript + all tool JSON schemas) | Every request (OpenAI-completions / Anthropic paths) | `pi-ai/src/api/openai-completions.ts:1185-1220,1474-1509` |
| SSE/stream decoding | O(bytes received), roughly linear in output+reasoning tokens | Every streamed response | OpenAI: vendored `openai` SDK's `LineDecoder`; Anthropic: pi's own hand-rolled line parser (`pi-ai/src/api/anthropic-messages.ts:395-408,411-466`) — **different CPU profile per provider, worth profiling separately** |
| Full component-tree re-render (interactive mode only) | O(total mounted transcript size) per frame, every ~16ms-throttled tick, incl. 80ms spinner ticks | Interactive mode only | `pi-tui/src/tui.ts` `Container.render`; `pi-tui/src/tui-main-screen.ts:264` |
| Streamed assistant text: Markdown re-lex + re-highlight | O(accumulated response length) **per render frame** (cache defeated — see below) | Interactive mode, during every streamed response | `coding-agent/src/modes/interactive/components/assistant-message.ts:91-96`; `pi-tui/src/components/markdown.ts:246-294` |

Compaction resets several of these to ~O(keepRecentTokens) right after it fires, then they grow
again — expect a **sawtooth** in per-turn latency/CPU that is not visible in session-file size
(which only ever grows).

### 6.2 Other fixed/one-shot costs

- **jiti extension compilation**: on-disk module cache is **explicitly disabled**
  (`moduleCache:false`); the only cache is an in-process `Map`, cleared on every `/reload` and on
  any cwd change — so every resource reload re-pays full TypeScript transpilation for every
  configured extension (`coding-agent/src/core/extensions/loader.ts:125-147,390-393,496-499`).
  That cache is also a **process-wide module-level singleton** — two concurrent sessions in one
  Node process with different `cwd`s will continuously evict and rebuild each other's cache
  (`loader.ts:125-147`, `useExtensionCacheCwd`).
- Every skill's `SKILL.md` is fully `readFileSync`'d at every resource reload just to parse
  frontmatter, even though only name/description/path are retained
  (`coding-agent/src/core/skills.ts:277-286,355-383` vs. the `Skill` interface at `skills.ts:74-80`)
  — startup/reload disk I/O scales with total skill-file bytes, not the small amount of retained
  metadata.
- AGENTS.md/CLAUDE.md context-file discovery walks **every** ancestor directory from cwd to
  filesystem root (no git-boundary stop); `.agents/skills` discovery stops at the nearest git
  repo root — two different termination conditions for what look like similar walks
  (`coding-agent/src/core/resource-loader.ts:119-157` vs.
  `coding-agent/src/core/package-manager.ts:448-481`).
- Extension lifecycle events (~35 of them) dispatch **sequentially**, one extension's handler
  fully awaited before the next — per-tool-call latency with N hooking extensions scales with the
  *sum*, not the max, of handler latencies (`coding-agent/src/core/extensions/runner.ts:988-1017,
  1082-1152`).
- Config values in `settings.json`/`models.json` prefixed `!` are resolved by **synchronously**
  spawning a shell (`spawnSync`/`execSync`, hardcoded 10s timeout) and then cached in-memory for
  the process's lifetime with no eviction — a startup/first-use cost missed by every subsystem
  review (`coding-agent/src/core/resolve-config-value.ts:130-131,145-196,208-216`).

### 6.3 Mode contrast (most important lever for the experiment)

- **print (text/json) and rpc modes never build a TUI Component tree at all.** Their per-event
  cost is a single `JSON.stringify(toJsonEvent(event))` (json mode) or printing the final text
  once (text mode) — `coding-agent/src/modes/print-mode.ts:33-169,109-110`. No Markdown
  lexing/highlighting, no differential-render diffing, no 16ms render throttle.
  json mode also explicitly `await`s stdout backpressure; the TUI render path does a raw,
  un-checked `process.stdout.write()` with no backpressure handling at all
  (`pi-tui/src/terminal.ts:470-471` vs. `print-mode.ts:113-118`) — a plausible host-memory-growth
  risk under a slow/throttled PTY that is unique to interactive mode.
- **rpc mode is a long-lived process** that reads JSONL commands from stdin until shutdown, rather
  than one process per prompt (docs/cli.md:47; docs/cli-integration.md:59) — this changes what
  "per-process memory/CPU growth" means for the load-test harness: in RPC mode you should expect
  monotonic growth across many turns in *one* process (per §5), whereas print/interactive-per-
  invocation testing resets per process.
- RPC-mode command dispatch is **not serialized**: each stdin line's handler and each prompt's
  execution are fired with `void handler(...)` / `void session.prompt(...)`, not awaited — a
  client that pipes many commands rapidly can create unbounded concurrently in-flight promise
  chains with no intake backpressure (`coding-agent/src/modes/rpc/rpc-mode.ts:398-414,809-812`).

---

## 7. Network: every outbound host, when, and how to disable

| Host / endpoint | When | Disable with |
|---|---|---|
| Every configured **model provider**'s API (Anthropic, OpenAI, OpenRouter, Google, Bedrock, Mistral, xAI, Groq, Cerebras, NVIDIA NIM, Cloudflare AI Gateway, HuggingFace, Together, Baseten, Fireworks, DeepSeek, Moonshot, MiniMax, etc., or any `models.json` custom `baseUrl`) | Every assistant turn's completion request, plus the cache-warmer's periodic low-token refresh | No single flag — remove that provider's credential. `PI_OFFLINE` does **not** stop this. |
| `https://pi.dev/api/models/providers/{id}` (remote model-catalog overlay) | Process start (if network allowed) + interactive-mode start + at most every 4h/provider after, conditional GET with ETag | `PI_OFFLINE=1` (or `--offline`) |
| `https://pi.dev/api/latest-version` | Once at interactive-mode start, fire-and-forget | `PI_OFFLINE=1` disables entirely; `PI_SKIP_VERSION_CHECK=1` disables just the comparison (note: `--offline`/`PI_OFFLINE` sets `PI_SKIP_VERSION_CHECK=1` as a *side effect* — `main.ts:568-572` — so grepping `version-check.ts` alone for `PI_OFFLINE` would miss this indirection) |
| `https://pi.dev/api/report-install` | Once per fresh install or detected changelog/version bump, interactive mode only, 5s timeout | `PI_OFFLINE=1`, or `PI_TELEMETRY=0`, or `settings.json enableInstallTelemetry:false` |
| Radius gateway `/v1/bug-reports` (also an **AI model provider gateway**, reused here — not a telemetry collector) | Only when a user explicitly uploads a bug report; never automatic | `PI_RADIUS_GATEWAY` to redirect; simply don't trigger the upload |
| GitHub release assets (`ripgrep`, `fd` binaries) | First `grep`/`find` call if binaries aren't already cached locally | `PI_OFFLINE=1` |
| npm registry / git remotes | Resolving/updating configured `npm:`/git extension-or-skill packages, or `pi update` | `PI_OFFLINE=1` |
| OpenRouter / NVIDIA NIM / Cloudflare AI Gateway attribution headers (not a separate call — headers on requests already being made) | Every request to those hosts | Same install-telemetry flag as above |
| `x-opencode-session`/`x-opencode-client` headers to opencode.ai-routed models | Every such request | **Not gated by the telemetry flag** — unconditional, a correction the telemetry subsystem review missed (`coding-agent/src/core/provider-attribution.ts:67-77`) |
| OpenAI-Codex/ChatGPT backend WebSocket (`chatgpt.com/backend-api`) | Cached per session/account, idle-expiry timers | Avoid selecting a Codex/ChatGPT-backed model |

`PI_OFFLINE=1` is the one broad switch worth setting for a clean "zero background network" Docker
baseline: it is read independently by `model-runtime.ts:197`, `package-manager.ts:54`,
`tools-manager.ts:15`, `interactive-mode.ts:1082,1197,1293`, and `main.ts:569-572`
(docs/cli.md / `cli/args.ts:328`: `--offline` = `PI_OFFLINE=1`). It does **not** stop ordinary
model-provider inference traffic or the cache warmer's re-sends to that same provider.

One global HTTP transport note worth correcting from an earlier draft of this review: pi's
`httpIdleTimeoutMs` setting (default 300000ms) controls undici's `bodyTimeout`/`headersTimeout` —
how long a stalled in-flight request is tolerated before aborting — **not** how long idle pooled
sockets are kept open. Undici's own default `keepAliveTimeout` (not set by pi) is **4000ms**,
capped by `keepAliveMaxTimeout` (600000ms) if the server's own `Keep-Alive` header asks for
longer. So idle connections to a provider host should be expected to close after roughly 4s of
inactivity by default, not 300s — important for interpreting `ss`/`conntrack` output in the
container (`coding-agent/src/core/http-dispatcher.ts:91,95`; vendored
`node_modules/undici/lib/dispatcher/client.js:252-254`, read directly, not in pi-src).
`allowH2:false` and `proxyTunnel:true` are also both forced for every provider connection
(`http-dispatcher.ts:88-90`).

---

## 8. Security / environment

- **No built-in confirmation/permission gate exists for any tool call**, including bash —
  commands execute immediately once the model issues them, with the full OS permissions of the
  pi process (docs/security.md:3; no gating code anywhere in `core/tools/bash.ts`).
- **No sudo/setuid/getuid-aware code anywhere** — pi is completely privilege-unaware; isolation is
  entirely the operator's responsibility (Docker/VM), which pi's own docs recommend explicitly
  (docs/security.md:13-17,97; docs/containerization.md:1-9,33-47, a `Dockerfile.pi` recipe is
  provided).
- **"Project trust" only gates config loading, not execution.** It decides whether `.pi/*`
  project-local settings/extensions/skills/themes/system-prompt files and ancestor
  `.agents/skills` dirs are loaded at startup; it has **zero** effect on what a running tool call
  can touch afterward (`coding-agent/src/core/trust-manager.ts:30-38,185-207`; enforcement is
  actually in `settings-manager.ts:410-413` and `package-manager.ts:2417-2468`, not the lines one
  draft of this review first cited).
- **Full environment inheritance into bash.** `getShellEnv()` spreads the complete
  `...process.env` into every spawned shell, only prepending PATH
  (`coding-agent/src/utils/shell.ts:138-150`); any secret in the parent process's env (API keys,
  etc.) flows straight into every bash tool call and every `!`/`!!` command. The bash tool does
  explicitly delete-then-conditionally-re-add the 5 `PI_SESSION_*`/`PI_PROVIDER`/`PI_MODEL`/
  `PI_REASONING_LEVEL` vars (`bash.ts:170-196`) and an arbitrary `BashSpawnHook` can rewrite the
  env further before spawn — but nothing strips provider credentials by default.
- **No controlling TTY for the default spawn path.** `stdio[0]` is `'ignore'`, not a pty, for
  ordinary `/bin/bash -c` execution, and the shell is `detached:true` (`setsid`) on Unix
  (`bash.ts:95-102`). A `sudo` call with no `NOPASSWD` entry, or an interactive git credential
  prompt, has no TTY to read from and should fail fast rather than hang — this is the key
  behavior to verify empirically for the "sudo inside bash" experiment. (Exception: a legacy-WSL-
  bash `commandTransport:'stdin'` configuration uses a real pipe for stdin instead —
  `utils/shell.ts:20-21` — not the path you'll hit in a standard Linux container.)
- **Disk permission asymmetry**: `auth.json` is `0o600` (parent `0o700`) but session JSONL
  transcripts — which can contain credentials exposed mid-conversation per pi's own docs — get
  default umask permissions (commonly `644`), not restricted.
- No single kill switch exists for tool-call sandboxing in core — it is entirely an extension's
  responsibility (the opt-in `permission-gate.ts`/`sandbox/`/`gondolin/` example extensions
  demonstrate regex-based command blocking, OS-level sandboxing via
  `@anthropic-ai/sandbox-runtime`, and routing into a Gondolin micro-VM respectively; **none ship
  active by default**). The Gondolin example in particular still leaks the full host env into the
  guest VM (`examples/extensions/gondolin/index.ts:315-363`; docs/containerization.md:155-159
  explicitly warns about this).
- Files written outside sessions/auth: `~/.pi/agent/trust.json` (trust decisions), `models-store.json`
  (remote catalog cache), `crashes.json` (local-only, capped at 5 entries / 7 days, interactive-
  mode-only `uncaughtException` handler — RPC mode registers none,
  `coding-agent/src/core/crash-log.ts:17-18,123-143`; `interactive-mode.ts:4266-4268`).

---

## 9. Instrumentation guide — concrete hooks

| What to measure | How | Evidence |
|---|---|---|
| Startup phase timing (parseArgs, migrations, runtime creation, stdin read, theme init, model scope resolution, session creation) | `PI_TIMING=1` env var — prints `--- Startup Timings: main ---` and an `extensions` block to stderr; **complete no-op** when unset (not even a trivial allocation beyond one `new Map()`) | `coding-agent/src/core/timings.ts:6,16-19,21-23,45-46` |
| Per-extension module-import vs. factory-execution time | Same `PI_TIMING=1`, `extensions` namespace | `coding-agent/src/core/extensions/loader.ts:553,568` |
| Interactive-mode cold-start benchmark | `PI_STARTUP_BENCHMARK=1` (interactive mode only) | `coding-agent/src/main.ts:914-962` |
| Every agent/turn/message/tool lifecycle event | `Agent.subscribe(listener)` — fires for `agent_start/end`, `turn_start/end`, `message_start/update/end`, `tool_execution_start/update/end` | `pi-agent-core/src/agent.ts:263-266`; event union `pi-agent-core/src/types.ts:485-500` |
| ~35 extension-level lifecycle events (richer: `tool_call`/`tool_result` can block/rewrite, `before_provider_request/headers`, `after_provider_response`, `session_*`, `user_bash`, etc.) | Write a `pi.on(event, handler)` extension (load with `-e <path>`); this is the main way to attach custom measurement code **in-process**, on the same main thread as everything else — a slow hook here blocks the whole agent loop | `coding-agent/src/core/extensions/types.ts` (full event list); `coding-agent/src/core/extensions/runner.ts:988-1152` |
| Raw outbound request payload / response headers per LLM call | `options.onPayload` / `options.onResponse` on `StreamOptions` | `pi-ai/src/types.ts:152,156`; wired at `pi-ai/src/api/openai-completions.ts:361-364,378`, `anthropic-messages.ts:578,595` |
| Retry/backoff attempts and delays | `auto_retry_start`/`auto_retry_end` AgentSession events; `settings.retry.*` config | `coding-agent/src/core/agent-session.ts:3379-3419` |
| Whether a turn triggered compaction, its reason/cost | `session_before_compact`/`session_compact_failed` extension hooks; `CompactionEntry.usage` field in the session JSONL itself | docs/compaction.md:296-383; `coding-agent/src/core/usage-totals.ts:36-73` |
| Live session file growth / per-turn disk writes | `PI_SESSION_FILE` env var (injected into every bash child) → `wc -l`/`stat`/`tail -f` on it directly | docs/environment-variables.md:26-27 |
| Bash/shell command actually spawned (incl. any sudo) | `BashSpawnHook` (constructor option) sees `{command, cwd, env}` immediately pre-spawn; combine with a `tool_call` extension hook on `toolName==='bash'` for pre-validation visibility, the same pattern the `permission-gate.ts` example uses | `coding-agent/src/core/tools/bash.ts:162-196`; `examples/extensions/permission-gate.ts:11-33` |
| Which env vars a spawned child actually receives | Run `env` via the bash tool vs. via `!env` and diff against the host's own env | docs/environment-variables.md:20-73 |
| Live PID of every spawned shell (for external `ps`/cgroup CPU/RSS sampling per tool call) | `child.pid`, tracked via `trackDetachedChildPid`/`untrackDetachedChildPid` | `coding-agent/src/core/tools/bash.ts:107,144`; `coding-agent/src/utils/shell.ts` |
| Whether a command hit output truncation (proxy for heavy-output load) | `TruncationResult.truncated`/`truncatedBy` fields on every tool result | `coding-agent/src/core/tools/truncate.ts:15-38` |
| Full-redraw frequency / render diagnostics (interactive mode) | `fullRedraws` counter exists on the TUI interface but is **not currently read anywhere in coding-agent** — must be wired to a sink to use; `PI_TUI_DEBUG_REDRAW=1` and `PI_TUI_DEBUG=1` give file-based logging instead | `pi-tui/src/tui.ts:430,520-522`; `pi-tui/src/tui-main-screen.ts:278,321-328,569-596` |
| Distinguish pi processes/modes in `ps`/cgroups | `process.title` = `pi` or `pi-rpc`; `AI_AGENT=pi` + `PI_CODING_AGENT=true` env markers inherited by every child | `coding-agent/src/cli/setup.ts:5`; `rpc-entry.ts:6`; docs/environment-variables.md "Process Marker" |
| Isolate pi's own config/trust state per container (avoid the lock-contention busy-spin under concurrent instances) | `PI_CODING_AGENT_DIR` env var overrides `~/.pi/agent` entirely | `coding-agent/src/config.ts:508,528-534` |
| Zero-network baseline | `PI_OFFLINE=1` — see §7 for exactly what it does and does not cover | multiple call sites, §7 |
| Mock an OpenAI-compatible provider for controlled load testing | Add a custom provider block to `~/.pi/agent/models.json`: `{"providers": {"mymock": {"baseUrl": "http://localhost:PORT/v1", "apiKey": "sk-test", "api": "openai-completions", "models": [{"id": "mock-model", ...}]}}}` — loaded once at `ModelRuntime.create()` and **re-loaded on every explicit `/models` refresh or settings change** (`ModelRuntime.refresh()`, not just at startup — a correction to a reader's "loaded once" claim) | `coding-agent/src/core/model-config.ts:188-333` (schema: `baseUrl, apiKey, api, headers, compat, authHeader, models[], modelOverrides`); `coding-agent/src/core/model-runtime.ts:177` (create) vs. `:702` (refresh re-reads from disk); `coding-agent/src/core/provider-composer.ts:207-245,459-562` |
| Force-enable/disable install telemetry independent of settings | `PI_TELEMETRY=1/0` | `coding-agent/src/core/telemetry.ts:8-13` |

---

## 10. Predictions to test in the experiment

1. A bare `pi -p 'hi' --offline` should show near-zero network syscalls and a single short-lived
   process with no persistent threads besides the main one — the "zero load" Docker baseline.
2. `PI_TIMING=1` across repeated `pi -p` invocations should show `createRuntime`/
   `createAgentSessionRuntime` (resource-loader discovery + model-runtime init) dominating
   cold-start latency over `parseArgs`/migrations, especially before OS file-cache warms the
   extensions/skills/themes directories.
3. `pi --mode rpc` should show one extra outbound HTTPS connection shortly after start (the
   background 15s-bounded catalog refresh) that print mode does not show.
4. As conversation length (turn/message count) grows, per-turn non-network latency should climb
   *before* any context-window limit is hit (uncached O(n) `buildSessionProjection`/
   `getCurrentTools` rescans), with a visible drop immediately after each auto-compaction event —
   a sawtooth in latency, a monotonic ramp in session-file size and RSS.
5. A single assistant message issuing many tool calls (e.g. 10+ parallel bash/file reads) should
   spawn that many concurrent OS processes with no throttling — process count/FDs/concurrent
   HTTP connections should scale ~1:1 with tool-call count in that message.
6. Killing a long bash command (timeout or abort) should kill its entire process group
   instantaneously via `SIGKILL` — test with `sleep 100 & wait` and confirm both PIDs disappear
   together, with no SIGTERM grace window (contrast with extension-driven `exec.ts` calls, which
   *do* get a 5s SIGTERM-then-SIGKILL grace).
7. `sudo -n true` should succeed only with true NOPASSWD; `sudo true` under a password-required
   config should fail near-instantly ("no tty present"/"a password is required") rather than hang,
   because the spawned shell has no controlling terminal. Likewise a `git clone` of a private repo
   with no cached credentials should fail fast with "could not read Username".
8. Commands producing >50KB/2000 lines of output should produce a `pi-bash-*.log` temp file and a
   "Full output: <path>" notice in the result; disk usage under the container's tmpdir should grow
   roughly linearly with such calls over a long run if nothing cleans them up (worth checking for a
   leak).
9. On a fresh image with no `rg`/`fd` preinstalled and `PI_OFFLINE` unset, the first grep/find call
   should show one outbound GitHub connection + download; zero thereafter (binary cached).
10. Over a long idle session with cache warming enabled, expect a small extra background request
    roughly every `promptCacheTTL × 0.9`, output capped at 1 token, for up to 1h (streaming) /
    30min (idle).
11. Injecting 429/503 from a mock provider should show two nested retry rhythms: fast (<10s)
    transport-level retries inside slower, longer (2s→4s→8s…capped at 60s) whole-turn retries if
    the transport retries are exhausted.
12. Running many concurrent `pi` processes sharing one `$HOME` (hence one `trust.json`/
    `settings.json`) should show measurable CPU burn (not idle wait) from the busy-spin lock retry
    under contention — and that should disappear once each container gets its own
    `PI_CODING_AGENT_DIR`.
13. Streaming a long assistant response with large fenced code blocks in interactive mode should
    show TUI-thread CPU growing super-linearly with response length (full re-lex/re-highlight per
    delta); the equivalent `--mode json`/rpc run on the same prompt should scale closer to linearly.
14. A terminal resize mid-session (interactive mode) should produce one sharp CPU spike
    proportional to total transcript size (full redraw + full Markdown-cache invalidation across
    every historical message).
15. Comparing `pi -p` (text) / `--mode json` / `--mode rpc` / interactive mode on an identical
    scripted prompt/tool sequence should show interactive mode with meaningfully higher CPU
    attributable to rendering, while the other three track each other closely.

---

## 11. Open questions / unconfirmed items

- Exact literal file/line of the "first model request" HTTP call inside `core/sdk.ts`/
  `core/agent-session.ts` was not pinned down by the agent-loop review (it's inside
  `streamAssistantResponse`'s `streamFunction()` call, `agent-loop.ts:402-406`, but the
  provider-side dispatch internals live in pi-ai and were covered separately in §6/§7).
- Whether temp output-spill files (`pi-bash-*.log`) are ever deleted by pi itself — no `unlink`
  call was found in the subsystem's files; unconfirmed whether they accumulate indefinitely in
  `/tmp` under sustained load, or are cleaned by some session-lifecycle code not covered here.
- Whether `which`/`where` shell-detection results are cached across multiple bash-tool invocations
  within one session — `utils/shell.ts` shows no caching, but the call site in the bash tool
  itself was not traced for a possible memoization layer.
- Whether `pi-agent-core/src/harness/*` (AgentHarness/Lane engine, pico3 kernel) is reachable from
  the shipped CLI through any path this review missed (a feature flag, a different binary, an SDK
  consumer outside this package) — static import analysis says no (zero references in
  `coding-agent/src` or `dist/bundle/cli.js`), but this should be confirmed by tracing a *live*
  pi process (e.g. require-hooking those module paths) in the Docker harness rather than trusted
  from source alone.
- The undici "unbounded connection pool" (no `connections` cap) claim was confirmed by reading the
  vendored `undici` package directly (outside `pi-src`), not from pi's own source — verify
  empirically by opening many concurrent requests to one mocked origin inside the container and
  counting sockets.
- Whether Google Generative AI / Vertex / Bedrock API adapters have their own hand-rolled SSE
  parsers (like Anthropic's) or fully delegate to their SDKs was only partially confirmed (Google
  confirmed to use raw `fetch`, not the `openai` SDK; the parsing loop itself was not read in
  full).
- Whether a hard cap exists anywhere on simultaneous in-flight provider requests at the
  coding-agent level (independent of the undici socket pool) was not found in the files reviewed;
  likely lives in session-runtime code not covered by this synthesis.
- No live `PI_TIMING=1` run was executed against a real pi process during this research (no
  model credentials / outbound network available in the research environment) — every timing/
  scaling claim above is derived from reading the instrumentation and control-flow code, not from
  a captured trace. Running the harness itself is the first real validation step.
- Whether `appendFileSync`'s underlying `write()` calls are OS-buffered/coalesced enough that the
  "no fsync" finding meaningfully affects durability at the load levels you plan to test, versus
  being a negligible risk in practice, is a question for the experiment, not source analysis.
- Full behavior of context-overflow/length-triggered recovery (`context_edit` omissions) was read
  from docs but not independently verified line-by-line against `agent-session.ts`'s recovery code.
- Whether any extension-contributed (non-built-in) tool sets `executionMode:'sequential'`, which
  would force an entire turn's tool calls to serialize — only built-in tools were checked (none do).
