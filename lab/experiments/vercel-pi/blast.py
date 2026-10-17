#!/usr/bin/env python3
"""Blast radius on variant A: an innocent chat runs, then an out-of-memory chat starts while it is still going.
Did they share a Vercel instance, and did the innocent one die with it? Saves lab/out/vercel-pi/heavy/blast/trial-NN.json."""
import json, pathlib, sys, threading, time
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import heavy

BASE = sys.argv[1]
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
DELAY = float(sys.argv[3]) if len(sys.argv) > 3 else 8
out = heavy.ROOT / "blast"
out.mkdir(parents=True, exist_ok=True)
for n in range(1, TRIALS + 1):
    res = {}
    def innocent():
        res["innocent"] = heavy.run_turn(BASE, "a", f"cart-blast{int(time.time()*1000)}", heavy.TASKS["cart"], "cart", cleanup=True)
    def oom():
        time.sleep(DELAY)
        res["oom"] = heavy.run_turn(BASE, "a", f"oom-blast{int(time.time()*1000)}", heavy.TASKS["oom"], "oom", cleanup=True)
    ts = [threading.Thread(target=innocent), threading.Thread(target=oom)]
    [t.start() for t in ts]; [t.join() for t in ts]
    f = out / f"trial-{n:02d}.json"
    f.write_text(json.dumps(res, indent=1))
    for k in ("innocent", "oom"):
        r = res[k]; d = r.get("done") or {}
        print(f"trial {n} {k:8s} {r['client']['total_ms']/1000:6.1f}s tools {len(r['tool_calls'])} inst {d.get('instanceId')} cold {d.get('cold')} inFlight@end {d.get('concurrentAtEnd')} maxInFlight {d.get('maxInFlight')} err {(r['error'] or '')[:90]}", flush=True)
    time.sleep(10)
