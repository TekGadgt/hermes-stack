#!/bin/sh
set -eu

tailscaled \
    --state=/var/lib/tailscale/tailscaled.state \
    --statedir=/var/lib/tailscale \
    --socket=/var/run/tailscale/tailscaled.sock \
    --tun=userspace-networking &
daemon_pid=$!

cleanup() {
    kill "$daemon_pid" 2>/dev/null || true
    wait "$daemon_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Keep the network namespace stable while the node is logged out. Once an
# interactive `tailscale up` succeeds, hand off to the official container
# bootstrap so TS_SERVE_CONFIG is applied and maintained by Tailscale.
while kill -0 "$daemon_pid" 2>/dev/null; do
    if tailscale status --json 2>/dev/null \
        | grep -q '"BackendState": "Running"'; then
        cleanup
        trap - INT TERM EXIT
        exec /usr/local/bin/containerboot
    fi
    sleep 2
done

wait "$daemon_pid"
