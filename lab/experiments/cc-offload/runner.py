#!/usr/bin/env python3
"""cc-offload runner: the same scripted coding session through Claude Code, tools local (A) or offloaded by hooks (C).

  python3 runner.py --modes A C --repeats 3      # interleaved A, C, A, C, ...
  python3 runner.py --modes A --repeats 1 --tag smoke

Per run it writes into lab/out/cc-offload/<rid>/: claude.jsonl (stream-json), cc.summary.json + cc.samples.jsonl
(the Claude Code container), tools.summary.json + tools.samples.jsonl (mode C), mock.jsonl, and a row in runs.jsonl.
Needs images cco-cc (image/), cco-mock (mock/), tsz-sandbox (tool-sizing) and volumes tsz-fixtures, tsz-cache.
"""
import argparse, json, os, pathlib, subprocess, time

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parents[1] / "out" / "cc-offload"
NET = "tsz-net"
PROMPT = "SCENARIO: plan file=/plans/coding.json say=400 tps=50 latency=800"
REAL_MODEL = "claude-sonnet-5-5"
REAL_PROMPT = ("You are in a Next.js 15 app (the current directory). Do this:\n"
               "1. Initialise a git repository and commit the files as they are.\n"
               "2. Install the dependencies with npm install.\n"
               "3. Add components/Stats.tsx: a component taking values: number[] that shows the mean, max and count. "
               "Render it on app/gen/p05/page.tsx using that page's data.\n"
               "4. Add lib/mean.mjs exporting mean and median, with node:test tests in test/mean.test.mjs, and run them with node --test.\n"
               "5. Make sure npx tsc --noEmit, npx eslint . and npx next build all pass; fix anything that fails.\n"
               "6. Commit your work with a short message.\n"
               "Keep your final answer to two sentences.")
TOKEN_FILE = os.environ.get("CCO_TOKEN_FILE")  # --real: an env file holding only CLAUDE_CODE_OAUTH_TOKEN=... (mode 600)
CC_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-lab-mock-key-000000000000", "ANTHROPIC_MODEL": "claude-sonnet-4-5-20250929",
    "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5-20251001", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5-20251001",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5-20250929", "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-1-20250805",
    "DISABLE_AUTOUPDATER": "1", "IS_SANDBOX": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1", "NEXT_CPUS": "1", "NEXT_TELEMETRY_DISABLED": "1",
}


def sh(args, check=True, timeout=None):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{r.stderr.strip()[-800:]}")
    return r.stdout


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def drop_caches():
    sh(["docker", "run", "--rm", "--privileged", "debian:trixie-slim", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"])


def run_container(name, image, cpuset, env, mounts, cmd=None, env_file=None):
    args = ["docker", "run", "-d", "--name", name, "--privileged", "--network", NET, "--cpus", "2", "--cpuset-cpus", cpuset,
            "--memory", "8g", "--memory-swap", "8g"] + (["--env-file", env_file] if env_file else [])
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    for m in mounts:
        args += ["-v", m]
    sh(args + [image] + (cmd or []))


def start_sampler(ctr, prefix):
    sh(["docker", "exec", "-d", ctr, "python3", "/opt/cco/sampler.py", prefix])
    time.sleep(0.5)


def stop_sampler(ctr, prefix):
    sh(["docker", "exec", ctr, "pkill", "-TERM", "-f", "[s]ampler.py"], check=False)
    for _ in range(100):
        if subprocess.run(["docker", "exec", ctr, "test", "-f", f"{prefix}.summary.json"]).returncode == 0:
            return
        time.sleep(0.2)


def one_run(mode, rid, outdir, real=False):
    outdir.mkdir(parents=True, exist_ok=True)
    mock, cc, tools = f"cco-mock-{rid}", f"cco-cc-{rid}", f"cco-tools-{rid}"
    drop_caches()
    if real:  # the real Anthropic API through the subscription token (env file); no mock, no API key, no model overrides
        if not TOKEN_FILE:
            raise SystemExit("--real needs CCO_TOKEN_FILE=<env file with CLAUDE_CODE_OAUTH_TOKEN>")
        env = {k: v for k, v in CC_ENV.items() if not k.startswith("ANTHROPIC_")}
        env_file = TOKEN_FILE
    else:
        run_container(mock, "cco-mock", "6-7", {"MOCK_WORKSPACE": "/work/next-app", "MOCK_LOG": "/out/mock.jsonl"},
                      [f"{HERE / 'mock'}:/plans:ro", f"{outdir}:/out"])
        env = dict(CC_ENV, ANTHROPIC_BASE_URL=f"http://{mock}:8080")
        env_file = None
    common = ["tsz-fixtures:/fixtures:ro", "tsz-cache:/cache", f"{outdir}:/out"]
    try:
        if mode == "A":
            sh(["docker", "volume", "create", f"cco-w-{rid}"])
            run_container(cc, "cco-cc", "0-1", env, [f"cco-w-{rid}:/work", *common], env_file=env_file)
            sh(["docker", "exec", cc, "sh", "-c", "mkdir -p /work && cp -a /fixtures/next-app /work/"])
        else:  # C: tools container owns the workspace; Claude Code keeps a sparse local mirror and offloads through hooks
            sh(["docker", "volume", "create", f"cco-w-{rid}"])
            run_container(tools, "cco-cc", "2-3", {"NEXT_CPUS": "1", "NEXT_TELEMETRY_DISABLED": "1"},
                          [f"cco-w-{rid}:/work", *common], ["python3", "/opt/cco/execd.py", "7070"])
            sh(["docker", "exec", tools, "sh", "-c", "cp -a /fixtures/next-app /work/"])
            env.update(CCO_TOOLS=f"http://{tools}:7070")
            run_container(cc, "cco-cc", "0-1", env, common, env_file=env_file)
            sh(["docker", "exec", cc, "mkdir", "-p", "/work/next-app"])
            for _ in range(50):
                if subprocess.run(["docker", "exec", cc, "curl", "-sf", f"http://{tools}:7070/health"], capture_output=True).returncode == 0:
                    break
                time.sleep(0.2)
            start_sampler(tools, "/out/tools")
        flags = ["--settings", "/opt/cco/settings-offload.json"] if mode == "C" else []
        if not real:
            for _ in range(50):
                if subprocess.run(["docker", "exec", cc, "curl", "-sf", f"http://{mock}:8080/health"], capture_output=True).returncode == 0:
                    break
                time.sleep(0.2)
            # warm-up launch outside the measured window: first start in a fresh HOME writes config and caches
            sh(["docker", "exec", "-w", "/work/next-app", cc, "claude", "-p", "--dangerously-skip-permissions", *flags,
                "SCENARIO: echo chars=50"], check=False, timeout=300)
        else:
            flags += ["--model", REAL_MODEL]
        start_sampler(cc, "/out/cc")
        t0 = time.time()
        r = subprocess.run(["docker", "exec", "-w", "/work/next-app", cc, "claude", "-p", "--output-format", "stream-json", "--verbose",
                            "--dangerously-skip-permissions", *flags, REAL_PROMPT if real else PROMPT], capture_output=True, text=True,
                           timeout=3600)
        wall = time.time() - t0
        stop_sampler(cc, "/out/cc")
        if mode == "C":
            stop_sampler(tools, "/out/tools")
        (outdir / "claude.jsonl").write_text(r.stdout)
        if r.stderr:
            (outdir / "claude.stderr.txt").write_text(r.stderr)
        events = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        tool_uses = [b for e in events if e.get("type") == "assistant" for b in e["message"].get("content", []) if b.get("type") == "tool_use"]
        tool_errs = [b for e in events if e.get("type") == "user" for b in (e["message"].get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")]
        result = next((e for e in events if e.get("type") == "result"), {})
        usage = result.get("usage") or {}
        row = {"rid": rid, "mode": mode, "real": real, "model": REAL_MODEL if real else "mock", "wall_s": round(wall, 2),
               "cost_usd": result.get("total_cost_usd"), "usage": usage,
               "tool_names": [b.get("name") for b in tool_uses], "rc": r.returncode, "tool_uses": len(tool_uses),
               "tool_errors": len(tool_errs), "num_turns": result.get("num_turns"), "result_subtype": result.get("subtype"),
               "cc": json.loads((outdir / "cc.summary.json").read_text()) if (outdir / "cc.summary.json").exists() else None,
               "tools": json.loads((outdir / "tools.summary.json").read_text()) if (outdir / "tools.summary.json").exists() else None}
        with open(OUT / "runs.jsonl", "a") as f:
            f.write(json.dumps({k: v for k, v in row.items()}) + "\n")
        c = row["cc"] or {}
        log(f"  {mode} {rid}: wall {wall:.1f}s tools {len(tool_uses)} errors {len(tool_errs)} | cc cpu {c.get('cgroup_cpu_s')}s "
            f"peak {c.get('mem_peak_mb')} MiB anon {c.get('anon_peak_mb')} | tools cpu {(row['tools'] or {}).get('cgroup_cpu_s')}")
        return row
    finally:
        for n in (cc, tools, mock):
            sh(["docker", "rm", "-f", n], check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", nargs="+", default=["A", "C"])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--real", action="store_true", help="real Claude via the subscription token instead of the mock")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for i in range(a.repeats):
        for m in a.modes:
            rid = f"{a.tag}-{m}-{i}-{int(time.time())}"
            log(f"{m} repeat {i}: {rid}")
            one_run(m, rid, OUT / rid, real=a.real)


if __name__ == "__main__":
    main()
