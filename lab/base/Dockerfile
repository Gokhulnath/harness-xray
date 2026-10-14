# Agent-lab base image: every harness image builds FROM this.
# Debian bookworm + Node 24 (most harnesses are Node; others add their own runtime), the usual
# CLI tools an agent expects, a non-root `agent` user with a sudo policy, and a seeded workspace.
FROM node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates git ripgrep fd-find procps psmisc sysstat strace sudo \
      python3 python3-venv iproute2 curl jq tmux util-linux time less file xz-utils unzip \
 && rm -rf /var/lib/apt/lists/*

# Non-root agent user. A password exists so password-gated sudo can be tested.
RUN useradd -m -s /bin/bash -u 1001 agent && echo 'agent:labpass' | chpasswd
# Last match wins in sudoers: everything needs a password except a short allowlist.
RUN printf '%s\n' \
  'Defaults:agent logfile=/var/log/sudo.log, !lecture' \
  'agent ALL=(ALL) ALL' \
  'agent ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/id, /usr/bin/whoami, /usr/bin/ls, /usr/bin/du, /usr/bin/sha256sum, /usr/bin/dd, /usr/bin/cat /etc/hostname, /usr/bin/sleep' \
  > /etc/sudoers.d/agent && chmod 0440 /etc/sudoers.d/agent && visudo -c \
 && touch /var/log/sudo.log && chmod 0644 /var/log/sudo.log

COPY workspace-seed/ /opt/workspace-seed/
RUN mkdir -p /workspace && chown -R agent:agent /home/agent /workspace /opt/workspace-seed
WORKDIR /workspace
CMD ["sleep", "infinity"]
