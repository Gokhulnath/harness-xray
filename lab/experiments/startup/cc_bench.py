import os, sys, time, statistics as st, subprocess
def run(argv, env):
    t0=time.perf_counter(); p=subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    _, status, ru = os.wait4(p.pid, 0); wall=(time.perf_counter()-t0)*1000
    return wall, (ru.ru_utime+ru.ru_stime)*1000, ru.ru_maxrss/1024, ru.ru_minflt
label=sys.argv[1]; env=dict(os.environ, PI_OFFLINE="1")
for name, argv in [("--version", ["pi","--version"]), ("json chat", ["pi","--mode","json","SCENARIO: chat chars=200"])]:
    rows=[run(argv, env) for _ in range(16)]
    first, rest = rows[0], rows[1:]
    med=lambda i: st.median(r[i] for r in rest)
    print(f"{label:<14} {name:<10} first: cpu {first[1]:6.1f} ms wall {first[0]:6.1f} rss {first[2]:5.1f} MiB | warm median (n=15): cpu {med(1):6.1f} ms  wall {med(0):6.1f} ms  rss {med(2):5.1f} MiB  minflt {med(3):6.0f}")
