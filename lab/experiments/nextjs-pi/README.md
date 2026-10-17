# pi inside a Next.js chat app, tools in a separate sandbox

Proof of concept for "Next.js on Vercel with pi loaded in the server function, E2B for the tool calls".
Two containers stand in for the two sides:

| Container | Stands in for | Runs |
|---|---|---|
| `xray-next` | the Vercel function | Next.js 15 server with `@earendil-works/pi-coding-agent` 0.87.1 loaded as a library. Holds the model API key. Never spawns a process. |
| `xray-sandbox` | the E2B sandbox | `sandbox/sandboxd.mjs`, a small HTTP daemon (like E2B's envd) that runs commands and file operations as non-root `node`. No API keys. |

How pi is wired (`app/lib/pi.js`, `app/lib/sandbox.js`):

- `createAgentSession()` with `SessionManager.inMemory()`, `SettingsManager.inMemory()` and a minimal resource
  loader, so pi reads nothing from disk (only `/tmp` is writable on Vercel). One session per chat, kept in a
  `Map` on `globalThis`; pi's code is loaded once per server process.
- pi's tools take pluggable `operations` "to delegate to remote systems". `remoteTools()` builds `bash`, `read`,
  `write` and `edit` with operations that call the sandbox over HTTP; registering them under the built-in names
  replaces the local versions.
- `app/api/chat/route.js` streams pi's events (text, thinking, tool start/end, done) as NDJSON.
- `next.config.mjs`: `serverExternalPackages: ["@earendil-works/pi-coding-agent"]` so webpack leaves pi alone.

## Run

```bash
docker build -t xray-poc-sandbox sandbox && docker build -t xray-poc-next app
KEY_FILE=path/to/.env ./run.sh          # real model via OpenRouter (key goes to docker by name only)
PI_PROVIDER=mock PRELOAD=0 ./run.sh     # lab mock LLM (needs lab-mockllm on lab_default)
open http://127.0.0.1:3100              # chat UI with a live server-stats panel
python3 measure.py chat "Run the tests in /workspace ..."     # one turn + container CPU/memory + processes
python3 measure.py sessions 51 "Say hello in one sentence."   # memory per extra session
```

## Results (2026-10-14, Docker Desktop arm64, Node 24.21, model nvidia/nemotron-3-super-120b-a12b:free)

**It works.** The task "run the tests, find the bug in cart.js, fix it, re-run" succeeded every time (CLI driver:
12 tool calls, 83 s; browser UI: 8 tool calls, 40 s, first token 665 ms). The fixed file exists only in the sandbox;
the Next.js container has no `/workspace` and never ran more than one process (`next-server`).

| Measurement | Value |
|---|---|
| Next.js server at start, routes not preloaded (`PRELOAD=0`) | 108 MiB RSS, 244 ms CPU |
| Next.js server at start, default (preloads every route, so pi too) | 177-179 MiB RSS, 491-577 ms CPU |
| Loading pi on the first request (`PRELOAD=0`) | 310 ms CPU, 226 ms wall, +67 MiB RSS |
| First session including pi load and one mock turn | 376 ms CPU, RSS 191 MiB |
| Each additional live session (sessions 2-51, mock model) | +541 KiB RSS |
| Server CPU per simple chat turn (mock) | 3.2 ms median |
| Real 12-tool-call task (83 s): Next.js container CPU | 668 ms (about 8 ms/s), memory 105 -> 135 MiB |
| Same task: sandbox container CPU | 369 ms (bash, npm, node --test, the daemon) |
| Same task: sandbox calls from the server | 5 exec, 6 read, 1 write, 7 access; 278 ms total round trips |

Compared with the pi CLI in one container (reports/REPORT.md): no ~250 ms start and ~100 MiB per chat, no fork of
the Node process per tool call, and no tool children in the server. What remains on the server is token streaming,
request building, and memory for open sessions.

## Bugs hit along the way

- `useEffect(() => ref.current?.scrollIntoView())` crashed React in production: this Chrome's `scrollIntoView()`
  returns a Promise, which React then calls as an effect cleanup ("u is not a function"). Effects now use braces.
- The free model sometimes passes `/cart.js` instead of `/workspace/cart.js`, or retries `edit` when tabs don't
  match; pi reports the error and the model recovers. That is model behaviour, not the architecture.

## Not covered here (needed for Vercel production)

- Function time limits (300 s default, 800 s max on Pro): long runs need a durable loop (save each step, resume).
- Sessions live in memory: an instance restart loses them. Use pi's storage seam (or pi-durable + a DB).
- Real E2B: swap `SANDBOX_URL` calls for the E2B SDK (`commands.run`, `files.read/write`), one sandbox per
  session, `lifecycle: { onTimeout: 'pause' }`, `Sandbox.connect(id)` per request.
