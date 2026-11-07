#!/bin/zsh
# Alternate node and bun, 3 reps per config. /usr/bin/time -l adds whole-process CPU and max RSS.
out=results.jsonl; : > $out
for cfg in "1 1" "1 20" "50 4" "200 1"; do
  for rep in 1 2 3; do
    for rt in node bun; do
      t=$( { /usr/bin/time -l $rt bench.mjs ${=cfg} > line.json; } 2>&1 )
      maxrss=$(echo "$t" | awk '/maximum resident set size/{print $1}')
      real=$(echo "$t" | awk '/ real /{print $1}'); user=$(echo "$t" | awk '/ real /{print $3}'); sys=$(echo "$t" | awk '/ real /{print $5}')
      python3 -c "import json,sys; d=json.loads(open('line.json').read().strip().splitlines()[-1]); d.update(rep=$rep, procMaxRssMiB=round($maxrss/1048576,1), procWallS=$real, procCpuS=round($user+$sys,3)); print(json.dumps(d))" >> $out
    done
  done
done
echo done
