# Harness adapter: pi (npm @earendil-works/pi-coding-agent)
FROM lab-agent-base
ARG PI_VERSION=0.87.1
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@${PI_VERSION} && npm cache clean --force
COPY --chown=agent:agent home/ /home/agent/
