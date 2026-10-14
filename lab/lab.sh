#!/usr/bin/env bash
# Agent lab control.  ./lab.sh up [harness] | down | build [harness] | run <harness> <scenarios...> | status
set -euo pipefail
cd "$(dirname "$0")"
cmd=${1:-status}; shift || true
# the grep/find scenarios search a ~590-file TypeScript repo: pi's own sources, recovered from the
# source maps in its npm package (not stored in this repository)
make_seed() {
  [ -d base/workspace-seed/bigrepo ] && return
  tmp=$(mktemp -d); (cd "$tmp" && npm pack -q @earendil-works/pi-coding-agent@0.87.1 >/dev/null && tar -xzf ./*.tgz)
  python3 tools/extract_pi_sources.py "$tmp/package" base/workspace-seed/bigrepo; rm -rf "$tmp"
}
case "$cmd" in
  build) H=${1:-pi}; make_seed; docker build -q -t lab-agent-base base/ >/dev/null; HARNESS=$H docker compose build ;;
  up)    H=${1:-pi}; make_seed; docker build -q -t lab-agent-base base/ >/dev/null; HARNESS=$H docker compose up -d --build
         echo "dashboard: http://localhost:8765   explorer: http://localhost:8765/explorer/" ;;
  down)  docker compose down ;;
  run)   H=$1; shift; HARNESS=$H docker compose up -d >/dev/null; python3 -u run.py --harness "$H" "$@" ;;
  status) docker compose ps; curl -s localhost:8765/health; echo ;;
  *) echo "usage: $0 up|down|build|run|status"; exit 1 ;;
esac
