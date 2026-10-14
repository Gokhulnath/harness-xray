#!/usr/bin/env python3
"""Harness-agnostic scenario runner for the agent lab (host side, stdlib only).

  python3 run.py --harness pi --list
  python3 run.py --harness pi all            # every scenario this harness supports
  python3 run.py --harness pi 03-each-tool 10-sudo
  OPENROUTER_KEY_FILES=path/to/.env python3 run.py --harness pi real   # OpenRouter scenarios

A harness adapter lives in harnesses/<name>/ (Dockerfile + harness.json). harness.json gives the
command templates per mode (json / print / tui / tui_idle / rpc), env vars, which processes are
"the agent" (agent_match), optional flags (all_tools, small_context, real) and feature tags.
Scenarios are written once against intents (see mockllm/server.mjs), so they drive any harness.

Each run writes out/<harness>/runs/<scenario>/:
  samples.jsonl  proc-events.jsonl  syscalls.json   (monitor: /proc, cgroups, net, VM, eBPF)
  probe-<pid>.jsonl                                 (in-process probe, Node harnesses)
  mockllm.jsonl                                     (every model request)
  events-*.jsonl  stderr-*.txt  tmux-*.txt          (harness output)
  meta.json                                         (scenario, steps, timings, disk usage, leftovers)
"""
import json, os, subprocess, sys, threading, time, urllib.request, urllib.parse, shlex, select

LAB = os.path.dirname(os.path.abspath(__file__))
MON = "http://127.0.0.1:8765"
MOCK = "http://127.0.0.1:8080"
CONTAINER = "lab-agent"
OR_MODEL = os.environ.get("OR_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
SUBAGENT_EXT = "/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/examples/extensions/subagent/index.ts"
H = {}  # loaded harness adapter
HNAME = "pi"
FRESH = True   # recreate the agent + mock containers before every scenario (--no-fresh to reuse)
COLD = False   # also drop the VM page cache before every scenario (--cold)


def load_harness(name):
    global H, HNAME
    HNAME = name
    H = json.load(open(os.path.join(LAB, "harnesses", name, "harness.json")))
    return H


def out_root():
    return os.path.join(LAB, "out", HNAME, "runs")


def cpath(rd):  # container path of a run dir
    return f"/lab/out/{HNAME}/runs/{os.path.basename(rd)}"


def post(url):
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=5).read()
    except Exception as e:
        print("  ! control call failed", url, e)


def mark(label):
    post(f"{MON}/control/mark?label={urllib.parse.quote(label)}")


def has(feature):
    return feature in H.get("features", [])


def flags(name):
    return [f.replace("{OR_MODEL}", OR_MODEL) for f in H.get("flags", {}).get(name, [])]


def agent_argv(mode, prompt=None, extra=()):
    tpl = H["modes"].get(mode)
    if not tpl:
        return None
    argv = []
    for part in tpl:
        if part == "{flags}":
            argv.extend(extra)
        elif part == "{prompt}":
            if prompt is not None:
                argv.append(prompt)
        else:
            argv.append(part)
    return argv


def agent_env(rd, env=None, offline=True, probe=True):
    e = dict(H.get("env", {}))
    if offline:
        e.update(H.get("offline_env", {}))
    if probe and H.get("probe") == "node":
        e["NODE_OPTIONS"] = "--require /lab/probe/probe.cjs"
        e["PROBE_DIR"] = cpath(rd)
    e.update(env or {})
    return e


def exec_cmd(argv, env, user="agent", interactive=False, workdir="/workspace"):
    cmd = ["docker", "exec"] + (["-i"] if interactive else []) + ["-u", user, "-w", workdir]
    for k, v in env.items():
        cmd += ["-e", k if v is None else f"{k}={v}"]  # None = pass by name (secrets never in argv)
    return cmd + [CONTAINER] + argv


def dexec(args, user="agent", env=None, stdin=None, timeout=None):
    return subprocess.run(exec_cmd(args, env or {}, user, stdin is not None), input=stdin, capture_output=True, text=True, timeout=timeout)


def reset_workspace():
    dexec(["bash", "-c", "rm -rf /workspace/* /workspace/.[!.]* 2>/dev/null; cp -R /opt/workspace-seed/. /workspace/; rm -rf /tmp/pi-* /tmp/*.log 2>/dev/null; true"], user="root")
    dexec(["chown", "-R", "agent:agent", "/workspace"], user="root")


# ---------------------------------------------------------------- step kinds
def run_json(rd, tag, prompt, extra=(), env=None, offline=True, timeout=900, probe=True, mode="json"):
    argv = agent_argv(mode, prompt, extra)
    if argv is None:
        return {"tag": tag, "skipped": f"harness has no '{mode}' mode"}
    e = agent_env(rd, env, offline, probe)
    t0 = time.time()
    with open(os.path.join(rd, f"events-{tag}.jsonl"), "w") as so, open(os.path.join(rd, f"stderr-{tag}.txt"), "w") as se:
        try:
            rc = subprocess.run(exec_cmd(argv, e), stdout=so, stderr=se, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    return {"tag": tag, "mode": mode, "rc": rc, "wall_s": round(time.time() - t0, 3), "argv": [a if a != prompt else "<prompt>" for a in argv], "prompt": prompt[:300],
            "env": {k: v for k, v in (env or {}).items() if "KEY" not in k and v is not None}, "probe": probe}


def run_parallel(rd, tag, n, prompt_fn, extra=(), env_fn=None):
    res = [None] * n

    def one(i):
        res[i] = run_json(rd, f"{tag}-{i:02d}", prompt_fn(i), extra, env_fn(i) if env_fn else None)

    ths = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return {"tag": tag, "parallel": n, "wall_s": round(time.time() - t0, 3), "runs": res}


def shell(cmd, user="agent", timeout=300):
    t0 = time.time()
    p = dexec(["bash", "-c", cmd], user=user, timeout=timeout)
    return {"shell": cmd, "rc": p.returncode, "wall_s": round(time.time() - t0, 3), "out": (p.stdout or "")[-3000:], "err": (p.stderr or "")[-1000:]}


def run_tui(rd, tag, prompt=None, extra=(), wait_done=None, idle_after=5.0, max_wait=300, after_done=None, env=None, offline=True):
    """Interactive TUI inside tmux (a real 200x50 PTY). Done = the mock answered with text (no tool call)
    in this session, or for real models the pane stops changing (the spinner animates while working)."""
    argv = agent_argv("tui" if prompt is not None else "tui_idle", prompt, extra)
    if argv is None:
        return {"tag": tag, "skipped": "harness has no tui mode"}
    e = agent_env(rd, env, offline)
    passthru = [k for k, v in e.items() if v is None]
    env_s = " ".join(f"{k}={shlex.quote(v)}" for k, v in e.items() if v is not None)
    inner = f"cd /workspace && env {env_s} {' '.join(shlex.quote(a) for a in argv)}"
    dexec(["tmux", "kill-server"])
    mlog = os.path.join(rd, "mockllm.jsonl")
    try:
        n_before = len(open(mlog).read().strip().splitlines())
    except FileNotFoundError:
        n_before = 0
    t0 = time.time()
    subprocess.run(["docker", "exec", "-u", "agent", "-w", "/workspace"] + sum((["-e", k] for k in passthru), []) + [CONTAINER, "tmux", "new-session", "-d", "-s", "agent", "-x", "200", "-y", "50", inner], capture_output=True)
    done_at, last_pane, since = None, None, time.time()
    while time.time() - t0 < max_wait:
        time.sleep(0.5)
        if wait_done == "never":
            if time.time() - t0 >= idle_after:
                break
            continue
        if wait_done == "pane":
            pane = dexec(["tmux", "capture-pane", "-p", "-t", "agent"]).stdout
            if pane != last_pane:
                last_pane, since = pane, time.time()
            elif time.time() - since >= max(15, idle_after) and time.time() - t0 > 20:
                break
            time.sleep(1.0)
            continue
        try:
            rows = [json.loads(x) for x in open(mlog).read().strip().splitlines()[n_before:]]
            last = next((r for r in reversed(rows) if not r.get("aux")), None)
        except Exception:
            last = None
        if last and "textChars" in last.get("reply", {}):
            done_at = done_at or time.time()
            if time.time() - done_at >= idle_after:
                break
    if after_done:
        after_done()
    pane = dexec(["tmux", "capture-pane", "-p", "-t", "agent"]).stdout
    open(os.path.join(rd, f"tmux-{tag}.txt"), "w").write(pane or "")
    for _ in range(2):
        dexec(["tmux", "send-keys", "-t", "agent", "C-c"])
        time.sleep(0.4)
    time.sleep(0.8)
    dexec(["tmux", "kill-server"])
    return {"tag": tag, "mode": "tui", "argv": argv, "wall_s": round(time.time() - t0, 3)}


def run_rpc(rd, tag, prompts, env=None, extra=(), idle_after=0, offline=True):
    argv = agent_argv("rpc", None, extra)
    if argv is None:
        return {"tag": tag, "skipped": "harness has no rpc mode"}
    e = agent_env(rd, env, offline)
    t0 = time.time()
    p = subprocess.Popen(exec_cmd(argv, e, interactive=True), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(os.path.join(rd, f"stderr-{tag}.txt"), "w"), text=True, bufsize=1)
    out = open(os.path.join(rd, f"events-{tag}.jsonl"), "w")
    per = []
    done_marker = H.get("rpc_done", '"type":"agent_settled"')
    for i, pr in enumerate(prompts):
        ts = time.time()
        msg = {k: (v.replace("{prompt}", pr) if isinstance(v, str) else v) for k, v in H.get("rpc_prompt", {"type": "prompt", "message": "{prompt}"}).items()}
        msg["id"] = f"p{i}"
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        while True:
            line = p.stdout.readline()
            if not line:
                break
            out.write(line)
            if done_marker in line:
                break
        per.append(round(time.time() - ts, 3))
        time.sleep(1.0)
    if idle_after:
        mark(f"rpc idle {idle_after}s")
        end = time.time() + idle_after
        while time.time() < end:
            r, _, _ = select.select([p.stdout], [], [], 0.5)
            if r:
                line = p.stdout.readline()
                if line:
                    out.write(line)
    p.stdin.close()
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
    out.close()
    return {"tag": tag, "mode": "rpc", "rpc": True, "per_prompt_s": per, "wall_s": round(time.time() - t0, 3), "rc": p.returncode}


def sleep_step(s, label="idle"):
    mark(f"{label} {s}s")
    time.sleep(s)
    return {"sleep": s, "label": label}


def make_home(path, settings=None, extra_models=None):
    """Private config dir (pi): copy of ~/.pi/agent with optional settings/model overrides."""
    py = (
        "import json,shutil;"
        f"src='/home/agent/.pi/agent';dst='{path}';"
        "shutil.rmtree(dst,ignore_errors=True);shutil.copytree(src,dst,ignore=shutil.ignore_patterns('sessions'));"
        f"s=json.load(open(dst+'/settings.json'));s.update({json.dumps(settings or {})});json.dump(s,open(dst+'/settings.json','w'));"
        f"m=json.load(open(dst+'/models.json'));m['providers']['mock']['models']+={json.dumps(extra_models or [])};json.dump(m,open(dst+'/models.json','w'))"
    )
    r = shell(f"python3 -c {shlex.quote(py)}")
    r["shell"] = f"make_home {path} {json.dumps(settings or {})}"
    return r


def resize_spikes():
    for w, h in ((120, 40), (200, 50), (90, 30), (200, 50)):
        mark(f"resize {w}x{h}")
        dexec(["tmux", "resize-window", "-t", "agent", "-x", str(w), "-y", str(h)])
        time.sleep(2.5)


def ALL():
    return flags("all_tools")


def probe_shell(rd, args):
    e = agent_env(rd)
    env_s = " ".join(f"{k}={shlex.quote(v)}" for k, v in e.items() if v is not None)
    r = shell(f"cd /workspace && env {env_s} {args}")
    r["shell"] = args
    return r


def strace_step(rd, opts, outfile):
    e = {**H.get("env", {}), **H.get("offline_env", {})}
    env_s = " ".join(f"{k}={shlex.quote(v)}" for k, v in e.items() if v is not None)
    argv = " ".join(shlex.quote(a) for a in agent_argv("json", "SCENARIO: each-tool", ALL()))
    r = shell(f"cd /workspace && env {env_s} strace {opts} -o {cpath(rd)}/{outfile} {argv} > /dev/null 2>&1; tail -45 {cpath(rd)}/{outfile} | head -60")
    r["shell"] = f"strace {opts} <agent each-tool>"
    return r


# ---------------------------------------------------------------- scenarios
def S(name, desc, fn, requires=(), real=False):
    return {"name": name, "desc": desc, "fn": fn, "requires": list(requires), "real": real}


SCENARIOS = [
    S("00-baseline", "Container idle with no agent running: floor for CPU/memory.", lambda rd: [sleep_step(8, "baseline")]),
    S("01-startup", "Cold-start costs: version, a one-turn chat offline vs online, startup timing env, a TS extension.", lambda rd: [
        *[probe_shell(rd, " ".join(H["version_cmd"])) for _ in range(5)],
        sleep_step(1, "offline-chat"),
        *[run_json(rd, f"chat-offline-{i}", "SCENARIO: chat chars=200") for i in range(5)],
        sleep_step(1, "online-chat"),
        *[run_json(rd, f"chat-online-{i}", "SCENARIO: chat chars=200", offline=False) for i in range(5)],
        *([sleep_step(1, "timing"), *[run_json(rd, f"chat-timing-{i}", "SCENARIO: chat chars=200", env={"PI_TIMING": "1"}) for i in range(3)]] if has("timing-env") else []),
        *([sleep_step(1, "with-extension"), *[run_json(rd, f"chat-ext-{i}", "SCENARIO: chat chars=200", ["-e", SUBAGENT_EXT]) for i in range(3)]] if has("subagent-ext") else []),
    ]),
    S("02-idle-tui", "Interactive TUI in a real PTY (tmux) sitting idle for 30 s: timers, polling, memory floor, startup network.", lambda rd: [
        run_tui(rd, "idle", None, wait_done="never", idle_after=30, offline=False),
    ]),
    S("03-each-tool", "One call of every tool kind (ls, write, read, edit, grep, find, shell), x3 repeats.", lambda rd: [
        run_json(rd, f"each-{i}", "SCENARIO: each-tool", ALL()) for i in range(3)
    ]),
    S("04-loop-200", "200 sequential turns, each one shell `echo`: per-turn overhead and growth with conversation length.", lambda rd: [
        run_json(rd, "loop200", "SCENARIO: loop n=200 tool=bash cmd='echo turn-$I'"),
    ]),
    S("05-loop-mixed-300", "300 turns cycling shell/read/ls/grep/write/find over a 589-file repo: realistic tool mix growth curve.", lambda rd: [
        run_json(rd, "mixed300", "SCENARIO: loop n=300 tool=mixed path=bigrepo pattern=function glob=*.ts", ALL()),
    ]),
    S("06-parallel-tools", "One model message with 16 tool calls (sleep 1 + small CPU) x 3 rounds: concurrency of tool execution.", lambda rd: [
        run_json(rd, "par16", "SCENARIO: parallel k=16 rounds=3 tool=bash cmd='sleep 1; python3 -c \"sum(range(2*10**6))\"; echo $I'"),
    ]),
    S("07-big-output", "Tool output stress: 50 MB stdout, 200k lines, 4 MB base64, a 19.5 MB file read 3x: buffering, truncation, spill files.", lambda rd: [
        run_json(rd, "bigout", "SCENARIO: script cmds='head -c 50000000 /dev/zero | tr \"\\\\0\" a | fold -w 100;;seq 1 200000;;head -c 3000000 /dev/urandom | base64;;python3 -c \"print(chr(10).join(str(i)*50 for i in range(80000)))\" > big.txt && wc -c big.txt'"),
        run_json(rd, "bigread", "SCENARIO: loop n=3 tool=read file=big.txt"),
        shell("ls -la /tmp | head -30; du -sh /tmp", user="root"),
    ]),
    S("08-cpu-child", "CPU-heavy tool children (5 s single-core python, 8 parallel sha256 of 200 MB): attribution agent vs children.", lambda rd: [
        run_json(rd, "cpu", "SCENARIO: script cmds='timeout 5 python3 -c \"while True: sum(range(10000))\";;head -c 200000000 /dev/urandom > blob && for i in 1 2 3 4 5 6 7 8; do sha256sum blob & done; wait'"),
    ]),
    S("09-timeout-orphans", "Timeouts and cleanup: sleep 30 with a 2 s timeout, a background job, a setsid escape, sudo sleep with timeout.", lambda rd: [
        run_json(rd, "timeout", "SCENARIO: script timeout=2 cmds='sleep 30;;(sleep 300 &) ; echo started-bg;;setsid sleep 301 & echo escaped;;sudo sleep 302'"),
        shell("ps -eo pid,ppid,pgid,sid,user,etimes,comm,args --forest | grep -v ' ps '", user="root"),
        shell("pkill -f 'sleep 30[0-2]' ; true", user="root"),
    ]),
    S("10-sudo", "sudo chains: NOPASSWD allowed, password-required without a TTY, -S with a piped password, apt-get update as root.", lambda rd: [
        shell("cp /var/log/sudo.log /tmp/sudo.before", user="root"),
        run_json(rd, "sudo", "SCENARIO: script cmds='sudo -n id;;sudo whoami;;sudo cat /etc/shadow;;sudo -n cat /etc/shadow;;echo labpass | sudo -S -p \"\" head -c 40 /etc/shadow;;sudo apt-get update -qq 2>&1 | tail -2;;sudo du -sh /var/cache/apt;;sudo -n sha256sum /usr/local/bin/node'"),
        shell("diff /tmp/sudo.before /var/log/sudo.log | tail -40", user="root"),
    ]),
    S("11-stream-modes", "A long streamed answer (40k chars) at full speed and at 60 chunks/s across json, print, rpc and the TUI.", lambda rd: [
        run_json(rd, "json-fast", "SCENARIO: stream chars=40000"),
        run_json(rd, "json-60tps", "SCENARIO: stream chars=12000 tps=60"),
        run_json(rd, "print-fast", "SCENARIO: stream chars=40000", mode="print"),
        run_rpc(rd, "rpc-fast", ["SCENARIO: stream chars=40000"]),
        run_tui(rd, "tui-fast", "SCENARIO: stream chars=40000", idle_after=3),
        run_tui(rd, "tui-60tps", "SCENARIO: stream chars=12000 tps=60", idle_after=3),
    ]),
    S("12-compaction", "Context growth until auto-compaction: 64k-context model, ~48 KB of tool output per turn for 30 turns.", lambda rd: [
        run_json(rd, "compact", "SCENARIO: loop n=30 tool=bash cmd='head -c 36000 /dev/urandom | base64 -w 120'", flags("small_context")),
    ], requires=["small-context"]),
    S("13-concurrency", "N parallel agent processes (1, 4, 16, 32), each a 30-turn shell loop; plus 16 with 400 ms model latency.", lambda rd: [
        run_parallel(rd, "n01", 1, lambda i: f"SCENARIO: loop n=30 tool=bash cmd='echo a{i}-$I'"), sleep_step(3, "n04"),
        run_parallel(rd, "n04", 4, lambda i: f"SCENARIO: loop n=30 tool=bash cmd='echo a{i}-$I'"), sleep_step(3, "n16"),
        run_parallel(rd, "n16", 16, lambda i: f"SCENARIO: loop n=30 tool=bash cmd='echo a{i}-$I'"), sleep_step(3, "n32"),
        run_parallel(rd, "n32", 32, lambda i: f"SCENARIO: loop n=30 tool=bash cmd='echo a{i}-$I'"), sleep_step(3, "n16-latency"),
        run_parallel(rd, "n16lat", 16, lambda i: f"SCENARIO: loop n=30 tool=bash latency=400 cmd='echo a{i}-$I'"),
    ]),
    S("14-rpc-session", "One long-lived RPC process, 6 prompts x 40 mixed turns: does memory come back between prompts?", lambda rd: [
        run_rpc(rd, "rpc", ["SCENARIO: loop n=40 tool=mixed path=bigrepo pattern=export glob=*.md" for _ in range(6)], extra=ALL()),
    ], requires=["rpc"]),
    S("15-subagents", "Subagent extension: the agent delegates 4 parallel tasks, each a child agent doing 15 shell turns.", lambda rd: [
        run_json(rd, "subagent", "SCENARIO: subagent k=4 child=\"loop n=15 tool=bash cmd='echo child-$I'\"", ["-e", SUBAGENT_EXT]),
    ], requires=["subagent-ext"]),
    S("16-fd-download", "find with no usable fd on PATH (Debian fdfind 8.x rejects --no-require-git): pi downloads fd from GitHub.", lambda rd: [
        shell("mv /usr/bin/fdfind /usr/bin/fdfind.off 2>/dev/null; rm -rf /home/agent/.pi/agent/bin; true", user="root"),
        run_json(rd, "fd-dl", "SCENARIO: loop n=3 tool=find path=bigrepo glob=*.ts", ALL(), offline=False),
        shell("ls -la /home/agent/.pi/agent/bin 2>&1; mv /usr/bin/fdfind.off /usr/bin/fdfind 2>/dev/null; true", user="root"),
    ], requires=["fd-download"]),
    S("17-strace", "Syscall census of one each-tool run under strace -f -c, plus an exec/connect/open trace.", lambda rd: [
        strace_step(rd, "-f -c", "strace-summary.txt"),
        strace_step(rd, "-f -tt -e trace=execve,clone,clone3,vfork,connect,openat,unlink,rename", "strace-trace.txt"),
    ]),
    S("18-cache-warmer", "Cache warmer: 30 s prompt-cache TTL, cacheWarming=idle, prices high enough that warming pays; RPC session idles 75 s.", lambda rd: [
        make_home("/tmp/pihome-cache", {"cacheWarming": "idle", "defaultModel": "mock-cache"},
                  [{"id": "mock-cache", "name": "Mock with prompt cache", "contextWindow": 200000, "maxTokens": 8192, "input": ["text"],
                    "promptCache": {"short": 30, "long": 300}, "cost": {"input": 300, "output": 300, "cacheRead": 3, "cacheWrite": 400}}]),
        run_rpc(rd, "rpc-idle", ["SCENARIO: loop n=3 tool=bash cmd='echo warm-$I'"], env={"PI_CODING_AGENT_DIR": "/tmp/pihome-cache"}, idle_after=75),
    ], requires=["cache-warmer", "rpc"]),
    S("19-retries", "Provider errors: 503 x3 then success, 429 (retry-after 2) x2, 500 x8: retry rhythm and cost.", lambda rd: [
        run_json(rd, "r503", "SCENARIO: loop n=3 tool=bash fail=503 fails=3 failstep=1"),
        run_json(rd, "r429", "SCENARIO: loop n=3 tool=bash fail=429 fails=2 failstep=1 retryafter=2"),
        run_json(rd, "r500", "SCENARIO: loop n=3 tool=bash fail=500 fails=8 failstep=1", timeout=400),
    ]),
    S("20-lock-contention", "32 parallel agents sharing one config dir vs 32 each with a private one (settings/trust lock spin).", lambda rd: [
        *[make_home(f"/tmp/pihome-{i:02d}") for i in range(32)],
        sleep_step(2, "shared-home"),
        run_parallel(rd, "shared", 32, lambda i: f"SCENARIO: loop n=10 tool=bash cmd='echo s{i}-$I'"), sleep_step(3, "private-homes"),
        run_parallel(rd, "private", 32, lambda i: f"SCENARIO: loop n=10 tool=bash cmd='echo p{i}-$I'", env_fn=lambda i: {"PI_CODING_AGENT_DIR": f"/tmp/pihome-{i:02d}"}),
    ], requires=["config-dir"]),
    S("21-tui-scaling", "TUI streaming 10k/20k/40k/80k chars at full speed, then terminal resizes on the 80k transcript; json-80k control.", lambda rd: [
        run_tui(rd, "tui-10k", "SCENARIO: stream chars=10000", idle_after=2),
        run_tui(rd, "tui-20k", "SCENARIO: stream chars=20000", idle_after=2),
        run_tui(rd, "tui-40k", "SCENARIO: stream chars=40000", idle_after=2),
        run_tui(rd, "tui-80k", "SCENARIO: stream chars=80000", idle_after=3, after_done=resize_spikes),
        run_json(rd, "json-80k", "SCENARIO: stream chars=80000"),
    ]),
    S("22-env-leak", "Environment inheritance: a fake secret in the agent's env; tool children print what they received, their fds and TTY.", lambda rd: [
        run_json(rd, "env", "SCENARIO: script cmds='env | sort | grep -E -e LAB_CANARY -e ^PI_ -e ^AI_AGENT -e ^CLAUDE -e ^ANTHROPIC -e NODE_OPTIONS | cut -c1-90;;grep -E ^Uid -e ^Gid -e ^Groups -e ^CapEff -e ^NoNewPrivs -e ^Seccomp /proc/self/status;;ls -l /proc/self/fd;;tty || echo no-tty;;ps -o pid,pgid,sid,tty,comm -p $$'", env={"LAB_CANARY_API_KEY": "canary-value-123"}),
    ]),
    S("23-probe-overhead", "Measurement control: 10 runs of a 20-turn loop with the in-process probe, 10 without (eBPF measures both).", lambda rd: [
        *[run_json(rd, f"probe-{i}", "SCENARIO: loop n=20 tool=bash cmd='echo p$I'") for i in range(10)],
        sleep_step(1, "no-probe"),
        *[run_json(rd, f"noprobe-{i}", "SCENARIO: loop n=20 tool=bash cmd='echo n$I'", probe=False) for i in range(10)],
    ]),
]

REAL = [
    S("R1-fix-tests-json", "Real model fixes 3 failing unit tests in pyproj (JSON mode).", lambda rd: [
        run_json(rd, "fix", "In the pyproj folder: run the unit tests, read inventory/core.py, fix the bugs so all tests pass (remove() must raise ValueError if stock would go negative), then run the tests again and report.", flags("real"), env=H.get("real_env"), offline=False, timeout=1500),
        shell("cd /workspace/pyproj && python3 -m unittest discover -s tests 2>&1 | tail -3"),
    ], real=True),
    S("R2-sudo-task-json", "Real model task that needs sudo (apt install jq) and a password-gated read.", lambda rd: [
        shell("cp /var/log/sudo.log /tmp/sudo.before", user="root"),
        run_json(rd, "sudo", "You are user `agent` with passwordless sudo for apt-get only. Install the `jq` package with apt-get (non-interactive), then create a small JSON file and pretty-print it with jq. Finally try to show the first line of /etc/shadow and tell me what happened.", flags("real"), env=H.get("real_env"), offline=False, timeout=1500),
        shell("diff /tmp/sudo.before /var/log/sudo.log | tail -40", user="root"),
    ], real=True),
    S("R3-explore-tui", "Real model explores the 589-file repo in the interactive TUI (PTY).", lambda rd: [
        run_tui(rd, "explore", "Explore /workspace/bigrepo briefly with grep/ls/read and explain in 10 bullet points how the bash tool spawns processes. Keep tool calls under 12.", flags("real"), wait_done="pane", idle_after=15, max_wait=900, env=H.get("real_env"), offline=False),
    ], real=True),
]


# env files to look for OPENROUTER_API_KEY / OPEN_ROUTER_API_KEY in (colon-separated); the first key OpenRouter accepts is used
KEY_FILES = [p for p in os.environ.get("OPENROUTER_KEY_FILES", "~/.config/agent-lab/openrouter.env:.env.local").split(":") if p]


def load_openrouter_key():
    """First OpenRouter key (from KEY_FILES) that OpenRouter accepts; put in os.environ, never printed."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    for path in KEY_FILES:
        try:
            lines = open(os.path.expanduser(path)).read().splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            k, _, v = line.strip().partition("=")
            if k.replace("export ", "").strip() not in ("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY"):
                continue
            v = v.strip().strip('"').strip("'")
            try:
                urllib.request.urlopen(urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {v}"}), timeout=15).read()
            except Exception:
                print(f"  OpenRouter key in {path} rejected; trying the next file")
                continue
            os.environ["OPENROUTER_API_KEY"] = v
            print(f"  using the OpenRouter key from {path}")
            return True
    return False


def fresh_containers():
    """Recreate lab-agent and lab-mockllm from their images, then wait until the monitor's eBPF tracer
    is attached to the new container's cgroup (first per-second tick seen) and the mock answers."""
    t0 = time.time()
    r = subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "--no-deps", "agent", "mockllm"], cwd=LAB, env={**os.environ, "HARNESS": HNAME}, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("container recreate failed: " + r.stderr[-500:])
    cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", CONTAINER], capture_output=True, text=True).stdout.strip()
    image = subprocess.run(["docker", "inspect", "-f", "{{.Image}}", CONTAINER], capture_output=True, text=True).stdout.strip()
    deadline = time.time() + 60
    ok_bpf = ok_mock = False
    while time.time() < deadline and not (ok_bpf and ok_mock):
        try:
            h = json.loads(urllib.request.urlopen(f"{MON}/health", timeout=3).read())
            ok_bpf = h.get("bpf_cid") == cid and h.get("bpf_ready")
        except Exception:
            pass
        try:
            ok_mock = urllib.request.urlopen(f"{MOCK}/health", timeout=3).read() == b"ok"
        except Exception:
            pass
        time.sleep(0.3)
    if not ok_bpf:
        print("  ! eBPF tracer did not confirm attachment to the new container")
    if COLD:
        post(f"{MON}/control/drop_caches")
    return {"container_id": cid, "image": image, "recreate_s": round(time.time() - t0, 2), "bpf_attached": bool(ok_bpf), "cold": COLD}


def postflight():
    du = shell("du -sb /workspace /tmp /home/agent 2>/dev/null; echo; find /home/agent /tmp -type f -newer /tmp/.run-start -printf '%s %p\\n' 2>/dev/null | sort -rn | head -25", user="root")
    left = shell("ps -eo pid,ppid,user,etimes,comm,args | grep -vE ' (ps|grep|bash -c ps)' | tail -n +2", user="root")
    return du["out"], left["out"]


def run(sc):
    name = sc["name"]
    missing = [r for r in sc["requires"] if not has(r)]
    if missing:
        print(f"== {name}: skipped (harness lacks {missing})")
        return None
    rd = os.path.join(out_root(), name)
    os.makedirs(rd, exist_ok=True)
    for f in os.listdir(rd):
        os.remove(os.path.join(rd, f))
    print(f"== [{HNAME}] {name}: {sc['desc']}")
    container = fresh_containers() if FRESH else {"fresh": False}
    container["fresh"] = FRESH
    reset_workspace()
    shell("touch /tmp/.run-start", user="root")
    post(f"{MON}/control/run?dir={urllib.parse.quote(cpath(rd))}&agent={urllib.parse.quote(H.get('agent_match', ''))}&harness={HNAME}")
    post(f"{MOCK}/control/log?path={urllib.parse.quote(cpath(rd) + '/mockllm.jsonl')}")
    time.sleep(2.0)
    t0 = time.time()
    mark(f"start {name}")
    steps = []
    try:
        steps = sc["fn"](rd)
    except Exception as e:
        import traceback
        traceback.print_exc()
        steps.append({"error": repr(e)})
    t1 = time.time()
    mark(f"end {name}")
    time.sleep(3.0)
    post(f"{MON}/control/sync?after={int(t1 * 1000) + 1500}&timeout=20")  # last per-second eBPF dump must land in this run
    disk, leftovers = postflight()
    meta = {"name": name, "harness": HNAME, "harness_display": H.get("display"), "desc": sc["desc"], "t0": t0 * 1000, "t1": t1 * 1000, "wall_s": round(t1 - t0, 3),
            "steps": steps, "disk_usage": disk, "leftover_processes": leftovers, "agent_match": H.get("agent_match"), "container": container}
    json.dump(meta, open(os.path.join(rd, "meta.json"), "w"), indent=1)
    post(f"{MON}/control/run?dir={urllib.parse.quote(f'/lab/out/{HNAME}/idle')}")
    for s in steps:
        if isinstance(s, dict):
            print("  ", {k: v for k, v in s.items() if k in ("tag", "rc", "wall_s", "parallel", "per_prompt_s", "error", "skipped")} or {"shell": (s.get("shell") or "")[:70], "rc": s.get("rc")})
    return meta


if __name__ == "__main__":
    args = sys.argv[1:]
    hname = "pi"
    if "--harness" in args:
        i = args.index("--harness")
        hname = args[i + 1]
        del args[i: i + 2]
    if "--no-fresh" in args:
        FRESH = False
        args.remove("--no-fresh")
    if "--cold" in args:
        COLD = True
        args.remove("--cold")
    load_harness(hname)
    if not args or "--list" in args:
        for s in SCENARIOS + REAL:
            miss = [r for r in s["requires"] if not has(r)]
            print(f"{s['name']:22s} {'(skip: needs ' + ','.join(miss) + ') ' if miss else ''}{s['desc']}")
        sys.exit(0)
    chosen = []
    for a in args:
        if a == "all":
            chosen += SCENARIOS
        elif a == "real":
            chosen += REAL
        else:
            chosen += [s for s in SCENARIOS + REAL if s["name"] == a or s["name"].startswith(a + "-")]
    if any(s["real"] for s in chosen) and not load_openrouter_key():
        sys.exit("OPENROUTER_API_KEY not available")
    for sc in chosen:
        run(sc)
