#!/bin/bash
# Start N pi chats at once on this (2-vCPU) container; model waits 6 s so all stay alive after sending.
cg=/sys/fs/cgroup
for N in 1 2 4 8 16 32; do
  f=/cap/N$N.jsonl; rm -f $f
  start=$(date +%s%3N)
  for i in $(seq 1 $N); do SNAP_FILE=$f NODE_OPTIONS="--require /cap/snap2.cjs" PI_OFFLINE=1 pi --mode json "SCENARIO: chat chars=200 latency=6000" >/dev/null 2>&1 & done
  # wait until all N have sent their first request (max 20 s)
  for k in $(seq 1 400); do [ -f $f ] && [ $(wc -l < $f) -ge $N ] && break; sleep 0.05; done
  sleep 0.3
  cur=$(cat $cg/memory.current); anon=$(awk '/^anon /{print $2}' $cg/memory.stat); file=$(awk '/^file /{print $2}' $cg/memory.stat)
  echo "N=$N start=$start mem_current=$cur anon=$anon file=$file"
  wait
  sleep 1
done
