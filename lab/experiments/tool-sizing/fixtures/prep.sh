#!/bin/bash
# One-time fixture prep (network on). Writes project templates without node_modules into /fixtures and warms the
# shared package caches in /cache by installing throwaway copies under /tmp (discarded with this container).
set -euo pipefail
F=/fixtures S=/src W=/tmp/warm
mkdir -p "$F" "$W"
cp -a "$S/cart" "$F/cart"

echo "== next-app"
cd "$F" && npx --yes create-next-app@15.5.27 next-app --ts --eslint --tailwind --app --no-src-dir --import-alias "@/*" \
  --use-npm --skip-install --disable-git --no-turbopack --yes
cd next-app && node "$S/next-extra/gen-pages.mjs" . && npm install --package-lock-only
cp -a "$F/next-app" "$W/" && (cd "$W/next-app" && npm ci && npx next build >/dev/null && echo "next warm build ok")
(cd "$F/next-app" && pnpm import >/dev/null && cp -a pnpm-lock.yaml "$F/next-pnpm-lock.yaml")
cp -a "$F/next-app" "$W/next-pnpm" && (cd "$W/next-pnpm" && pnpm install --frozen-lockfile --store-dir /cache/pnpm-store >/dev/null && echo "pnpm store warm")

echo "== vite-app"
cd "$F" && npx --yes create-vite@6 vite-app --template react-ts
cd vite-app && cp -a "$S/vite-extra/." . && npm install --package-lock-only
npm install --package-lock-only -D vitest@3 jsdom@26 @testing-library/react@16 @testing-library/dom@10 @playwright/test@1
cp -a "$F/vite-app" "$W/" && (cd "$W/vite-app" && npm ci && npx playwright install chromium && npx vite build >/dev/null \
  && npx vitest run >/dev/null && npx playwright test && echo "vite warm ok")

echo "== express"
git clone --bare --depth 500 --single-branch https://github.com/expressjs/express "$F/express.git"
git clone -q "$F/express.git" "$W/express" && cd "$W/express" && npm install --package-lock-only \
  && cp package-lock.json "$F/express-package-lock.json" && npm ci && npm test >/dev/null && echo "express warm ok"

echo "== react (large repo)"
git clone --bare --depth 1 --single-branch https://github.com/facebook/react "$F/react.git"

echo "== python"
mkdir -p "$F/py" && cp -a "$S/py/." "$F/py/" && python3 -m venv "$W/venv" && "$W/venv/bin/pip" install -q -r "$F/py/requirements.in" \
  && "$W/venv/bin/pip" freeze > "$F/py/requirements.txt" && (cd "$F/py" && "$W/venv/bin/pytest" -q >/dev/null && echo "py warm ok")

echo "== versions"
{ echo "node $(node -v)"; echo "npm $(npm -v)"; echo "pnpm $(pnpm -v)"; python3 --version; git --version; rg --version | head -1
  (cd "$W/next-app" && node -p "'next '+require('next/package.json').version+' react '+require('react/package.json').version+' typescript '+require('typescript/package.json').version+' eslint '+require('eslint/package.json').version")
  (cd "$W/vite-app" && node -p "'vite '+require('vite/package.json').version+' vitest '+require('vitest/package.json').version+' playwright '+require('@playwright/test/package.json').version")
  (cd "$W/express" && echo "express $(git log -1 --format='%h %cs') $(node -p "require('./package.json').version")")
  echo "react.git $(git --git-dir=$F/react.git log -1 --format='%h %cs')"; cat "$F/py/requirements.txt"; } | tee "$F/VERSIONS.txt"
du -sh "$F"/* /cache/* 2>/dev/null
