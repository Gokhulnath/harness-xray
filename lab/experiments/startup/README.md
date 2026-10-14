# Startup experiments (run in throwaway harness containers, no lab monitor needed)

Used for `reports/REPORT.md` §3.9 and Part 5.3 fix 4. Mount this folder and run inside `lab-agent-pi`
on the lab network so `mockllm` resolves:

```bash
# cumulative counters at request sent / first byte / reply done / exit (snap.cjs), + strace -c counts
docker run --rm --network lab_default -u agent -v $PWD:/snap --cap-add SYS_PTRACE \
  --security-opt seccomp=unconfined -w /workspace --entrypoint bash lab-agent-pi /snap/run.sh broken
python3 agg.py .            # medians per moment (expects broken.jsonl / cached.jsonl)

# compile cache on/off: add -e NODE_COMPILE_CACHE=/home/agent/.cache/ncc
docker run --rm --network lab_default -u agent -v $PWD/cc_bench.py:/b.py:ro --entrypoint python3 lab-agent-pi /b.py label

# N agents started at once on 2 vCPU (snap2.cjs + burst.sh mounted at /cap)
docker run --rm --network lab_default -u agent --cpuset-cpus=0,1 -v $PWD:/cap -w /workspace \
  --entrypoint bash lab-agent-pi /cap/burst.sh

# V8 CPU profile of startup: node --cpu-prof --cpu-prof-interval 50 .../dist/bundle/cli.js ...
python3 prof_agg.py <dir-with-.cpuprofile>          # by file / bucket
python3 prof_hot.py <dir> <chunk-OJP47DM6.js>       # hottest functions in pi's main chunk
```

`snap.cjs` / `snap2.cjs` are `--require` preloads that subscribe to undici's diagnostics channels and
record `process.resourceUsage()` (exact, includes exited threads), memory and `/proc/self` counters.
Note: the snapshot files they append to are counted in wchar/write_bytes after the first moment.
