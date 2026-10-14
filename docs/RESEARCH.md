# RESEARCH: harness-agnostic load-test lab — survey & decision doc

Date of research: 2026-10-13. Scope: (1) make the "pi" load-test lab harness-agnostic across coding-agent CLIs, (2) find existing open-source tooling to reuse instead of building more ourselves.

**Sourcing method.** Five research angles were run in parallel; two (`agent-observability`, `mock-llm-servers`) and part of a third (`ebpf-container-tracers`) came back with placeholder/test data and are *not* used as findings here — only their adversarial-verification passes (which redirected to real primary-source checks) are used. All claims below were then re-checked in this pass: 19 repos were queried live against the GitHub REST API (`api.github.com/repos/...`) for stars/archived/pushed_at, several docs pages were fetched directly, and refuted/corrected claims from the per-angle verifier are applied. Every row cites a URL; anything I could not independently confirm is marked **UNVERIFIED**.

---

## 1. TL;DR recommendations

**Adopt now (small lift, clear win):**
1. **CopilotKit/aimock** as the mock-LLM layer. MIT, multi-protocol (OpenAI, Anthropic, Gemini, Bedrock, Azure, Vertex, Ollama, Cohere, OpenRouter + MCP/A2A/AG-UI), scripted multi-turn tool-call fixtures, configurable TTFT/TPS/jitter, record-mode (proxy+capture real traffic into fixtures), chaos/drift testing. Live-confirmed: 973★, **pushed today (2026-10-13)**, package `@copilotkit/aimock` (renamed from `@copilotkit/llmock`). This covers more protocols than our hand-rolled mock server and already solves "replay scripted tool-call loops." → github.com/CopilotKit/aimock
2. **Node built-in profilers** (`--cpu-prof`/`--heap-prof`, `node:inspector` Profiler/HeapProfiler domains, `--prof`+`--perf-basic-prof` for perf/eBPF-crossable flamegraphs) for the Node-based harnesses (Claude Code, pi, Cline CLI, Amp). Zero new deps, composes directly with our existing eBPF work. → nodejs.org/docs/latest-v24.x/api/cli.html
3. **node:diagnostics_channel + OpenTelemetry Node zero-code auto-instrumentation** (`--require`/`NODE_OPTIONS`) to get outbound-LLM-request spans (size, timing) for free on Node harnesses, via undici's `undici:request:create/:headers/:trailers` channels. → opentelemetry.io/docs/zero-code/js/, nodejs.org/api/diagnostics_channel.html
4. **py-spy** for Python harnesses (aider): zero-code PID-attach or launch-and-profile, SVG flamegraph. Needs `--cap-add=SYS_PTRACE`; Docker's seccomp default for `process_vm_readv` itself was since allowlisted (moby/moby#42083) but ptrace-attach still needs the capability. → pydevtools.com/handbook/reference/py-spy.md
5. **cargo-flamegraph** for Rust harnesses (Codex CLI's `codex-rs`, goose): zero-code wrapper over `perf`/`dtrace`, PID-attach supported. Needs `perf_event_paranoid<=1` and `debug=true` in the release profile. **Untested: whether Docker Desktop's VM on Apple Silicon actually exposes usable hardware perf counters — bench this before relying on it.** → github.com/flamegraph-rs/flamegraph

**Integrate / borrow design, don't adopt wholesale:**
6. **Harbor** (harbor-framework/harbor — live: 6,083★, Python, **pushed 2026-10-14**, official successor harness to Terminal-Bench 2.0) — its pluggable `--agent` adapter interface and per-agent container install recipes (Claude Code, Codex, OpenHands, Gemini CLI) are the closest existing model for "one task/driver format, swappable agent backend." Not a profiler; read its adapter source before writing ours. → github.com/harbor-framework/harbor
7. **harness-perf-benchmark** (KonghaYao — live: 24★, TypeScript, pushed 2026-09-28) — a small prior-art project doing almost exactly our thing in miniature: a Bun/Hono mock server speaking 4 LLM protocols, driving 12+ coding-agent CLIs including Claude Code, Codex, Cline, OpenCode, Kimi, and **pi itself**, sampling process-tree CPU+RSS at 100ms. No eBPF, no syscall/disk/network capture, no dashboard — but its process-tree accounting (because some harnesses are launcher+worker, e.g. OpenCode v2 does CPU work in a worker process) and per-harness sandboxed home dirs are directly reusable design ideas. → github.com/KonghaYao/harness-perf-benchmark
8. **Agent Client Protocol (ACP)** (agentclientprotocol/agent-client-protocol — live: 4,434★, Apache-2.0, **pushed 2026-10-14**, transferred from zed-industries) — JSON-RPC-2.0-over-stdio standard for driving any compliant coding agent. Confirmed native/adapter support: Cline CLI (`cline --acp`, confirmed via Cline's own blog). Not confirmed this session for Claude Code/Codex/opencode/goose/crush/aider/cursor-agent/Amp/kimi-cli — worth a design spike, not a dependency yet. → github.com/agentclientprotocol/agent-client-protocol
9. **OpenTelemetry GenAI semantic conventions** (`gen_ai.*`) as the common attribute schema for LLM-request telemetry across harnesses — adopt the names, but pin a specific spec version: the spec itself states no finished stability level (status: "Development") as of this check. → github.com/open-telemetry/semantic-conventions-genai

**Keep custom (this is the lab's actual differentiator):**
10. eBPF `sched_process_exit` CPU accounting, per-thread `/proc` sampling, run-queue latency, syscall/disk/network capture, and the SQLite store + dashboard. No project surveyed (Harbor, Terminal-Bench, harness-perf-benchmark, Pixie, Inspektor Gadget) combines harness-agnostic swapping + OS-level resource profiling + mock-LLM load generation the way this lab does — that combination is still the novel part.
11. Rust/Go zero-code profiling adapters (Codex CLI, goose, crush) — no stock env-var toggle exists for any of these; must be built on `perf`+frame-pointer unwinding ourselves (see §4).

**Drop / do not invest in:**
12. **kimi-cli** — live-confirmed **archived=true**, last pushed 2026-09-22. Do not build an adapter for it as a long-term target.
13. **clinic.js** — project's own README says it is unmaintained and may be inaccurate on current Node internals; don't trust it over `--cpu-prof` directly.
14. **Amp CLI** as a mock-LLM load-test target — only `AMP_API_KEY`/`AMP_SETTINGS_FILE`/`HTTP(S)_PROXY` are documented; an `amp.url` setting exists in the settings schema but its semantics are undocumented (UNVERIFIED) — deprioritize until clarified directly with Sourcegraph docs.
15. **aider** as an upstream-dependency bet — live-confirmed last push 2026-05-22 (~5 months stale as of today). Fine to keep as a profiling target (stable, well-documented), but don't assume active upstream maintenance.

---

## 2. Agent-specific observability projects

| Project | What it is | Maturity (live-checked 2026-10-13) | Fit | URL |
|---|---|---|---|---|
| harness-perf-benchmark | Mock-server-driven CPU/RSS load test across 12+ coding CLIs incl. pi | 24★, pushed 2026-09-28, no tagged releases (per repo README) | Integrate design (process-tree accounting, sandboxed homes) | github.com/KonghaYao/harness-perf-benchmark |
| Harbor | Pluggable-agent benchmark harness, official successor to Terminal-Bench | 6,083★, Python, pushed 2026-10-14 — actively maintained | Borrow adapter pattern + container install recipes | github.com/harbor-framework/harbor |
| Terminal-Bench (legacy repo) | Original laude-institute/terminal-bench; now redirects to **harbor-framework/terminal-bench-1** | 2,600★, pushed 2026-07-11 (~3 mo stale); its own README tells new users to use Harbor instead | Superseded — use Harbor, not this | github.com/harbor-framework/terminal-bench-1 |
| Claude Code OTel exporter | Built-in `CLAUDE_CODE_ENABLE_TELEMETRY=1` → otlp/prometheus/console metrics+logs, beta traces | Anthropic's own docs call it **Beta** | Integrate for the one harness that already speaks OTel | code.claude.com/docs/en/monitoring-usage |
| Claw Orchestrator / OpenClaw coding-agent plugin | Third-party project running Claude Code/Codex/Antigravity/Cursor/OpenCode as one runtime behind an OpenAI-compatible endpoint + MCP + ACP | Small/young, single-or-few-maintainer, unverified stars this session | Borrow ideas only, not a dependency | github.com/Enderfga/claw-orchestrator |

---

## 3. eBPF / container tooling comparison

**Caveat:** the dedicated research pass for this angle returned no usable findings (placeholder data), so this table was rebuilt directly in this pass from live GitHub metadata + targeted search. Nothing here has been tested against our actual Docker Desktop arm64 container — treat the arm64/Docker-Desktop column as the single biggest open question in this whole document (see §7) and bench-test before depending on any row.

| Tool | Live stats (2026-10-13) | Docker Desktop arm64 | Granularity | Short-lived processes | Output | Overhead | Sidecar recipe | URL |
|---|---|---|---|---|---|---|---|---|
| **bpftrace** | 10,367★, pushed 2026-10-14 | Runs on any Linux kernel with BTF/eBPF support; Docker Desktop's LinuxKit VM kernel is Linux, so it should work in principle — **not tested by us** | Script-level, arbitrary kprobes/tracepoints incl. `sched_process_exit` (this is close to what the lab already hand-rolls) | Handles exit probes natively — this is the same mechanism our lab already uses | Text/histograms; no built-in structured export | Low for targeted probes | Run as a sibling container with `--pid=host --privileged` or `CAP_SYS_ADMIN`/`CAP_BPF` | github.com/bpftrace/bpftrace |
| **Tracee** (Aqua Security) | 4,638★, pushed 2026-10-01 | UNVERIFIED — docs claim broad kernel-version support; arm64/Docker Desktop specifically not confirmed this session | Syscalls, file/network events, runtime security signatures; container-aware (cgroup/namespace tagging) built in | Designed for runtime security monitoring of live containers; short-lived-process capture not specifically verified | JSON event stream | Signature-engine overhead scales with ruleset size (unverified number) | Ships as a single container image, mountable against host `/sys/kernel/debug` | github.com/aquasecurity/tracee |
| **Inspektor Gadget** | 2,939★, pushed 2026-10-13 | Primarily packaged as a `kubectl` plugin (Kubernetes-first); also ships a standalone `ig` CLI that can run against plain containerd/Docker — **not confirmed working against Docker Desktop specifically** | Per-gadget (profile, trace, snapshot); has a dedicated CPU-profiling gadget (`profile cpu`) | Not confirmed | Structured JSON per gadget | Not confirmed | `ig run` as a privileged sidecar (uncontainerized-Docker mode unverified) | kinvolk.io/blog/2022/10/measuring-cpu-usage-of-ebpf-programs-with-inspektor-gadget/ |
| **Pixie** | 6,547★, pushed 2026-10-08 | Kubernetes-first (per Pixie's own docs); the eBPF CPU profiler is explicitly **system-wide**, filtered to a container only after collection | ~10ms sampling interval per Pixie's own docs | Not specifically addressed; system-wide sampling would catch short-lived processes but attribution overhead unclear | Query-language (PxL) output, OTel export supported | Low per-sample cost at 10ms interval (Pixie's own claim) | Needs a full Pixie Cloud/Vizier deployment — heavy for a single-container lab | blog.px.dev/cpu-profiling-2/, docs.px.dev/about-pixie/pixie-ebpf/ |
| **Falco** | 9,478★, pushed 2026-10-13 | UNVERIFIED for Docker-Desktop/arm64 specifically; broadly Linux-kernel-eBPF or kernel-module based | Syscall-level runtime security rules, not a CPU/perf profiler | Rule-driven, not geared to short process lifetimes specifically | JSON/syslog alerts | Rule-count dependent (unverified number) | Sidecar container with `--privileged`, host `/proc`/`/sys` mounts | github.com/falcosecurity/falco |
| **Parca** (Polar Signals) | 4,993★, pushed 2026-09-29 | UNVERIFIED for Docker Desktop/arm64 | Continuous whole-system or per-cgroup CPU profiling via eBPF | Continuous sampling should catch short-lived processes, but not verified in a non-k8s single-container setup | pprof-compatible profiles, built-in UI | Designed to be "always on, low overhead" (vendor claim, not independently benchmarked here) | Agent + server split; agent can run as a privileged sidecar | github.com/parca-dev/parca |
| **cilium/ebpf** (library, not a tool) | 8,005★, pushed 2026-10-12 | Go library for writing your own eBPF loaders — what you'd use to extend/replace the lab's existing hand-rolled collector rather than adopt a packaged tool | N/A (library) | N/A | N/A | N/A | N/A — this is the "keep building it ourselves" option | github.com/cilium/ebpf |

**Read on this table:** none of the packaged tools (Tracee, Inspektor Gadget, Pixie, Falco, Parca) are a drop-in replacement for the lab's existing `sched_process_exit`/per-thread/run-queue-latency collector — they're either Kubernetes-first, security-rule-oriented, or whole-system continuous profilers, not built for "measure exactly this one short-lived sandboxed process tree under a scripted load test." `bpftrace` is the closest lightweight match and is effectively what the lab's custom collector already is, hand-rolled. **Recommendation: keep the custom eBPF collector; it is not competing against anything better-suited to this specific job.**

---

## 4. Per-runtime deep profiling (Node / Bun / Rust / Go / Python)

| Runtime | Harnesses using it | Zero-code profiler | Caveats (verified) | URL |
|---|---|---|---|---|
| **Node.js 24** | Claude Code, pi, Cline CLI, Amp | `--cpu-prof`/`--heap-prof` (V8 sampling, writes `.cpuprofile`/`.heapprofile`); `node:inspector` Profiler/HeapProfiler domains for per-turn start/stop; `--prof`+`--perf-basic-prof` for native perf/eBPF-crossable flamegraphs | `node:diagnostics_channel` built-in groups are **Console, HTTP, HTTP/2, Modules, NET, UDP, Process, Web Locks, Worker Thread** (corrected from an earlier draft that wrongly included Zlib/WebCrypto/WebStreams and wrongly claimed no `net` channel exists — `net.client.socket`/`net.server.socket` do exist, Experimental). `node:fs` still has **no** built-in channel — file I/O still needs `/proc` or eBPF. OTel zero-code config env vars (`OTEL_NODE_*`) and OTLP exporter env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`) live on **two different** docs pages — don't assume one page covers both. `async_hooks`/`AsyncLocalStorage` has real but **unquantified** overhead — no authoritative 2025–2026 benchmark found; measure your own workload. | nodejs.org/docs/latest-v24.x/api/cli.html, nodejs.org/docs/latest-v24.x/api/diagnostics_channel.html, opentelemetry.io/docs/zero-code/js/configuration/ |
| **Bun** | opencode (confirmed Bun 1.3+, see §5) | `bun --cpu-prof` / `--cpu-prof-md` (same `.cpuprofile` format as Node, DevTools-compatible) | The claimed `bun:jsc` **sampling CPU profiler is not confirmed** — Bun's own benchmarking docs only show `bun:jsc.heapStats()` for memory, not a CPU sampler. No documented `--prof`/eBPF-symbol story for Bun. | bun.com/docs/project/benchmarking |
| **Rust** | Codex CLI (`codex-rs`), goose | `cargo-flamegraph` / `flamegraph-rs`, wraps `perf`(Linux)/`dtrace`(macOS); can attach to a running PID; needs `perf_event_paranoid<=1` + `debug=true` release profile | Codex's Rust rewrite is well attested by press but the primary trail is a GitHub Discussion comment from a Codex team member, not a formal OpenAI blog post — treat "official announcement" framing as overstated, the underlying fact (Rust rewrite) as solid. | github.com/flamegraph-rs/flamegraph, infoq.com/news/2025/06/codex-cli-rust-native-rewrite |
| **Go** | crush | **No stock zero-code toggle.** `runtime/pprof`/`net/http/pprof` require the target binary to already import them; third-party wrapper conventions (e.g. `envpprof`'s `GOPPROF`) only work if that specific binary vendors them. Fallback: `perf` + frame-pointer unwinding, same approach as Rust. | pkg.go.dev/runtime/pprof |
| **Python** | aider | **py-spy** — attaches by PID or launches fresh, zero code change, SVG flamegraph (`py-spy record`) | Needs `--cap-add=SYS_PTRACE` for the ptrace-attach path. The specific claim "Docker's default seccomp blocks `process_vm_readv`" is now **dated**: moby/moby#42083 allowlisted that syscall in a later Docker release — verify against the Docker version actually pinned in this lab's container rather than assuming the block still applies. | pydevtools.com/handbook/reference/py-spy.md, github.com/moby/moby/pull/42083 |

---

## 5. Harness adapter matrix (implementation spec)

One row per harness. This is what the adapter layer should be built from. "Confirmed" = fetched directly this session or a prior session's direct fetch that I re-verified; "UNVERIFIED" = secondary-source only; corrections from the per-angle verifier are folded in.

### Claude Code (`@anthropic-ai/claude-code`)
- **Runtime:** Node.js CLI (native-binary install path also available via `claude install`)
- **Install:** `npm install -g @anthropic-ai/claude-code`
- **Non-interactive:** `claude -p "prompt"`; piped stdin; `--max-turns`, `--max-budget-usd`, `--permission-mode`, `--dangerously-skip-permissions`
- **Endpoint config:** `ANTHROPIC_BASE_URL` (proxy/mock endpoint), `ANTHROPIC_AUTH_TOKEN` (custom bearer), `ANTHROPIC_API_KEY` (X-Api-Key) — settable via env or `~/.claude/settings.json` `env` block. **Confirmed.**
- **Protocol:** Anthropic Messages API
- **Tools:** Bash, Read, Write, Edit, Grep, Glob, WebFetch, WebSearch, Task, TodoWrite, NotebookEdit, + MCP tools
- **Event stream:** `--output-format text|json|stream-json`, `--input-format text|stream-json`, `--include-partial-messages`
- **Telemetry:** `CLAUDE_CODE_ENABLE_TELEMETRY=1` → OTel metrics/logs (otlp/prometheus/console), **Beta**; traces need an additional beta flag
- URLs: code.claude.com/docs/en/cli-reference, code.claude.com/docs/en/env-vars, code.claude.com/docs/en/monitoring-usage

### OpenAI Codex CLI (`@openai/codex`)
- **Runtime:** Rust (`codex-rs`), npm-wrapped native binary
- **Install:** `npm install -g @openai/codex`; also Homebrew, curl installer
- **Non-interactive:** `codex exec "prompt"`; `--sandbox workspace-write|danger-full-access`
- **Endpoint config:** `~/.codex/config.toml` → `[model_providers.<id>]` with `base_url`+`env_key`, selected via `model_provider = "<id>"`. **Field-level semantics (e.g., reusing a built-in id doesn't override it) are UNVERIFIED against a primary doc page this session** — only corroborated by third-party guides.
- **Protocol:** OpenAI Chat Completions or Responses API, via `wire_api` per provider
- **Tools:** `shell`, `apply_patch` (V4A diff format); MCP client — **UNVERIFIED** against an official tools reference
- **Event stream:** `codex exec --json` (NDJSON); exact field names **disputed between sources** — confirm against developers.openai.com directly before parsing
- **Telemetry:** No OTel/telemetry env vars found in any source consulted
- URLs: github.com/openai/codex, developers.openai.com/codex/noninteractive.md, developers.openai.com/codex/local-config

### opencode (now `anomalyco/opencode`, formerly `sst/opencode`)
- **Runtime:** **Bun (confirmed this session)** — CONTRIBUTING.md requires Bun ≥1.3, `bun install`/`bun dev`; TUI in SolidJS+opentui, desktop app in Electron
- **Install:** `curl -fsSL https://opencode.ai/install | bash`; or npm/bun/pnpm/yarn global install of `opencode-ai`; or Homebrew `anomalyco/tap/opencode`
- **Non-interactive:** `opencode run` (`--command`/positional message); `--format json`; `--agent`, `--auto`, `--attach` to a running `opencode serve`
- **Endpoint config:** `opencode.json` → `provider.<name>.options.baseURL`+`apiKey`, `type: "openai-compatible"` provider (via `@ai-sdk/openai-compatible`). **Confirmed** against opencode.ai/docs/providers/.
- **Protocol:** Provider-pluggable via the Vercel AI SDK (OpenAI-compatible, Anthropic, Gemini)
- **Tools:** Not confirmed from fetched docs this session
- **Event stream:** `--format json`; structure undocumented on the fetched page
- **Telemetry:** Not mentioned in fetched docs
- **Maintenance:** live-confirmed **213,313★**, pushed 2026-10-14 — extremely active
- URLs: opencode.ai/docs/, opencode.ai/docs/providers/, github.com/anomalyco/opencode, raw.githubusercontent.com/anomalyco/opencode/HEAD/CONTRIBUTING.md

### goose (now `aaif-goose/goose`, formerly `block/goose`)
- **Runtime:** Rust
- **Install:** `curl -fsSL https://github.com/.../download_cli.sh | bash`
- **Non-interactive:** `goose run -t/--text "prompt" --no-session`; `--max-turns`
- **Endpoint config:** `OPENAI_HOST` (scheme+host) + `OPENAI_BASE_PATH` (path) as two separate vars, plus `GOOSE_PROVIDER=openai`/`GOOSE_MODEL`. **Not confirmed against the primary goose-docs.ai page this session** — but now corroborated by four independent third-party integration docs (Prem, Lyceum, Opper, Respan) all describing the same host/path split and the same "full URL in host → 404" failure mode, which raises confidence considerably even though no primary source was reached directly. Treat as **likely correct, verify with `goose info -v` before relying on it.**
- **Protocol:** OpenAI-compatible Chat Completions; also ACP and MCP integration
- **Tools:** No fixed built-in table — connects to 70+ extensions via MCP
- **Maintenance:** live-confirmed **55,263★**, pushed 2026-10-14 — actively maintained
- URLs: github.com/aaif-goose/goose, docs.opper.ai/integrations/coding-agents/goose, docs.prem.io/guides/goose

### crush (`charmbracelet/crush`)
- **Runtime:** Go
- **Install:** `brew install charmbracelet/tap/crush`; `npm install -g @charmland/crush`; `go install github.com/charmbracelet/crush@latest`
- **Non-interactive:** `crush run` (prompt via args or stdin), built for scripting/automation; `--yolo` skips permission prompts
- **Endpoint config:** a `crushrc` script file (`./.crushrc`, `./crushrc`, or `~/.config/crush/crushrc`) with Bash-like builtins: `provider add --type openai-compat --base-url "<url>" --api-key "$KEY"`, `model add ...`. **(Corrected — not a standalone `crush provider add` top-level subcommand as an earlier draft claimed; `crush.json`-based config is explicitly deprecated on the fetched page.)** Note: `crushrc` can execute `$(...)` shell substitutions at load time — review untrusted config before running.
- **Protocol:** OpenAI-compatible (`openai`, `openai-compat`), Anthropic-compatible, native Gemini, Vertex AI
- **Tools:** examples cited — `view`, `ls`, `grep`, `edit`, `bash`, `sourcegraph` (not confirmed as the complete list)
- **Telemetry:** pseudonymous usage metrics (prompts/responses excluded); opt out via `CRUSH_DISABLE_METRICS=1` or `DO_NOT_TRACK=1`
- **Maintenance:** live-confirmed **28,630★**, pushed 2026-10-14 — actively maintained
- URLs: github.com/charmbracelet/crush, mintlify.com/charmbracelet/crush/configuration/overview

### aider
- **Runtime:** Python
- **Install:** `pip install aider-chat` (standard Python packaging)
- **Non-interactive:** flags beyond env/config (e.g. `--message`) **not independently re-verified this session**
- **Endpoint config:** `OPENAI_API_BASE` + `OPENAI_API_KEY` env vars (or `.aider.conf.yml`); model name must be prefixed `openai/<model-name>` so aider's LiteLLM backend routes it as OpenAI-compatible. **Confirmed.**
- **Protocol:** Routes through LiteLLM — OpenAI Chat Completions wire format for `openai/`-prefixed models
- **Maintenance:** live-confirmed **49,535★**, last push **2026-05-22** — **~5 months stale as of 2026-10-13**, low-velocity
- URLs: aider.chat/docs/llms/openai-compat.html, github.com/Aider-AI/aider

### gemini-cli (`@google/gemini-cli`)
- **Runtime:** Node.js
- **Status: being sunset.** Google's own developer blog confirms Gemini CLI (and the Gemini Code Assist IDE extensions) stop serving requests for unpaid-tier/Google One users on **2026-06-18**, replaced by **Antigravity CLI** (not open-source). Enterprise Code Assist Standard/Enterprise and paid Gemini-API-key users reportedly retain access (secondary coverage, not independently confirmed). **Confirmed via primary source this session.**
- **Non-interactive:** headless mode auto-triggers in a non-TTY environment, or via `-p/--prompt`
- **Endpoint config:** auth via `GEMINI_API_KEY`; the custom-base-URL env var name is **disputed and unconfirmed against any primary source** (`GOOGLE_GEMINI_BASE_URL` vs `GEMINI_API_BASE_URL`)
- **Protocol:** Gemini API wire format (not OpenAI-compatible by default)
- **Event stream:** `--output-format json` (one object with `response`/`stats`/`error`); streaming JSONL event types `init`, `message`, `tool_use`, `tool_result`, `error`, `result`. Exit codes: 0 success, 1 general/API error, 42 input error, 53 turn-limit exceeded.
- **Recommendation:** given the sunset, **deprioritize building a gemini-cli adapter**; if Antigravity CLI becomes the relevant target, its non-interactive/endpoint story needs separate research (not covered here).
- URLs: developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/, geminicli.com/docs/cli/headless

### cursor-agent CLI
- **Runtime:** not researched this session
- **Non-interactive:** `cursor-agent -p/--print`; `--force`/`--yolo` to apply edits; `--output-format text|json|stream-json`
- **Endpoint config:** **confirmed no custom base-URL override exists** — only `CURSOR_API_KEY` for unattended auth is documented. Cursor's backend is fixed/proprietary.
- **Recommendation:** cannot be pointed at a mock LLM server — **exclude from the mock-LLM load-test design**, or load-test it only against the real Cursor backend (different methodology).
- URLs: cursor.com/docs/cli/headless

### kimi-cli (`MoonshotAI/kimi-cli`)
- **Status: archived.** Live-confirmed `archived: true`, last pushed 2026-09-22, final release 1.52.0 same date.
- **Endpoint config (for reference only):** a generic `openai` provider type exists with overridable `base_url`/`OPENAI_API_KEY`/`OPENAI_BASE_URL` — this would have been the swap point. **Confirmed.**
- **Recommendation:** do not build a long-term adapter for an archived/dormant repo.
- URLs: github.com/MoonshotAI/kimi-cli, kimi.com/code/docs/en/kimi-code-cli/configuration/providers.html

### Cline CLI (`cline/cline`, `apps/cli`)
- **Runtime:** Node.js (npm package `cline`)
- **Non-interactive:** `-y/--yolo` for full autonomy, `--json` for structured stdout; also auto-triggers on piped/non-TTY
- **Protocol surface:** `cline --acp` runs Cline as an **Agent Client Protocol server** — **confirmed** via Cline's own blog, making it usable behind ACP-aware tooling (alongside Zed, JetBrains, Neovim, Emacs)
- **Endpoint config:** the Cline **VS Code extension** has a documented "OpenAI Compatible" provider that lets you point at a custom endpoint (confirmed via maintainer reply + user reports in github.com/cline/cline/issues/4633, 2026). **Whether the standalone `apps/cli` exposes the identical option is unconfirmed** — this is the thing to check before assuming the CLI (not just the extension) can be redirected to a mock server.
- URLs: cline.bot/blog/introducing-cline-cli-2-0, github.com/cline/cline/issues/4633

### Amp CLI (`Sourcegraph/ampcode`)
- **Runtime:** Node.js (npm package `@sourcegraph/amp`)
- **Non-interactive:** `amp -x "command"` (single-shot); `--stream-json --dangerously-allow-all` for fully unattended runs
- **Endpoint config:** only `AMP_API_KEY`, `AMP_SETTINGS_FILE`, `AMP_LOG_LEVEL`, `HTTP_PROXY`/`HTTPS_PROXY` are documented for auth/networking. A setting key **`amp.url`** does exist in the official settings schema (called out specifically as something a managed-workspace settings file cannot set, "could make the client unable to reach the settings server") — this **implies** a real endpoint-override mechanism, but its exact semantics are undocumented on the page found. **Corrected from an earlier draft that claimed no such setting exists at all.**
- **Recommendation:** worth a direct follow-up (`amp config`/Sourcegraph support) before concluding Amp can or cannot sit behind a mock LLM.
- URLs: ampcode.com/docs/cli/settings

### pi (`@earendil-works/pi-coding-agent`)
- **Runtime:** Node.js — the harness this lab already load-tests
- **Non-interactive:** `pi -p "prompt"`; piped stdin; `--mode json`/`--mode rpc`; `--approve`/`-a` or `--no-approve`/`-na`
- **Endpoint config:** `~/.pi/agent/models.json` — custom provider = base URL + API format (OpenAI/Anthropic/Google) + model definitions. Already in use by this lab; not re-verified this session since it's the known-good baseline.
- URLs: github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/custom-provider.md

---

## 6. Mock LLM / record-replay options and recommendation

| Option | Protocols | Scripting model | Maturity (live 2026-10-13) | License | URL |
|---|---|---|---|---|---|
| **CopilotKit/aimock** (recommended) | OpenAI (Chat/Responses/Realtime), Anthropic, Gemini, Bedrock, Azure, Vertex, Ollama, Cohere, OpenRouter; + MCP, A2A, AG-UI | Fixture files keyed on `turnIndex`/`hasToolResult`/`toolResultContains`/`sequenceIndex`/custom predicates; record-mode proxies to a real API and captures fixtures automatically; configurable TTFT/TPS/jitter; chaos/drift testing | **973★, pushed 2026-10-13 (today)** — package renamed `@copilotkit/llmock`→`@copilotkit/aimock` v1.7.0+, Docker image `ghcr.io/copilotkit/aimock` available | MIT | github.com/CopilotKit/aimock |
| harness-perf-benchmark's own mock server | OpenAI Chat Completions, Anthropic Messages, OpenAI Responses, Gemini (separate file) | Hand-written per-harness scripted sequences | 24★, pushed 2026-09-28, small/young | not confirmed this session | github.com/KonghaYao/harness-perf-benchmark |
| dwmkerr/mock-llm | OpenAI Chat Completions + `/v1/models`, MCP (incl. OAuth 2.1 emulation), basic A2A | YAML rules with a `sequence` property for ordered scripted responses; SSE streaming with configurable chunk size/interval | 14★, 8 forks — small, low-activity | MIT | github.com/dwmkerr/mock-llm |
| 9osiris/mockllm | OpenAI Chat Completions only | Scenario file: plain reply / random choice / tool call / injected error; can wait on a step for tool results | Not independently re-verified this session | not confirmed | github.com/9osiris/mockllm |

**Recommendation:** adopt **aimock** as the primary mock-LLM layer — it already covers more wire protocols than this lab's hand-rolled server (useful the moment the harness roster expands beyond OpenAI-speaking agents to Anthropic/Gemini-native ones like Claude Code and gemini-cli), ships record-mode (capture real traffic → fixtures, useful for building realistic scripted tool-call loops from actual pi/Claude Code sessions), and is the most actively maintained of the four options by a wide margin. Keep the lab's own SQLite/dashboard schema, but key captured request/response rows on `gen_ai.*`-style attribute names (§1.9) so the data model doesn't have to change again when the second or third harness protocol gets added.

---

## 7. Open questions

Resolved this session (no longer open): opencode's runtime is Bun (confirmed via CONTRIBUTING.md); gemini-cli's Antigravity-CLI sunset is confirmed via Google's own blog, dated 2026-06-18.

Still open:
1. **eBPF feasibility on Docker Desktop/arm64 specifically** — none of §3's tools' arm64/Docker-Desktop compatibility was confirmed from a primary source this session. This is the single highest-priority item to settle, since it gates both the "keep custom eBPF collector" recommendation and any packaged-tool alternative.
2. **Codex CLI's exact `codex exec --json` event schema** — field names are disputed between secondary sources (`type`/`turn.completed` vs `method`/`turn/completed`); needs a direct fetch of developers.openai.com/codex/noninteractive or the `codex-rs` `exec` crate source.
3. **goose's `OPENAI_HOST`/`OPENAI_BASE_PATH` split** — corroborated by four third-party integration docs but never confirmed on goose's own goose-docs.ai page directly; verify with `goose info -v` before building the adapter.
4. **Cline's standalone `apps/cli`** — confirmed the VS Code *extension* has an OpenAI-Compatible custom-endpoint provider; whether the CLI package exposes the identical option is unconfirmed.
5. **Amp's `amp.url` setting** — confirmed to exist in the settings schema; its actual semantics (does it redirect model calls, or something else like the settings-sync endpoint) are undocumented.
6. **kimi-cli's exact non-interactive flag** — only provider/endpoint config was confirmed; moot in practice since the repo is archived, but worth noting if revisited.
7. **aider's non-interactive CLI flags** (`--message`, `--no-stream`, etc.) — only env-var/config-file behavior was verified, not the flag reference page.
8. **ACP adoption breadth** — confirmed for Cline (`--acp`); not confirmed this session for Claude Code, Codex, opencode, goose, crush, aider, cursor-agent, Amp, kimi-cli, pi. Needs a per-harness pass against each project's own docs/integrations page before ACP can be relied on as the uniform driver layer.
9. **`cargo-flamegraph` on Docker Desktop's Apple-Silicon VM** — whether it can actually read hardware perf counters there (vs. needing a software-events-only fallback) is untested.
10. **Built-in tool *argument* names** (not just tool names) per harness — out of scope this round for all 12 harnesses; a second pass per harness's tools-reference page is needed before adapters can map tool-call arguments 1:1.
