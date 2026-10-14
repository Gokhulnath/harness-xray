# Harness adapter: Claude Code (npm @anthropic-ai/claude-code), pointed at the lab mock via ANTHROPIC_BASE_URL
FROM lab-agent-base
ARG CC_VERSION=2.1.299
RUN npm install -g @anthropic-ai/claude-code@${CC_VERSION} && npm cache clean --force
COPY --chown=agent:agent home/ /home/agent/
