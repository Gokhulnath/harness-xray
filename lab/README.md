# Agent lab

Run any coding-agent harness inside Docker under scripted load and measure everything: every process
and thread it creates (exact CPU, run-queue wait, peak memory, I/O), every syscall, file, socket,
signal and TCP connection, the container's cgroup and network counters, the VM, the Node.js internals
of the agent, and every request it sends to the model. Watch it live; store it all; revisit later.

```
┌──────────────────────────── Docker Desktop VM (linuxkit, arm64, cgroup v2) ─────────────────────────────┐
│                                                                                                          │
│  lab-agent  (harness image, e.g. lab-agent-pi)        lab-mockllm               lab-monitor (privileged) │
│  ┌──────────────────────────────────────────┐        ┌──────────────────┐      ┌──────────────────────┐ │
│  │ agent harness (pi, claude, codex, ...)   │◀─HTTP─▶│ scripted mock LLM│      │ bpftrace: proctrace  │ │
│  │   └ bash -c … → tools, sudo → root cmds  │        │ OpenAI chat /    │      │ /proc + cgroup + net │ │
│  │ NODE_OPTIONS=--require probe.cjs (Node)  │        │ Responses /      │      │ sampler (100 ms)     │ │
│  │ user `agent`, sudo policy, /workspace    │        │ Anthropic        │      │ live dashboard :8765 │ │
│  └──────────────────────────────────────────┘        └──────────────────┘      │ explorer /explorer/  │ │
│         ▲  docker exec (run.py)                              ▲ mockllm.jsonl   └──────────▲───────────┘ │
└─────────┼────────────────────────────────────────────────────┼────────────────────────────┼────────────┘
          │                                                    │                            │
   run.py (host) ── scenarios → out/<harness>/runs/<scenario>/ ┴── raw JSONL ──▶ analyze/build_db.py ──▶ out/agentlab.sqlite
                                                                                 analyze/build_explorer.py ──▶ explorer/data/
```

## Quick start

```bash
cd lab
./lab.sh up pi                          # build base + pi adapter + mock + monitor, start them
open http://localhost:8765              # live dashboard (what is running right now)
python3 run.py --harness pi --list      # scenarios this harness supports
python3 run.py --harness pi 03-each-tool 10-sudo
python3 run.py --harness pi all         # the full matrix (~12 min)
OPENROUTER_KEY_FILES=~/.config/agent-lab/openrouter.env python3 run.py --harness pi real   # real-model scenarios via OpenRouter (free model by default)
python3 analyze/build_db.py && python3 analyze/build_explorer.py
open http://localhost:8765/explorer/    # every stored run: timelines, process trees, threads, syscalls ...
```

The explorer is static: it also works without Docker, e.g. `python3 -m http.server 8777 -d explorer`.

## Swap the harness

A harness adapter is a folder `harnesses/<name>/`:

| File | Purpose |
|---|---|
| `Dockerfile` | `FROM lab-agent-base`, install the harness, copy its config into `/home/agent` |
| `home/` | config files the harness needs to talk to the mock (e.g. pi's `models.json`, Claude Code's `~/.claude.json`) |
| `harness.json` | how to drive it (below) |

`harness.json`:

```jsonc
{
  "name": "claude-code",
  "runtime": "node",                 // node → the in-process probe is injected via NODE_OPTIONS
  "probe": "node",                   // or "none"
  "protocol": "anthropic",           // what it speaks to the mock: openai-chat | openai-responses | anthropic
  "agent_match": "(^|/)claude( |$)", // regex on the command line that identifies agent processes
  "version_cmd": ["claude", "--version"],
  "env": { "ANTHROPIC_BASE_URL": "http://mockllm:8080", "ANTHROPIC_API_KEY": "..." },
  "offline_env": { "DISABLE_TELEMETRY": "1" },       // applied unless a step asks for online
  "modes": {                                         // {flags} and {prompt} are substituted
    "json":  ["claude", "-p", "--output-format", "stream-json", "--verbose", "{flags}", "{prompt}"],
    "print": ["claude", "-p", "{flags}", "{prompt}"],
    "tui":   ["claude", "{flags}", "{prompt}"],
    "tui_idle": ["claude", "{flags}"]
  },
  "flags": { "all_tools": [], "small_context": [], "real": [] },
  "features": []                     // scenario gates: rpc, subagent-ext, small-context, config-dir, ...
}
```

Then `./lab.sh up claude-code && python3 run.py --harness claude-code all`. Scenarios a harness
cannot support are skipped with a reason. Results land in `out/claude-code/runs/`, the same database,
and the explorer's harness selector and "Harnesses side by side" page.

How one scenario drives every harness: scenarios are written as *intents* (`shell`, `read`, `write`,
`edit`, `grep`, `find`, `ls`). The mock reads the tools each request advertises (names + JSON schemas)
and maps every intent onto that harness's own tool and argument names (`bash.command`,
`Bash.command`, `shell.command[]`, `Read.file_path`, `edit.edits[]`, `Edit.old_string` …). If a harness
has no tool for an intent, the step falls back to an equivalent shell command. The mock log records
the mapping for every call.

## Scenario prompt language (mock)

`SCENARIO: <name> key=value ...` anywhere in the prompt:

| Scenario | Params | What it does |
|---|---|---|
| `chat` | `chars` | one text answer |
| `stream` | `chars`, `tps` (chunks/s) | one long streamed answer |
| `each-tool` | — | ls, write, read, edit, grep, find, shell |
| `loop` | `n`, `tool` (bash/read/ls/grep/find/write/mixed), `cmd` (`$I` = turn), `path`, `file`, `pattern`, `glob`, `latency` (ms), `timeout` | n sequential tool turns |
| `parallel` | `k`, `rounds`, `tool`, `cmd` | k tool calls in one model message |
| `script` | `cmds` (separated by `;;`), `timeout` | a list of shell commands |
| `subagent` | `k`, `child`, `agent` | calls a `subagent` tool with k tasks |
| any | `fail` (status), `fails`, `failstep`, `retryafter` | inject provider errors |

## Files

```
base/           agent base image (Debian, Node 24, tools, `agent` user, sudo policy, seeded workspace)
harnesses/      one adapter per harness (pi, claude-code, ...)
mockllm/        multi-protocol scripted mock LLM (server.mjs)
probe/          in-process probe for Node agents (probe.cjs)
monitor/        sampler.py (+ labclass.py roles), proctrace.bt (bpftrace), dashboard.html
explorer/       offline explorer (index.html, app.js, app.css, vendor d3 + marked, data/)
analyze/        lablib.py, build_db.py, build_explorer.py
run.py          scenario runner
lab.sh          up / down / build / run / status
out/            all stored traces (see ../docs/DATA.md)
```

See `../docs/DATA.md` for every file, table and column, and `../docs/ARCHITECTURE.md` for how pi
works inside.
