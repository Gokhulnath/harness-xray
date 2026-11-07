# Claude Code on the tool-sizing sandbox image (same Node 24.21, git, ripgrep, Python as the tools container),
# plus the offload hooks, the remote-exec client, the exec server and a per-container sampler.
FROM tsz-sandbox
ARG CC_VERSION=2.1.303
RUN apt-get update && apt-get install -y --no-install-recommends jq && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@${CC_VERSION} && npm cache clean --force
COPY home/ /root/
COPY sampler.py execd.py hook.sh rexec settings-offload.json /opt/cco/
RUN chmod +x /opt/cco/hook.sh /opt/cco/rexec && claude --version
