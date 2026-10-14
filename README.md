# harness-xray

**An X-ray for coding-agent harnesses.** Put a harness (pi, Claude Code, Codex, …) in Docker, drive it
with scripted load, and measure **everything** it does:
every process and thread it spawns (exact CPU, run-queue wait, peak memory, I/O), every syscall, file,
socket, signal and TCP connection, the container's cgroup and network counters, the VM, the Node.js
internals of the agent, and every request it sends to the model. Watch it live, store it all in
SQLite, explore it later in an offline explorer. Swap the harness by adding an adapter folder.

The first harness studied is [pi](https://pi.dev) (`@earendil-works/pi-coding-agent` 0.87.1).

```
lab-agent (harness)  ◀── HTTP ──▶  lab-mockllm (scripted mock LLM: OpenAI chat / Responses / Anthropic)
   │ bash -c → tools, rg/fd, sudo → root commands
   ▼
lab-monitor (privileged): bpftrace (exec/fork/exit, exact CPU + peak RSS per process & thread,
   syscalls, file I/O, sockets, TCP, signals) + /proc + cgroup + net + VM sampler (100 ms)
   → live dashboard :8765, offline explorer :8765/explorer/, SQLite (lab/out/agentlab.sqlite)
```

## What is here

| Path | What |
|---|---|
| [`lab/`](lab/README.md) | the lab: base image, harness adapters, mock LLM, eBPF/proc monitor, in-process Node probe, scenario runner, analysis, explorer |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | how pi works inside (process model, agent loop, tools, memory, network, security), from its source, with 15 testable predictions |
| [`docs/DATA.md`](docs/DATA.md) | every trace file, database table and column, and example SQL |
| [`docs/RESEARCH.md`](docs/RESEARCH.md) | survey of existing tools (eBPF tracers, agent observability, mock LLMs, harness endpoints) |
| `reports/REPORT.md` | measured findings for pi (added after the full sweep is analyzed) |

## Quick start

```bash
cd lab
./lab.sh up pi                         # build + start agent, mock and monitor (Docker Desktop works)
open http://localhost:8765             # live: CPU by process role, threads, syscalls, files, network
python3 run.py --harness pi all        # 24 scenarios, each in a fresh container (~20 min)
python3 analyze/build_db.py && python3 analyze/build_explorer.py
open http://localhost:8765/explorer/   # every stored run: timelines, process trees, threads, syscalls…
```

Scenarios cover cold start, an idle TUI, every tool, 200–300-turn loops, parallel tool calls, 50 MB
outputs, CPU-heavy children, timeouts and orphans, sudo chains, output modes (json / print / rpc /
TUI), compaction, 1–32 concurrent agents, RPC sessions, subagents, provider errors and retries, cache
warming, environment inheritance, an strace census, probe-overhead control, and real-model runs.

## Adding a harness

Create `lab/harnesses/<name>/` with a `Dockerfile` (`FROM lab-agent-base`), the config the harness
needs to reach the mock, and a `harness.json` describing its command line per mode and which
processes are the agent. Scenarios are written as intents; the mock maps them onto whatever tools the
harness advertises. See [`lab/README.md`](lab/README.md#swap-the-harness).

## Requirements

Docker (tested on Docker Desktop for Mac, arm64, kernel with BTF), Python 3, Node (for `npm pack`).
The monitor container runs privileged (eBPF).

## License

MIT for this repository's code (see `LICENSE`). Vendored `d3` (ISC) and `marked` (MIT) keep their
own license headers. pi is MIT-licensed by its authors; its source is not included here.
