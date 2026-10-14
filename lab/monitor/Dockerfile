# Monitor: /proc + cgroup + network + VM sampler, eBPF tracer (bpftrace), live dashboard, explorer server.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 bpftrace procps linux-libc-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /lab/monitor
COPY sampler.py labclass.py dashboard.html proctrace.bt ./
CMD ["python3", "-u", "sampler.py"]
