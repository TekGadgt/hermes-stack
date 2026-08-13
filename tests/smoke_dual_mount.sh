#!/bin/sh
set -eu

image="${HERMES_STACK_SMOKE_IMAGE:-local/hermes-agent-dev:latest}"
probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/hermes-stack-dual-mount.XXXXXX")"
cleanup() {
    rm -f "$probe_dir/from-workspace" "$probe_dir/from-portable"
    rmdir "$probe_dir"
}
trap cleanup EXIT HUP INT TERM

docker run --rm \
    --entrypoint /bin/sh \
    --volume "$probe_dir:/workspace/dual-path-smoke" \
    --volume "$probe_dir:$probe_dir" \
    "$image" \
    -c '
        set -eu
        portable_path="$1"
        printf workspace > /workspace/dual-path-smoke/from-workspace
        test "$(cat "$portable_path/from-workspace")" = workspace
        printf portable > "$portable_path/from-portable"
        test "$(cat /workspace/dual-path-smoke/from-portable)" = portable
        test "$(stat -c "%d:%i" /workspace/dual-path-smoke/from-workspace)" = \
             "$(stat -c "%d:%i" "$portable_path/from-workspace")"
    ' sh "$probe_dir"

printf '%s\n' "Dual-path bind mount smoke test passed."
