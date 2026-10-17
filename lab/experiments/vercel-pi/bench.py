#!/usr/bin/env python3
"""Run the same coding task against each variant and keep every event and measurement as raw JSON.

  python3 bench.py --base https://...vercel.app --variant a --runs 3 --label vercel
  python3 bench.py --base http://127.0.0.1:3200 --variant b --runs 1 --label local
  python3 bench.py --base ... --probe --label vercel          # save /api/probe and /api/stats

Output: lab/out/vercel-pi/<label>/<variant>-<n>.json (events with client receive times, the server's "done"
record, client timings) and a one-line summary per run. The lab token is read from ~/.config/agent-lab/lab-token
and sent as a header; it is never printed."""
import argparse, json, os, pathlib, sys, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2] / "out" / "vercel-pi"
TOKEN = pathlib.Path("~/.config/agent-lab/lab-token").expanduser().read_text().strip()
TASK = "Run the tests in the workspace with npm test, find the bugs in cart.js, fix them, and re-run the tests until they pass. Keep the final answer short."


def req(base, path, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base.rstrip("/") + path, data=data, headers={"content-type": "application/json", "x-lab-token": TOKEN})
    return urllib.request.urlopen(r, timeout=timeout)


def run_once(base, variant, task, cleanup, path="/api/chat", extra=None):
    session = f"{variant}-{int(time.time() * 1000)}"
    body = {"variant": variant, "sessionId": session, "message": task, "cleanup": cleanup, **(extra or {})}
    t0 = time.time()
    events, done, error = [], None, None
    try:
        with req(base, path, body, timeout=900) as r:
            headers_ms = round((time.time() - t0) * 1000)
            for line in r:
                ev = json.loads(line)
                ev["_rx_ms"] = round((time.time() - t0) * 1000)
                if ev.get("t") in ("text", "thinking"):  # keep deltas compact
                    ev = {"t": ev["t"], "n": len(ev.get("d", "")), "_rx_ms": ev["_rx_ms"]}
                events.append(ev)
                if ev.get("t") == "done":
                    done = ev
                elif ev.get("t") == "error":
                    error = ev.get("message")
    except Exception as e:  # noqa: BLE001
        headers_ms = None
        error = f"{type(e).__name__}: {e}"
    total_ms = round((time.time() - t0) * 1000)
    first = next((e["_rx_ms"] for e in events if e.get("t") in ("text", "thinking", "tool_start")), None)
    tools = [e for e in events if e.get("t") == "tool_start"]
    return {
        "variant": variant, "session": session, "base": base, "task": task, "started": t0,
        "client": {"headers_ms": headers_ms, "first_event_ms": first, "total_ms": total_ms},
        "tool_calls": [{"name": e["name"], "args": e.get("args"), "rx_ms": e["_rx_ms"]} for e in tools],
        "tool_results": [{"name": e["name"], "isError": e.get("isError"), "output": (e.get("output") or "")[:400], "rx_ms": e["_rx_ms"]} for e in events if e.get("t") == "tool_end"],
        "passed": any("pass" in (e.get("output") or "") and "fail 0" in (e.get("output") or "") for e in events if e.get("t") == "tool_end"),
        "done": done, "error": error, "events": events,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--variant", default="a")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", required=True)
    ap.add_argument("--task", default=TASK)
    ap.add_argument("--path", default="/api/chat")
    ap.add_argument("--gap", type=float, default=0, help="seconds to wait between runs")
    ap.add_argument("--parallel", type=int, default=1, help="run this many sessions at the same time per round")
    ap.add_argument("--no-cleanup", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    out = ROOT / a.label
    out.mkdir(parents=True, exist_ok=True)
    if a.probe:
        for p in ("/api/probe", "/api/stats"):
            data = json.loads(req(a.base, p).read())
            (out / f"{p.strip('/').replace('/', '-')}-{int(time.time())}.json").write_text(json.dumps(data, indent=1))
            print(p, json.dumps(data)[:600])
        return
    for i in range(a.runs):
        if a.parallel > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(a.parallel) as ex:
                batch = list(ex.map(lambda _: run_once(a.base, a.variant, a.task, not a.no_cleanup, a.path), range(a.parallel)))
        else:
            batch = [run_once(a.base, a.variant, a.task, not a.no_cleanup, a.path)]
        for res in batch:
            res["parallel"] = a.parallel
            n = len(list(out.glob(f"{a.variant}-*.json"))) + 1
            (out / f"{a.variant}-{n:02d}.json").write_text(json.dumps(res, indent=1))
            report(a, res, n)
        if a.gap and i < a.runs - 1:
            time.sleep(a.gap)


def report(a, res, n):
    d = res["done"] or {}
    sb = d.get("sandbox") or {}
    w = d.get("worker") or {}
    print(f"{a.variant}-{n:02d} {'PASS' if res['passed'] else 'FAIL'} total {res['client']['total_ms']/1000:.1f}s first {res['client']['first_event_ms']}ms "
              f"tools {len(res['tool_calls'])} fnCPU {d.get('processCpuMs')}ms childCPU {d.get('toolChildCpuMs')}ms peakRSS {d.get('peakRssMiB')}MiB "
              f"{'cold' if d.get('cold') else 'warm'} inst {d.get('instanceId')} {('e2bCreate ' + str(sb.get('createMs')) + 'ms') if sb else ''}{('rivetCPU ' + str(w.get('processCpuMs')) + 'ms rivetRSS ' + str(w.get('rssAfterMiB'))) if w else ''} par {res.get('parallel', 1)} "
              f"tokens {d.get('usage', {}).get('input')}/{d.get('usage', {}).get('output')} err {res['error']}", flush=True)


if __name__ == "__main__":
    main()
