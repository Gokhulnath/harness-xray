# Rivet Compute runs this container serverlessly (it sets RIVETKIT_RUNTIME_MODE=serverless at deploy time, so
# registry.start() serves HTTP on RIVET_PORT, default 3000). Debian slim: RivetKit ships glibc napi binaries.
FROM node:24.21.0-trixie-slim AS deps
WORKDIR /app
COPY package.json split-node-modules.cjs ./
# rivetkit 2.3.27 depends on agentOS (@rivet-dev/agent-os-core: ~1.4 GB of sidecars and bundled agent CLIs) and
# the local engine CLI. This worker uses E2B for tools and Rivet Cloud as the engine, so drop both.
RUN npm install --omit=dev --no-audit --no-fund \
 && rm -rf node_modules/@rivet-dev/agentos-* node_modules/@agentos-software node_modules/@secure-exec \
           node_modules/@rivetkit/engine-cli-* node_modules/pyodide \
 && node split-node-modules.cjs 8

FROM node:24.21.0-trixie-slim
WORKDIR /app
COPY --from=deps /app/package.json ./
COPY --from=deps /split/b0/ ./node_modules/
COPY --from=deps /split/b1/ ./node_modules/
COPY --from=deps /split/b2/ ./node_modules/
COPY --from=deps /split/b3/ ./node_modules/
COPY --from=deps /split/b4/ ./node_modules/
COPY --from=deps /split/b5/ ./node_modules/
COPY --from=deps /split/b6/ ./node_modules/
COPY --from=deps /split/b7/ ./node_modules/
COPY index.mjs workspace.mjs ./
USER node
EXPOSE 3000
CMD ["node", "index.mjs"]
