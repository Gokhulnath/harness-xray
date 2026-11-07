# NFSv4.2 server (kernel nfsd in the Docker VM) exporting /export, standing in for shared workspace storage.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends nfs-kernel-server iproute2 procps && rm -rf /var/lib/apt/lists/*
COPY entry.sh /entry.sh
RUN chmod +x /entry.sh
CMD ["/entry.sh"]
