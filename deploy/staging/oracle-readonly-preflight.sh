#!/bin/sh
# Run on the authorized staging VM. Redis credentials, if needed, are supplied
# by the operator via REDISCLI_AUTH and are never printed.
set -eu

printf '%s\n' '--- host resources ---'
uname -smr
printf 'Visible CPUs: '
getconf _NPROCESSORS_ONLN
awk '/^(MemTotal|SwapTotal):/ { print }' /proc/meminfo
df -hP "${STAGING_DATA_PATH:-/}"
printf 'Shell file descriptor limit: '
ulimit -n
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != aarch64 ]; then
    printf '%s\n' 'Expected Linux aarch64. Check OCI shape separately.' >&2
    exit 1
fi

printf '%s\n' '--- container limits ---'
for container in "${STAGING_API_CONTAINER:?set staging API container name}" \
    "${STAGING_REDIS_CONTAINER:?set staging Redis container name}"; do
    docker inspect --format '{{.Name}} image={{.Image}} cpu_nanocores={{.HostConfig.NanoCpus}} cpu_quota={{.HostConfig.CpuQuota}} cpu_period={{.HostConfig.CpuPeriod}} cpuset={{.HostConfig.CpusetCpus}} memory_bytes={{.HostConfig.Memory}}' "$container"
done
docker exec "$STAGING_API_CONTAINER" sh -c '
    for key in cpu.max memory.max memory.current; do
        if [ -r "/sys/fs/cgroup/$key" ]; then
            printf "%s=" "$key"
            cat "/sys/fs/cgroup/$key"
        fi
    done
    printf "Process file descriptor limit: "
    ulimit -n
'

printf '%s\n' '--- listening sockets ---'
ss -ltn | awk 'NR == 1 || $4 ~ /:(443|18080|16379)$/'

printf '%s\n' '--- Redis authenticated telemetry ---'
: "${REDISCLI_AUTH:?set the staging monitoring Redis password through the secret store}"
redis() {
    redis-cli -h 127.0.0.1 -p 16379 --user "${STAGING_REDIS_MONITOR_USER:?set the Redis monitoring ACL user}" -e "$@"
}
redis ping
redis info persistence | grep -E '^(aof_enabled|aof_last_write_status|aof_last_bgrewrite_status|loading):'
redis info memory | grep -E '^(used_memory|used_memory_peak|maxmemory|maxmemory_policy):'
redis info stats | grep -E '^(evicted_keys|rejected_connections|total_error_replies):'
env -u REDISCLI_AUTH redis-cli -h 127.0.0.1 -p 16379 ping | grep -q NOAUTH
printf '%s\n' 'Unauthenticated Redis PING rejected.'

printf '%s\n' '--- HTTP readiness over loopback ---'
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:18080/ready
printf '\n'
