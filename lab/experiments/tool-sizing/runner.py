#!/usr/bin/env python3
"""Tool-sizing runner: start one sandbox container at a time with a CPU / memory / storage configuration, run coding-agent
tool calls in it through `step` (see image/step.py), and append one JSON row per tool call to lab/out/tool-sizing/<phase>.jsonl.

  python3 runner.py profile            # all workloads, 2 CPUs, local disk, 3 repeats (+ calibration repeats)
  python3 runner.py calib              # npm test / node --test / cpu bench x10, to scale M5 CPU-seconds to x86
  python3 runner.py cpu                # 0.25 / 0.5 / 1 / 2 / 4 CPUs, local disk
  python3 runner.py period             # 0.25 CPU with a 10 ms vs 100 ms CFS period
  python3 runner.py mem                # lowest memory limit each heavy step passes at (2 CPUs)
  python3 runner.py storage            # local / nfs / nfs-lat / hybrid / virtiofs, 2 CPUs
  python3 runner.py nfsopts            # NFS mount and export options on a few metadata-heavy steps

Needs the images tsz-sandbox and tsz-nfs and the volumes tsz-fixtures and tsz-cache (fixtures/prep.sh).
"""
import argparse, itertools, json, math, pathlib, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parents[1] / "out" / "tool-sizing"
SCRATCH = OUT / "host-dirs"  # virtiofs mode: host folders bind-mounted as /work
IMAGE, NET, NFS = "tsz-sandbox-rust", "tsz-net", "tsz-nfs"  # tsz-sandbox + Rust toolchain
BIG = 8192  # MiB, the "no limit" memory setting
_seq = itertools.count()


def sh(args, check=True, timeout=None):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{r.stderr.strip()[-800:]}")
    return r.stdout


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------- infrastructure ----------

def drop_caches():
    others = sh(["docker", "ps", "--filter", "name=tsz-sbx", "-q"]).split()
    if others:
        raise RuntimeError(f"another sandbox is running: {others}")
    sh(["docker", "run", "--rm", "--privileged", "debian:trixie-slim", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"])


def nfs_up(netem=None, export_sync="sync"):
    sh(["docker", "rm", "-f", NFS], check=False)
    sh(["docker", "network", "create", NET], check=False)
    env = ["-e", f"EXPORT_SYNC={export_sync}"] + (["-e", f"NETEM_DELAY={netem}"] if netem else [])
    sh(["docker", "run", "-d", "--name", NFS, "--privileged", "--network", NET, *env, "-v", "tsz-nfs-export:/export", "tsz-nfs"])
    for _ in range(50):
        if "nfs ready" in sh(["docker", "logs", NFS], check=False) + sh(["docker", "logs", NFS], check=False):
            break
        time.sleep(0.2)
    time.sleep(12)  # NFSv4 grace period is set to 10 s in nfs/entry.sh
    return sh(["docker", "inspect", "-f", '{{(index .NetworkSettings.Networks "tsz-net").IPAddress}}', NFS]).strip()


HYBRID_LOCAL = ["next-app/node_modules", "next-app/.next", "vite-app/node_modules", "express/node_modules", "next-pnpm/node_modules",
                "ripgrep/target"]


def workspace_mounts(mode, rid, nfs_ip=None, nfs_opts=""):
    """docker run arguments that put /work on the chosen storage, plus the venv path for Python."""
    venv = ".venv"
    if mode == "local":
        sh(["docker", "volume", "create", f"tsz-w-{rid}"])
        return ["-v", f"tsz-w-{rid}:/work"], venv
    if mode == "virtiofs":
        d = SCRATCH / "virtiofs" / rid
        d.mkdir(parents=True, exist_ok=True)
        return ["-v", f"{d}:/work"], venv
    sh(["docker", "exec", NFS, "mkdir", "-p", f"/export/{rid}"])
    vol = f"tsz-n-{rid}"
    sh(["docker", "volume", "create", "--driver", "local", "--opt", "type=nfs",
        "--opt", f"o=addr={nfs_ip},rw,nfsvers=4.2,hard{nfs_opts}", "--opt", f"device=:/{rid}", vol])
    args = ["-v", f"{vol}:/work"]
    if mode in ("hybrid", "hybrid-lat"):
        for i, p in enumerate(HYBRID_LOCAL):
            sh(["docker", "volume", "create", f"tsz-h-{rid}-{i}"])
            args += ["--mount", f"type=volume,src=tsz-h-{rid}-{i},dst=/work/{p}"]
        sh(["docker", "volume", "create", f"tsz-l-{rid}"])
        args += ["-v", f"tsz-l-{rid}:/local"]
        venv = "/local/venv"
    return args, venv


def start(cfg, rid):
    """Start a sandbox. cfg: cpus, mem_mb, storage, period_us (optional), nfs_ip, nfs_opts."""
    cpus = cfg["cpus"]
    n = max(1, math.ceil(cpus))  # vCPUs the guest would see (a 0.25-vCPU sandbox still shows 1 CPU)
    mounts, venv = workspace_mounts(cfg["storage"], rid, cfg.get("nfs_ip"), cfg.get("nfs_opts", ""))
    cpu_args = ["--cpuset-cpus", f"0-{n - 1}"]
    if cfg.get("period_us"):
        cpu_args += ["--cpu-period", str(cfg["period_us"]), "--cpu-quota", str(int(cfg["period_us"] * cpus))]
    else:
        cpu_args += ["--cpus", str(cpus)]
    env = {"NEXT_CPUS": max(1, n - 1), "PW_WORKERS": max(1, n // 2), "OPENBLAS_NUM_THREADS": n, "OMP_NUM_THREADS": n,
           "VENV": venv}
    name = f"tsz-sbx-{rid}"
    sh(["docker", "run", "-d", "--name", name, "--privileged", "--network", NET, *cpu_args,
        "--memory", f"{cfg['mem_mb']}m", "--memory-swap", f"{cfg['mem_mb']}m",
        *[x for k, v in env.items() for x in ("-e", f"{k}={v}")],
        *mounts, "-v", "tsz-fixtures:/fixtures:ro", "-v", "tsz-cache:/cache", IMAGE])
    return name


def stop(name):
    sh(["docker", "rm", "-f", name], check=False)


def run_step(name, s, timeout=1800, reclaim=True):
    out = sh(["docker", "exec", name, "step", s["name"], "--cwd", s.get("cwd", "/work"), "--timeout", str(s.get("timeout", timeout)),
              *(["--reclaim"] if reclaim else []), "--", s["cmd"]], check=False, timeout=s.get("timeout", timeout) + 120)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"name": s["name"], "rc": -1, "error": "no step output", "raw": out[-500:]}


def set_mem(name, mb):
    # reclaim page cache first so lowering the limit does not fail on cache the next step does not need
    sh(["docker", "exec", name, "sh", "-c", "echo 16G > /sys/fs/cgroup/memory.reclaim"], check=False)
    sh(["docker", "update", "--memory", f"{mb}m", "--memory-swap", f"{mb}m", name])


def record(phase, cfg, rid, extra, res):
    row = {"phase": phase, "rid": rid, "cpus": cfg["cpus"], "mem_mb": cfg["mem_mb"], "storage": cfg["storage"],
           "period_us": cfg.get("period_us"), "nfs_opts": cfg.get("nfs_opts", ""), "netem": cfg.get("netem"),
           "export_sync": cfg.get("export_sync", "sync"), "image": IMAGE, "t": round(time.time(), 1), **extra, **res}
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{phase}.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    flag = "" if res.get("rc") == 0 else f"  FAIL rc={res.get('rc')} oom={res.get('oom_kills')}"
    log(f"  {res.get('name', '?'):24} wall {res.get('wall_s', 0):8.2f}s cpu {res.get('cpu_s', 0):7.2f}s "
        f"peak {res.get('mem_peak_mb')} MiB anon {res.get('anon_peak_mb')} thr {res.get('throttled_s')}{flag}")


# ---------- workloads ----------

def S(name, cwd, cmd, timeout=1800, **kw):
    return {"name": name, "cwd": cwd, "cmd": cmd, "timeout": timeout, **kw}


def suite(calib=1, rust=True):
    st = [S("overhead-first", "/", "sleep 3"), S("cart-checkout", "/work", "cp -a /fixtures/cart /work/cart")]
    for k in range(calib):
        st.append(S(f"cart-test#{k}", "/work/cart", "node --test", group="cart-test"))
    for k in range(calib):
        st.append(S(f"cpu-bench#{k}", "/work/cart", "node bench/cpu.js", group="cpu-bench"))
    st += [
        S("express-clone", "/work", "mkdir -p express && cd express && git init -q && git fetch -q --update-shallow /fixtures/express.git HEAD "
          "&& git checkout -q FETCH_HEAD && cp /fixtures/express-package-lock.json package-lock.json"),
        S("express-install", "/work/express", "npm install --prefer-offline"),
        S("express-test", "/work/express", "npm test"),
        S("git-status", "/work/express", "git status --porcelain | wc -l"),
        S("git-log", "/work/express", "git log --stat -n 200 > /dev/null"),
        S("git-edit-commit", "/work/express", "sed -i '1s|^|// edited\\n|' lib/express.js && git diff --stat && git commit -qam edit"),
        S("react-clone", "/work", "git clone -q --no-local /fixtures/react.git react"),
        S("react-status", "/work/react", "git status --porcelain | wc -l"),
        S("react-rg", "/work/react", "rg -c useState | wc -l"),
        S("react-grep", "/work/react", "grep -rc useState --include='*.js' . | wc -l"),
        S("next-checkout", "/work", "cp -a /fixtures/next-app /work/"),
        S("next-install", "/work/next-app", "npm install --prefer-offline"),
        S("next-lint", "/work/next-app", "npx eslint ."),
        S("next-tsc", "/work/next-app", "npx tsc --noEmit --incremental false"),
        S("next-build", "/work/next-app", "npx next build"),
        S("nm-rg", "/work/next-app", "rg -c useEffect node_modules | wc -l"),
        S("nm-grep", "/work/next-app", "grep -rc useEffect node_modules | wc -l"),
        S("dev-start", "/work/next-app", "(nohup npx next dev -p 3100 > /tmp/dev.log 2>&1 &); for i in $(seq 1 1200); do "
          "curl -sf -o /dev/null http://127.0.0.1:3100/ && exit 0; sleep 0.5; done; exit 1"),
        S("dev-idle", "/", "sleep 10"),
        S("dev-page", "/", "curl -sf -o /dev/null http://127.0.0.1:3100/gen/p05"),
        S("dev-edit", "/work/next-app", "sed -i 's/Generated page 05/Edited page 05/' app/gen/p05/page.tsx && sleep 0.5 && "
          "curl -sf http://127.0.0.1:3100/gen/p05 | grep -c 'Edited page 05'"),
        S("cart-test-with-dev", "/work/cart", "node --test"),
        S("dev-stop", "/", "pkill -f '[n]ext dev'; pkill -f '[n]ext-server'; sleep 1; true"),
        S("vite-checkout", "/work", "cp -a /fixtures/vite-app /work/"),
        S("vite-install", "/work/vite-app", "npm install --prefer-offline"),
        S("vite-tsc", "/work/vite-app", "npx tsc -b"),
        S("vite-build", "/work/vite-app", "npx vite build"),
        S("vite-test", "/work/vite-app", "npx vitest run"),
        S("playwright", "/work/vite-app", "npx playwright test"),
        S("pnpm-checkout", "/work", "mkdir -p next-pnpm && cp -a /fixtures/next-app/. next-pnpm/ && cp /fixtures/next-pnpm-lock.yaml next-pnpm/pnpm-lock.yaml"),
        S("pnpm-install", "/work/next-pnpm", "pnpm install --frozen-lockfile --prefer-offline --store-dir /cache/pnpm-store"),
        S("py-checkout", "/work", "cp -a /fixtures/py /work/"),
        S("py-venv", "/work/py", 'python3 -m venv "$VENV"'),
        S("py-install", "/work/py", '"$VENV/bin/pip" install -q -r requirements.txt'),
        S("py-test", "/work/py", '"$VENV/bin/pytest" -q'),
        S("py-pandas", "/work/py", '"$VENV/bin/python" pandas_job.py'),
    ]
    if rust:
        st += [S("rust-checkout", "/work", "cp -a /fixtures/ripgrep /work/"),
               S("rust-build", "/work/ripgrep", "cargo build --release --offline -q", timeout=3600)]
    st.append(S("overhead-last", "/", "sleep 3"))
    return st


def run_suite(phase, cfg, steps, extra=None):
    rid = f"{phase}-{int(time.time())}-{next(_seq)}"
    drop_caches()
    name = start(cfg, rid)
    log(f"{phase} {rid}: cpus={cfg['cpus']} mem={cfg['mem_mb']} storage={cfg['storage']} {extra or ''}")
    try:
        for s in steps:
            res = run_step(name, s, reclaim=cfg.get("reclaim", True))
            record(phase, cfg, rid, {**(extra or {}), "group": s.get("group", s["name"])}, res)
    finally:
        stop(name)


# ---------- phases ----------

def phase_profile(a):
    cfg = {"cpus": 2, "mem_mb": BIG, "storage": "local"}
    for r in range(a.repeats):
        run_suite("profile", cfg, suite(calib=4), {"repeat": r})
    cold = [S("next-checkout", "/work", "cp -a /fixtures/next-app /work/"),
            S("next-install-cold", "/work/next-app", "npm_config_cache=/tmp/empty-cache npm install")]
    run_suite("profile", cfg, cold, {"repeat": 0, "variant": "cold-cache"})


def phase_calib(a):
    """Same commands whose x86 cost we already know: vercel-pi ran `npm test` on the cart project (Vercel, Xeon 2.5 GHz) and
    bench/cpu.js (Vercel and E2B). 10 repeats each, 1 and 2 CPUs."""
    for cpus in (1, 2):
        steps = [S("cart-checkout", "/work", "cp -a /fixtures/cart /work/cart")]
        steps += [S(f"cart-npm-test#{k}", "/work/cart", "npm test", group="cart-npm-test") for k in range(10)]
        steps += [S(f"cart-test#{k}", "/work/cart", "node --test", group="cart-test") for k in range(10)]
        steps += [S(f"node-startup#{k}", "/", "node -e 0", group="node-startup") for k in range(10)]
        steps += [S(f"cpu-bench#{k}", "/work/cart", "node bench/cpu.js", group="cpu-bench") for k in range(5)]
        run_suite("calib", {"cpus": cpus, "mem_mb": BIG, "storage": "local"}, steps, {"repeat": 0})


def phase_cpu(a):
    for r in range(a.repeats):
        for cpus in a.cpus:
            run_suite("cpu", {"cpus": cpus, "mem_mb": BIG, "storage": "local"}, suite(calib=2), {"repeat": r})


def phase_period(a):
    names = {"cart-checkout", "cart-test#0", "cart-test#1", "next-checkout", "next-install", "next-tsc", "next-build",
             "vite-checkout", "vite-install", "vite-test", "express-clone", "express-install", "express-test"}
    steps = [s for s in suite(calib=2) if s["name"] in names]
    for period in (100000, 10000):
        run_suite("period", {"cpus": 0.25, "mem_mb": BIG, "storage": "local", "period_us": period}, steps, {"repeat": 0})


MEM_LIMITS = [3072, 2048, 1536, 1024, 768, 512, 384, 256]


def mem_chains():
    """(chain, prerequisite steps, targets). Each target builds its command from a tag so every attempt starts clean."""
    nx = [S("next-checkout", "/work", "cp -a /fixtures/next-app /work/"), S("next-install", "/work/next-app", "npm install --prefer-offline")]
    vt = [S("vite-checkout", "/work", "cp -a /fixtures/vite-app /work/"), S("vite-install", "/work/vite-app", "npm install --prefer-offline"),
          S("vite-build", "/work/vite-app", "npx vite build")]
    ex = [S("express-clone", "/work", "mkdir -p express && cd express && git init -q && git fetch -q --update-shallow /fixtures/express.git HEAD "
            "&& git checkout -q FETCH_HEAD && cp /fixtures/express-package-lock.json package-lock.json"),
          S("express-install", "/work/express", "npm install --prefer-offline")]
    py = [S("py-checkout", "/work", "cp -a /fixtures/py /work/"), S("py-venv", "/work/py", 'python3 -m venv "$VENV"'),
          S("py-install", "/work/py", '"$VENV/bin/pip" install -q -r requirements.txt')]
    # start the dev server in its own process group and kill that group by id: a pkill -f pattern would also match this
    # step's own command line, which contains the start command
    dev = ("setsid npx next dev -p 3100 > /tmp/dev.log 2>&1 & pg=$!; ok=0; for i in $(seq 1 1200); do curl -sf -o /dev/null "
           "http://127.0.0.1:3100/ && ok=1 && break; sleep 0.5; done; [ $ok = 1 ] && curl -sf -o /dev/null http://127.0.0.1:3100/gen/p05 "
           "&& curl -sf -o /dev/null http://127.0.0.1:3100/gen/p17; rc=$?; kill -TERM -- -$pg; sleep 1; exit $rc")
    return [
        ("next", nx, [
            ("next-install", lambda t: S("next-install", "/work", f"mkdir -p ni-{t} && cp -a /fixtures/next-app/. ni-{t}/ && cd ni-{t} && npm install --prefer-offline")),
            ("next-tsc", lambda t: S("next-tsc", "/work/next-app", "npx tsc --noEmit --incremental false")),
            ("next-lint", lambda t: S("next-lint", "/work/next-app", "npx eslint .")),
            ("next-build", lambda t: S("next-build", "/work/next-app", f"DIST_DIR=.next-{t} npx next build")),
        ]),
        ("nextdev", nx, [("next-dev", lambda t: S("next-dev", "/work/next-app", dev))]),
        ("vite", vt, [
            ("vite-build", lambda t: S("vite-build", "/work/vite-app", f"npx vite build --outDir dist-{t}")),
            ("vite-test", lambda t: S("vite-test", "/work/vite-app", "npx vitest run")),
            ("playwright", lambda t: S("playwright", "/work/vite-app", "npx playwright test")),
        ]),
        ("express", ex, [("express-test", lambda t: S("express-test", "/work/express", "npm test"))]),
        ("rust", [S("rust-checkout", "/work", "cp -a /fixtures/ripgrep /work/")], [
            ("rust-build", lambda t: S("rust-build", "/work/ripgrep", f"CARGO_TARGET_DIR=target-{t} cargo build --release --offline -q", timeout=3600)),
        ]),
        ("py", py, [
            ("py-install", lambda t: S("py-install", "/work/py", f'python3 -m venv v-{t} && v-{t}/bin/pip install -q -r requirements.txt')),
            ("py-test", lambda t: S("py-test", "/work/py", '"$VENV/bin/pytest" -q')),
            ("py-pandas", lambda t: S("py-pandas", "/work/py", '"$VENV/bin/python" pandas_job.py')),
        ]),
    ]


def phase_mem(a):
    for chain, prereq, targets in mem_chains():
        if a.only and chain not in a.only:
            continue
        cfg = {"cpus": 2, "mem_mb": BIG, "storage": "local"}
        rid = f"mem-{chain}-{int(time.time())}"
        drop_caches()
        name = start(cfg, rid)
        log(f"mem {rid}")
        try:
            for s in prereq:
                record("mem", cfg, rid, {"group": s["name"], "role": "prereq"}, run_step(name, s))
            for tname, mk in targets:
                for lim in MEM_LIMITS:
                    set_mem(name, lim)
                    res = run_step(name, mk(f"{tname}-{lim}"))
                    set_mem(name, BIG)
                    record("mem", {**cfg, "mem_mb": lim}, rid, {"group": tname, "role": "target", "limit_mb": lim}, res)
                    if res.get("rc") != 0:
                        break
        finally:
            stop(name)


def phase_storage(a):
    modes = a.modes
    for mode in modes:
        cfg = {"cpus": 2, "mem_mb": BIG, "storage": mode, "reclaim": False}  # keep page cache: its speed effect is the point
        if mode in ("nfs", "nfs-lat", "hybrid", "hybrid-lat"):
            cfg["netem"] = "1ms" if mode.endswith("-lat") else None
            cfg["nfs_ip"] = nfs_up(netem=cfg["netem"])
        for r in range(a.repeats):
            run_suite("storage", cfg, suite(calib=1), {"repeat": r})


def phase_nfsopts(a):
    names = {"express-clone", "git-status", "git-log", "git-edit-commit", "react-clone", "react-status", "react-rg",
             "next-checkout", "next-install", "next-tsc", "nm-rg"}
    steps = [s for s in suite(calib=0) if s["name"] in names]
    variants = [("default", "", "sync"), ("nconnect4", ",nconnect=4", "sync"), ("actimeo60", ",actimeo=60", "sync"),
                ("noac", ",noac", "sync"), ("async-export", "", "async")]
    for label, opts, export_sync in variants:
        ip = nfs_up(netem="1ms", export_sync=export_sync)
        cfg = {"cpus": 2, "mem_mb": BIG, "storage": "nfs-lat", "nfs_ip": ip, "nfs_opts": opts, "netem": "1ms", "export_sync": export_sync,
               "reclaim": False}
        run_suite("nfsopts", cfg, steps, {"repeat": 0, "variant": label})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["profile", "calib", "cpu", "period", "mem", "storage", "nfsopts", "smoke"])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--cpus", type=float, nargs="+", default=[0.25, 0.5, 1, 2, 4])
    ap.add_argument("--modes", nargs="+", default=["local", "nfs", "nfs-lat", "hybrid", "virtiofs"])
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    if a.phase == "smoke":
        steps = [S("overhead-first", "/", "sleep 3"), S("peak-200", "/", "node -e \"const a=[];for(let i=0;i<20;i++)a.push(Buffer.alloc(10<<20,1))\""),
                 S("peak-20", "/", "node -e \"const a=[];for(let i=0;i<2;i++)a.push(Buffer.alloc(10<<20,1))\""),
                 S("cgroup-path", "/", "cat /proc/self/cgroup; nproc; echo NEXT_CPUS=$NEXT_CPUS"), S("overhead-0.25", "/", "sleep 5")]
        run_suite("smoke", {"cpus": 0.25, "mem_mb": 512, "storage": "local"}, steps)
        return
    {"profile": phase_profile, "calib": phase_calib, "cpu": phase_cpu, "period": phase_period, "mem": phase_mem, "storage": phase_storage,
     "nfsopts": phase_nfsopts}[a.phase](a)


if __name__ == "__main__":
    main()
