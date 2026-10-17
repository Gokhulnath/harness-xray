#!/usr/bin/env python3
"""Heavy end-to-end scenarios against the deployed app (variants a, b, c). Keeps every event and measurement as raw JSON.

  python3 heavy.py --base URL --scenario cpu    --variants abc --runs 2
  python3 heavy.py --base URL --scenario memory --variants abc --sessions 3 --turn-gap 20
  python3 heavy.py --base URL --scenario slow   --variants abc
  python3 heavy.py --base URL --scenario cart   --variants abc --parallel 5

Scenarios: cpu (8M SHA-256 in a tool), npm (npm install + fix 2 bugs), repo (300-file grep), slow (5.5-min command,
beyond Vercel's 300 s limit, then a follow-up question), memory (5 turns in one session), cart (the original task).
Output: lab/out/vercel-pi/heavy/<scenario>/<variant>-NN.json. The lab token comes from ~/.config/agent-lab/lab-token."""
import argparse, json, pathlib, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[2] / "out" / "vercel-pi" / "heavy"
TOKEN = pathlib.Path("~/.config/agent-lab/lab-token").expanduser().read_text().strip()

TASKS = {
    "cart": "Run the tests in the workspace with npm test, find the bugs in cart.js, fix them, and re-run the tests until they pass. Keep the final answer short.",
    "cpu": "Run `node bench/cpu.js` in the workspace. It performs a fixed amount of hashing. Report the checksum and the elapsed_ms it prints. Do not modify the script.",
    "npm": "In the workspace: run npm install, then npm test. Fix the failing tests by changing src/report.js only (not the tests), then re-run npm test until it passes. Keep the final answer short.",
    "repo": "The workspace has a generated repository in src/modules (300 files). Use grep to find the file that defines the function calculateShippingCost, explain in two sentences what it does, and tell me exactly how many files import it.",
    "slow": "Run `node bench/slow.js` in the workspace and wait for it to finish; it takes about 5.5 minutes and prints progress every 15 seconds. Then tell me the exact last line it printed.",
    "oom": "Run `node bench/mem.js` in the workspace. It allocates memory in 100 MB steps up to 3000 MB. Report the last line it printed and whether it finished.",
    "disk": "Run `node bench/disk.js` in the workspace. It writes 50 MB files up to 800 MB. Report the last line it printed.",
}
FOLLOWUPS = {
    "slow": "What was the exact last line printed by `node bench/slow.js`? If it is still running, wait for it to finish first.",
    "oom": "Run `free -m` and tell me how much memory is available right now.",
    "disk": "Run `df -h . /tmp` and tell me how much disk space is free.",
}
MEMORY_TURNS = [
    "Remember this code word for later: PINEAPPLE-42. Also create a file notes.txt in the workspace containing the line 'turn 1'.",
    "Append the line 'turn 2' to notes.txt.",
    "Append the line 'turn 3' to notes.txt.",
    "Append the line 'turn 4' to notes.txt.",
    "What is the code word I gave you earlier? Also show the full contents of notes.txt.",
]
lock = threading.Lock()


def outputs(res):
    return " ".join((t.get("output") or "") for t in res["tool_results"])


def check(scenario, res, turn=None):
    a, o = res.get("answer") or "", outputs(res)
    if scenario == "cpu":
        return "78645d20e7d5a43a" in a or "78645d20e7d5a43a" in o
    if scenario in ("npm", "cart"):
        return any(("pass" in (t.get("output") or "")) and ("fail 0" in (t.get("output") or "")) for t in res["tool_results"])
    if scenario == "repo":
        return "mod_137" in (a + o) and "23" in a
    if scenario == "slow":
        return "DONE checksum-2310" in (a + o)
    if scenario == "memory":
        return "PINEAPPLE-42" in a.upper() and all(f"turn {i}" in (a + o) for i in range(1, 5)) if turn == 5 else None
    return None


def post(base, path, body, timeout=900):
    r = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(), headers={"content-type": "application/json", "x-lab-token": TOKEN})
    return urllib.request.urlopen(r, timeout=timeout)


def run_turn(base, variant, session, message, scenario, cleanup, extra=None):
    path = "/api/chatc" if variant == "c" else "/api/chat"
    body = {"variant": variant, "sessionId": session, "message": message, "scenario": scenario, "cleanup": cleanup, **(extra or {})}
    t0 = time.time()
    events, done, error, text, status = [], None, None, [], None
    try:
        with post(base, path, body) as r:
            status = r.status
            for line in r:
                ev = json.loads(line)
                ev["_rx_ms"] = round((time.time() - t0) * 1000)
                if ev.get("t") == "text":
                    text.append(ev.get("d", ""))
                if ev.get("t") in ("text", "thinking"):
                    ev = {"t": ev["t"], "n": len(ev.get("d", "")), "_rx_ms": ev["_rx_ms"]}
                events.append(ev)
                if ev.get("t") == "done":
                    done = ev
                elif ev.get("t") == "error":
                    error = ev.get("message")
    except urllib.error.HTTPError as e:
        status = e.code
        error = f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"
    except Exception as e:  # noqa: BLE001  (stream cut by a platform timeout lands here)
        error = f"{type(e).__name__}: {e}"
    total = round((time.time() - t0) * 1000)
    if done is None and error is None:
        error = "stream ended without a done record (connection closed)"
    return {
        "variant": variant, "scenario": scenario, "session": session, "message": message, "started": t0, "http_status": status,
        "client": {"first_event_ms": next((e["_rx_ms"] for e in events if e.get("t") in ("text", "thinking", "tool_start")), None), "total_ms": total, "last_event_ms": events[-1]["_rx_ms"] if events else None},
        "answer": "".join(text),
        "tool_calls": [{"name": e["name"], "args": e.get("args"), "rx_ms": e["_rx_ms"]} for e in events if e.get("t") == "tool_start"],
        "tool_results": [{"name": e["name"], "isError": e.get("isError"), "output": (e.get("output") or "")[:2000], "rx_ms": e["_rx_ms"]} for e in events if e.get("t") == "tool_end"],
        "done": done, "error": error, "events": events,
    }


def save(scenario, variant, obj):
    out = ROOT / scenario
    with lock:
        out.mkdir(parents=True, exist_ok=True)
        n = len(list(out.glob(f"{variant}-*.json"))) + 1
        f = out / f"{variant}-{n:02d}.json"
        f.write_text(json.dumps(obj, indent=1))
    return f.name


def summary_line(r):
    d = r.get("done") or {}
    w = d.get("worker") or {}
    sb = d.get("sandbox") or {}
    return (f"{'PASS' if r.get('passed') else 'FAIL' if r.get('passed') is False else '----'} {r['client']['total_ms']/1000:6.1f}s "
            f"tools {len(r['tool_calls']):2d} fnCPU {d.get('processCpuMs')} child {d.get('toolChildCpuMs')} rss {d.get('peakRssMiB')} "
            f"{'cold' if d.get('cold') else 'warm'} inst {d.get('instanceId')} new_session {d.get('sessionCreated')} "
            f"{('e2b ' + str(sb.get('createMs')) + 'ms') if sb else ''}{(' rivetCPU ' + str(w.get('processCpuMs')) + ' rivetInst ' + str(w.get('instanceId'))) if w else ''} "
            f"tok {(d.get('usage') or {}).get('input')}/{(d.get('usage') or {}).get('output')} err {(r['error'] or '')[:160]}")


def one_session(base, variant, scenario, args, idx):
    stamp = f"{int(time.time() * 1000)}{idx}"
    session = f"{scenario}-{variant}{stamp}"
    if scenario == "memory":
        turns = []
        for i, msg in enumerate(MEMORY_TURNS, 1):
            r = run_turn(base, variant, session, msg, scenario, cleanup=(i == len(MEMORY_TURNS)))
            r["turn"] = i
            r["passed"] = check(scenario, r, turn=i)
            turns.append(r)
            print(f"  {variant} {session} turn {i}: {summary_line(r)}", flush=True)
            if i < len(MEMORY_TURNS) and args.turn_gap:
                time.sleep(args.turn_gap)
        obj = {"variant": variant, "scenario": scenario, "session": session, "turns": turns, "passed": turns[-1]["passed"],
               "instances": [((t.get("done") or {}).get("instanceId"), (t.get("done") or {}).get("sessionCreated")) for t in turns]}
        name = save(scenario, variant, obj)
        print(f"{name} {'PASS' if obj['passed'] else 'FAIL'} instances/new_session per turn {obj['instances']}", flush=True)
        return obj
    r = run_turn(base, variant, session, TASKS[scenario], scenario, cleanup=(scenario not in FOLLOWUPS))
    r["passed"] = check(scenario, r)
    if scenario in FOLLOWUPS:
        # same session again: did the instance, the conversation and the workspace survive the first request?
        time.sleep(5)
        f = run_turn(base, variant, session, FOLLOWUPS[scenario], scenario, cleanup=True, extra={"whenBusy": "followUp"})
        f["passed"] = check(scenario, f)
        r["followup"] = f
        print(f"  {variant} first:    {summary_line(r)}", flush=True)
        print(f"  {variant} followup: {summary_line(f)}", flush=True)
    r["parallel"] = args.parallel
    name = save(scenario, variant, r)
    print(f"{name} {summary_line(r)}", flush=True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--scenario", required=True, choices=["cart", "cpu", "npm", "repo", "slow", "memory", "oom", "disk"])
    ap.add_argument("--variants", default="abc")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=1, help="sessions at the same time per variant")
    ap.add_argument("--sessions", type=int, default=0, help="memory: concurrent sessions per variant (default = --parallel)")
    ap.add_argument("--turn-gap", type=float, default=0)
    ap.add_argument("--gap", type=float, default=5)
    ap.add_argument("--variants-together", action="store_true", help="run the variants at the same time instead of one after another")
    a = ap.parse_args()
    width = a.sessions or a.parallel

    def run_variant(v):
        for i in range(a.runs):
            if width > 1:
                with ThreadPoolExecutor(width) as ex:
                    list(ex.map(lambda k: one_session(a.base, v, a.scenario, a, k), range(width)))
            else:
                one_session(a.base, v, a.scenario, a, 0)
            if i < a.runs - 1:
                time.sleep(a.gap)

    if a.variants_together:
        with ThreadPoolExecutor(len(a.variants)) as ex:
            list(ex.map(run_variant, a.variants))
    else:
        for v in a.variants:
            run_variant(v)


if __name__ == "__main__":
    main()
