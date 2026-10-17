# E2B stand-in: an isolated box that only runs tools. No API keys, non-root user.
FROM node:24.21.0-trixie-slim
COPY --chown=node:node workspace /workspace
COPY sandboxd.mjs /opt/sandboxd.mjs
USER node
WORKDIR /workspace
EXPOSE 49983
CMD ["node", "/opt/sandboxd.mjs"]
