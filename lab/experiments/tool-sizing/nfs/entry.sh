#!/bin/sh
# Serve /export over NFSv4 only. EXPORT_OPTS defaults to sync (durable writes, like managed shared storage).
set -e
mountpoint -q /proc/fs/nfsd || mount -t nfsd nfsd /proc/fs/nfsd
echo "/export *(rw,${EXPORT_SYNC:-sync},no_subtree_check,no_root_squash,insecure,fsid=0)" > /etc/exports
exportfs -ra
# NFSv4 holds new opens for a grace period (default 90 s) after nfsd starts; keep it short so runs don't stall.
echo "${GRACE_S:-10}" > /proc/fs/nfsd/nfsv4leasetime
echo "${GRACE_S:-10}" > /proc/fs/nfsd/nfsv4gracetime
rpc.nfsd -N 3 ${NFSD_THREADS:-16}
rpc.mountd -N 3 -F &
[ -n "$NETEM_DELAY" ] && tc qdisc add dev eth0 root netem delay "$NETEM_DELAY"
echo "nfs ready"
trap 'rpc.nfsd 0; exportfs -ua; exit 0' TERM INT
while :; do sleep 3600 & wait $!; done
