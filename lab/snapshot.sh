#!/usr/bin/env bash
# Pack one harness's raw traces + the database + the explorer bundle into a dated archive.
#   ./snapshot.sh pi            -> out/snapshots/pi-YYYYmmdd-HHMMSS.tar.gz
# Restore: tar -xzf out/snapshots/<file> -C /some/dir ; open <dir>/explorer/index.html via any static server.
set -euo pipefail
cd "$(dirname "$0")"
H=${1:-pi}
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p out/snapshots
OUTF="out/snapshots/${H}-${TS}.tar.gz"
tar -czf "$OUTF" \
  "out/${H}" out/agentlab.sqlite explorer -C .. docs reports 2>/dev/null || tar -czf "$OUTF" "out/${H}" out/agentlab.sqlite explorer
echo "wrote $OUTF ($(du -h "$OUTF" | cut -f1))"
