#!/bin/zsh
# pi-durable on Node vs the bare loop on Node vs the same loop in Rust. Whole-process numbers from /usr/bin/time -l.
out=results3.jsonl; : > $out
typeset -A CMD
CMD[pi-durable-node]="node bench.mjs"; CMD[mini-node]="node mini.mjs"; CMD[mini-rust]="./mini-rs/target/release/mini"
for cfg in "1 1" "1 100" "50 4" "200 1"; do
  for rep in 1 2 3; do
    for impl in pi-durable-node mini-node mini-rust; do
      t=$( { /usr/bin/time -l ${=CMD[$impl]} ${=cfg} > line.json; } 2>&1 )
      ok=$(grep -c . line.json)
      maxrss=$(echo "$t" | awk '/maximum resident set size/{print $1}')
      real=$(echo "$t" | awk '/ real /{print $1}'); user=$(echo "$t" | awk '/ real /{print $3}'); sys=$(echo "$t" | awk '/ real /{print $5}')
      set -- ${=cfg}
      echo "{\"impl\":\"$impl\",\"sessions\":$1,\"turnsPerSession\":$2,\"rep\":$rep,\"ok\":$ok,\"wallS\":$real,\"cpuS\":$(echo "$user + $sys" | bc),\"maxRssMiB\":$(echo "scale=1; $maxrss/1048576" | bc),\"out\":$(tail -1 line.json | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))')}" >> $out
    done
  done
done
echo done
