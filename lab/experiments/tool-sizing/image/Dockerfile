# Sandbox image for tool-sizing: Debian trixie + Node 24.21 + the tools a coding agent shells out to.
FROM node:24.21.0-trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-venv python3-pip ripgrep build-essential procps ca-certificates curl iproute2 xz-utils \
    && rm -rf /var/lib/apt/lists/*
# pnpm, and the system libraries Chromium needs for Playwright (the browser itself lives in the shared cache volume)
RUN npm i -g pnpm@10 && npx -y playwright@1 install-deps chromium && rm -rf /var/lib/apt/lists/* /root/.npm
COPY step.py /usr/local/bin/step
RUN chmod +x /usr/local/bin/step && git config --system user.email lab@example.invalid && git config --system user.name lab \
    && git config --system init.defaultBranch main && git config --system advice.detachedHead false
ENV npm_config_cache=/cache/npm PIP_CACHE_DIR=/cache/pip PLAYWRIGHT_BROWSERS_PATH=/cache/ms-playwright \
    npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false NEXT_TELEMETRY_DISABLED=1 CI=1
WORKDIR /work
CMD ["sleep", "infinity"]
