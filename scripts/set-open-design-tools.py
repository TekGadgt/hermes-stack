#!/usr/bin/env python3
"""Set only the Hermes Open Design MCP tool filter via `config edit`."""

import os
import stat
import sys
import tempfile
from pathlib import Path

import yaml


EXCLUDED_TOOLS = [
    "start_run",
    "cancel_run",
    "delete_file",
    "delete_project",
]


def main() -> int:
    if len(sys.argv) != 2:
        print("Expected the Hermes config path from `hermes config edit`.", file=sys.stderr)
        return 2

    config_path = Path(sys.argv[1])
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        print("Hermes config root must be a mapping.", file=sys.stderr)
        return 1

    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict):
        print("Hermes mcp_servers must be a mapping.", file=sys.stderr)
        return 1
    server = servers.setdefault("open-design", {})
    if not isinstance(server, dict):
        print("Hermes open-design MCP entry must be a mapping.", file=sys.stderr)
        return 1
    tools = server.setdefault("tools", {})
    if not isinstance(tools, dict):
        print("Hermes open-design tools entry must be a mapping.", file=sys.stderr)
        return 1

    tools["exclude"] = EXCLUDED_TOOLS
    tools.pop("include", None)

    mode = stat.S_IMODE(config_path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, config_path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
