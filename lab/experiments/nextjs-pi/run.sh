#!/usr/bin/env bash
# Start the proof of concept: Next.js+pi container ("Vercel") and a separate sandbox container ("E2B").
#   KEY_FILE=path/to/.env ./run.sh            real model via OpenRouter (key passed to docker by name only)
#   PI_PROVIDER=mock ./run.sh                 lab mock LLM (needs the lab's mockllm container on lab_default)
set -euo pipefail
cd "$(dirname "$0")"
PROVIDER="${PI_PROVIDER:-openrouter}"
TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"

docker rm -f xray-next xray-sandbox >/dev/null 2>&1 || true
docker network inspect xray-poc >/dev/null 2>&1 || docker network create xray-poc >/dev/null

docker run -d --name xray-sandbox --network xray-poc -p 127.0.0.1:49983:49983 \
  -e SANDBOX_TOKEN="$TOKEN" xray-poc-sandbox >/dev/null

extra=()
if [ "$PROVIDER" = openrouter ]; then
  if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -n "${KEY_FILE:-}" ]; then
    OPENROUTER_API_KEY="$(grep -E '^(export )?(OPENROUTER_API_KEY|OPEN_ROUTER_API_KEY)=' "$KEY_FILE" | head -1 | cut -d= -f2- | tr -d "\"'")"
  fi
  [ -n "${OPENROUTER_API_KEY:-}" ] || { echo "no OPENROUTER_API_KEY (set it or KEY_FILE)"; exit 1; }
  export OPENROUTER_API_KEY
  extra+=(-e OPENROUTER_API_KEY)
else
  extra+=(-e MOCK_BASE_URL=http://mockllm:8080/v1 -e PI_MODEL=mock-1)
fi

docker run -d --name xray-next --network xray-poc -p 127.0.0.1:3100:3100 \
  -e PI_PROVIDER="$PROVIDER" -e PRELOAD="${PRELOAD:-1}" ${PI_MODEL:+-e PI_MODEL="$PI_MODEL"} \
  -e SANDBOX_URL=http://xray-sandbox:49983 -e SANDBOX_TOKEN="$TOKEN" "${extra[@]}" xray-poc-next >/dev/null
[ "$PROVIDER" = mock ] && docker network connect lab_default xray-next

for _ in $(seq 1 60); do curl -sf http://127.0.0.1:3100/api/stats >/dev/null && break; sleep 0.5; done
echo "app:     http://127.0.0.1:3100   (provider: $PROVIDER)"
curl -s http://127.0.0.1:3100/api/stats; echo
