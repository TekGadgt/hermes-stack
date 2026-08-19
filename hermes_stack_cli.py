"""Host-side CLI for the Hermes/Open Design Docker stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence


NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BASE_STACK_SERVICES = ("hermes", "tailscale", "tailscale-proxy")
OBSIDIAN_SERVICE = "obsidian-sync"
STATE_DIRECTORY_NAME = "hermes-stack"
LEGACY_STATE_DIRECTORY_NAME = "hermes-docker"
WORKSPACE_MANIFEST_CONTAINER_PATH = "/run/hermes-stack/workspaces.json"
WORKSPACE_RUNTIME_CONTAINER_DIRECTORY = "/run/hermes-stack"
NODE_MODES = ("auto", "on", "off")
OBSIDIAN_CONFIG_CATEGORIES = (
    "app,appearance,appearance-data,hotkey,core-plugin,core-plugin-data,"
    "community-plugin,community-plugin-data"
)
OBSIDIAN_FILE_TYPES = "image,audio,video,pdf,unsupported"
RESERVED_CONTAINER_PATHS = (
    Path("/command"),
    Path("/ms-playwright"),
    Path("/package"),
    Path("/workspace"),
    Path("/opt/data"),
    Path("/opt/hermes"),
    Path("/opt/open-design"),
    Path("/opt/open-design-data"),
    Path("/opt/open-design-node"),
    Path("/run/hermes-stack"),
)
SYSTEM_SENSITIVE_PATHS = (
    Path("/boot"),
    Path("/dev"),
    Path("/etc"),
    Path("/proc"),
    Path("/root"),
    Path("/run"),
    Path("/sys"),
    Path("/usr"),
    Path("/var/lib/docker"),
    Path("/var/run"),
    Path("/private/var/lib/docker"),
    Path("/private/var/run"),
)
HOME_SENSITIVE_RELATIVE_PATHS = (
    ".aws",
    ".azure",
    ".colima",
    ".config",
    ".docker",
    ".gnupg",
    ".hermes",
    ".kube",
    ".ssh",
)
WORKSPACE_SYSTEM_PROMPT = """Hermes Stack workspace contract:
- Read selected workspace mappings from /run/hermes-stack/workspaces.json.
- Each workspace_path and host_path pair names the same bind-mounted files, not two copies.
- Use /workspace/<name> for interactive work and dashboard file operations.
- Treat selected workspaces as independent unless the user's task explicitly connects them.
- Never dispatch duplicate work through both paths of one mapping.
- A node_project workspace uses one container-only Linux node_modules volume at both
  container paths. Never treat it as or overwrite the host's platform-specific dependencies.
- An obsidian_vault workspace exposes .obsidian read-only to Hermes; do not attempt to
  change plugins or synchronized Obsidian configuration.
- Prefer project-relative paths in code and automation. When an absolute path must persist
  outside this container, use the mapping's host_path; it also exists inside the container.
- Use the workspace-paths skill when creating scripts, scheduled jobs, configuration, or
  cross-workspace automation that records filesystem paths."""


@dataclass(frozen=True)
class WorkspaceMapping:
    name: str
    workspace_path: str
    host_path: str
    node_project: bool = False
    obsidian_vault: bool = False

    def as_document(self) -> Mapping[str, object]:
        return {
            "name": self.name,
            "workspace_path": self.workspace_path,
            "host_path": self.host_path,
            "node_project": self.node_project,
            "obsidian_vault": self.obsidian_vault,
        }


@dataclass(frozen=True)
class Location:
    path: str
    node: str = "auto"

    def as_document(self) -> Mapping[str, str]:
        return {"path": self.path, "node": self.node}

    def is_node_project(self) -> bool:
        if self.node == "on":
            return True
        if self.node == "off":
            return False
        return (Path(self.path) / "package.json").is_file()


class CliError(Exception):
    """An expected error that should be shown without a traceback."""


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.locations_file = state_dir / "locations.json"
        self.selection_file = state_dir / "current-projects.json"
        self.legacy_selection_file = state_dir / "current-projects"
        self.override_file = state_dir / "compose.projects.json"
        self.workspace_manifest_file = state_dir / "workspaces.json"
        self.obsidian_file = state_dir / "obsidian.json"
        self.obsidian_state_directory = state_dir / "obsidian-headless"
        self.runtime_directory = state_dir / "runtime"
        self.runtime_manifest_file = self.runtime_directory / "workspaces.json"

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.state_dir / "open-design").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "tailscale-state").mkdir(parents=True, exist_ok=True)
        self.obsidian_state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError as error:
            raise CliError(f"Invalid JSON in {path}: {error}") from error
        except OSError as error:
            raise CliError(f"Could not read {path}: {error}") from error

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent), text=True
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(path))
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def migrate_legacy_state(self) -> None:
        if self.locations_file.exists() or not self.legacy_selection_file.exists():
            return

        locations: "OrderedDict[str, Location]" = OrderedDict()
        selection: List[str] = []
        try:
            lines = self.legacy_selection_file.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise CliError(f"Could not read {self.legacy_selection_file}: {error}") from error

        for line in lines:
            if not line:
                continue
            if "=" not in line:
                raise CliError(f"Invalid legacy project entry: {line}")
            name, raw_path = line.split("=", 1)
            validate_name(name)
            project_path = workspace_directory(raw_path)
            locations[name] = Location(str(project_path))
            selection.append(name)

        self.save_locations(locations)
        self.save_selection(selection)

    def load_locations(self) -> "OrderedDict[str, Location]":
        if not self.locations_file.exists():
            return OrderedDict()
        document = self._read_json(self.locations_file)
        if not isinstance(document, dict) or document.get("version") not in (1, 2):
            raise CliError(f"Unsupported location registry format: {self.locations_file}")
        raw_locations = document.get("locations")
        if not isinstance(raw_locations, dict):
            raise CliError(f"Invalid location registry: {self.locations_file}")

        locations: "OrderedDict[str, Location]" = OrderedDict()
        version = document["version"]
        for name, raw_entry in raw_locations.items():
            if not isinstance(name, str):
                raise CliError(f"Invalid location entry in {self.locations_file}")
            validate_name(name)
            if version == 1 and isinstance(raw_entry, str):
                locations[name] = Location(raw_entry)
                continue
            if not isinstance(raw_entry, dict):
                raise CliError(f"Invalid location entry in {self.locations_file}")
            raw_path = raw_entry.get("path")
            node_mode = raw_entry.get("node", "auto")
            if not isinstance(raw_path, str) or node_mode not in NODE_MODES:
                raise CliError(f"Invalid location entry in {self.locations_file}")
            locations[name] = Location(raw_path, node_mode)
        return locations

    def save_locations(self, locations: Mapping[str, Location]) -> None:
        self._atomic_json(
            self.locations_file,
            {
                "version": 2,
                "locations": {
                    name: location.as_document()
                    for name, location in locations.items()
                },
            },
        )

    def migrate_location_registry(self) -> None:
        if not self.locations_file.exists():
            return
        document = self._read_json(self.locations_file)
        if isinstance(document, dict) and document.get("version") == 1:
            self.save_locations(self.load_locations())

    def load_obsidian_location(self) -> Optional[str]:
        if not self.obsidian_file.exists():
            return None
        document = self._read_json(self.obsidian_file)
        if not isinstance(document, dict) or document.get("version") != 1:
            raise CliError(f"Invalid Obsidian configuration: {self.obsidian_file}")
        location = document.get("location")
        if not isinstance(location, str):
            raise CliError(f"Invalid Obsidian configuration: {self.obsidian_file}")
        validate_name(location)
        return location

    def save_obsidian_location(self, name: str) -> None:
        self._atomic_json(self.obsidian_file, {"version": 1, "location": name})

    def disable_obsidian(self) -> None:
        try:
            self.obsidian_file.unlink()
        except FileNotFoundError:
            pass

    def load_selection(self) -> List[str]:
        if not self.selection_file.exists():
            return []
        document = self._read_json(self.selection_file)
        if not isinstance(document, dict) or document.get("version") != 1:
            raise CliError(f"Unsupported project selection format: {self.selection_file}")
        selection = document.get("projects")
        if not isinstance(selection, list) or not all(
            isinstance(item, str) for item in selection
        ):
            raise CliError(f"Invalid project selection: {self.selection_file}")
        for name in selection:
            validate_name(name)
        return selection

    def save_selection(self, selection: Sequence[str]) -> None:
        self._atomic_json(
            self.selection_file,
            {"version": 1, "projects": list(selection)},
        )

    def resolve_selection(self, entries: Sequence[str]) -> List[str]:
        locations = self.load_locations()
        selection: List[str] = []

        for entry in entries:
            if "=" in entry:
                name, raw_path = entry.split("=", 1)
                validate_name(name)
                project_path = str(workspace_directory(raw_path))

                # A host directory has one stable workspace alias. Assigning it
                # a new name transfers the registration to that name.
                displaced = [
                    saved_name
                    for saved_name, saved_location in locations.items()
                    if saved_location.path == project_path and saved_name != name
                ]
                inherited = locations.get(displaced[0]) if displaced else locations.get(name)
                for saved_name in displaced:
                    del locations[saved_name]
                locations[name] = Location(
                    project_path,
                    inherited.node if inherited is not None else "auto",
                )
                obsidian_location = self.load_obsidian_location()
                if displaced and obsidian_location == displaced[0]:
                    self.save_obsidian_location(name)
            else:
                name = entry
                validate_name(name)
                if name not in locations:
                    raise CliError(
                        f"Unknown location '{name}'. Register it with "
                        f"'{Path(sys.argv[0]).name} start {name}=/path'."
                    )

            if name in selection:
                raise CliError(f"Duplicate project name: {name}")
            selection.append(name)

        self.save_locations(locations)
        self.save_selection(selection)
        return selection

    def selected_workspaces(self) -> List[WorkspaceMapping]:
        locations = self.load_locations()
        selection = self.load_selection()
        workspaces: List[WorkspaceMapping] = []
        targets = set()
        for name in selection:
            location = locations.get(name)
            if location is None:
                raise CliError(
                    f"Selected location '{name}' is no longer registered. "
                    "Run start with a valid selection."
                )
            host_path = str(workspace_directory(location.path))
            workspace_path = f"/workspace/{name}"
            for target in (workspace_path, host_path):
                if target in targets:
                    raise CliError(f"Workspace mount target is not unique: {target}")
                targets.add(target)
            workspaces.append(
                WorkspaceMapping(
                    name=name,
                    workspace_path=workspace_path,
                    host_path=host_path,
                    node_project=location.is_node_project(),
                    obsidian_vault=name == self.load_obsidian_location(),
                )
            )
        return workspaces

    def generate_override(self) -> None:
        workspaces = self.selected_workspaces()
        manifest = {
            "version": 1,
            "workspaces": [workspace.as_document() for workspace in workspaces],
        }
        self._atomic_json(
            self.workspace_manifest_file,
            manifest,
        )
        self.runtime_directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.runtime_directory.chmod(0o755)
        self._atomic_json(self.runtime_manifest_file, manifest)
        # The parent state directory remains private (0700). These filtered
        # copies must be readable by unprivileged container processes; only the
        # dedicated runtime directory is bind-mounted into the container.
        self.workspace_manifest_file.chmod(0o644)
        self.runtime_manifest_file.chmod(0o644)

        volumes = []
        named_volumes = {}
        for workspace in workspaces:
            for target in (workspace.workspace_path, workspace.host_path):
                volumes.append(
                    {
                        "type": "bind",
                        "source": workspace.host_path,
                        "target": target,
                    }
                )
            if workspace.node_project:
                volume_key, volume_name = node_modules_volume(workspace.host_path)
                named_volumes[volume_key] = {"name": volume_name}
                for target in (workspace.workspace_path, workspace.host_path):
                    volumes.append(
                        {
                            "type": "volume",
                            "source": volume_key,
                            "target": f"{target}/node_modules",
                            "volume": {"nocopy": True},
                        }
                    )
            if workspace.obsidian_vault:
                obsidian_source = str(Path(workspace.host_path) / ".obsidian")
                for target in (workspace.workspace_path, workspace.host_path):
                    volumes.append(
                        {
                            "type": "bind",
                            "source": obsidian_source,
                            "target": f"{target}/.obsidian",
                            "read_only": True,
                        }
                    )
        volumes.append(
            {
                "type": "bind",
                "source": str(self.runtime_directory.resolve(strict=True)),
                "target": WORKSPACE_RUNTIME_CONTAINER_DIRECTORY,
                "read_only": True,
            }
        )

        safe_roots = ["/workspace", "/opt/data"]
        safe_roots.extend(workspace.host_path for workspace in workspaces)
        services = {
            "hermes": {
                "volumes": volumes,
                "environment": {
                    "HERMES_EPHEMERAL_SYSTEM_PROMPT": WORKSPACE_SYSTEM_PROMPT,
                    "HERMES_STACK_WORKSPACE_MANIFEST": (
                        WORKSPACE_MANIFEST_CONTAINER_PATH
                    ),
                    "HERMES_WRITE_SAFE_ROOT": ":".join(safe_roots),
                },
            }
        }
        obsidian_location = self.load_obsidian_location()
        if obsidian_location is not None:
            location = self.load_locations().get(obsidian_location)
            if location is None:
                raise CliError(
                    f"Configured Obsidian location '{obsidian_location}' is missing."
                )
            vault_path = str(workspace_directory(location.path))
            obsidian_config = Path(vault_path) / ".obsidian"
            obsidian_config.mkdir(mode=0o755, exist_ok=True)
            services[OBSIDIAN_SERVICE] = {
                "user": f"{os.getuid()}:{os.getgid()}",
                "volumes": [
                    {
                        "type": "bind",
                        "source": vault_path,
                        "target": "/vault",
                    },
                    {
                        "type": "bind",
                        "source": str(self.obsidian_state_directory.resolve()),
                        "target": "/state/obsidian-headless",
                    },
                ],
            }
        override = {"services": services}
        if named_volumes:
            override["volumes"] = named_volumes
        self._atomic_json(self.override_file, override)


def validate_name(name: str) -> None:
    if not NAME_PATTERN.fullmatch(name):
        raise CliError(
            f"Invalid project name '{name}'. Use letters, numbers, dots, "
            "underscores, or hyphens."
        )


def canonical_directory(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise CliError(f"Project directory does not exist: {path}") from error
    if not resolved.is_dir():
        raise CliError(f"Project path is not a directory: {resolved}")
    return resolved


def path_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def paths_overlap(first: Path, second: Path) -> bool:
    return path_contains(first, second) or path_contains(second, first)


def validate_workspace_directory(path: Path, home: Optional[Path] = None) -> None:
    resolved_home = (home or Path.home()).expanduser().resolve()

    if path.parent == path:
        raise CliError("Refusing to expose the filesystem root as a workspace.")
    if any(ord(character) < 32 for character in str(path)):
        raise CliError(f"Workspace path contains control characters: {path}")
    if os.pathsep in str(path):
        raise CliError(
            f"Workspace path contains '{os.pathsep}', which cannot be represented "
            "safely in HERMES_WRITE_SAFE_ROOT."
        )
    if path == resolved_home or path_contains(path, resolved_home):
        raise CliError(
            f"Refusing to expose the home directory or one of its parents: {path}"
        )

    sensitive_paths = [
        resolved_home / relative for relative in HOME_SENSITIVE_RELATIVE_PATHS
    ]
    sensitive_paths.extend(SYSTEM_SENSITIVE_PATHS)
    for sensitive in sensitive_paths:
        if paths_overlap(path, sensitive):
            raise CliError(
                f"Refusing workspace path {path}: it overlaps sensitive path {sensitive}."
            )

    for reserved in RESERVED_CONTAINER_PATHS:
        if paths_overlap(path, reserved):
            raise CliError(
                f"Refusing workspace path {path}: its exact-path mount would overlap "
                f"reserved container path {reserved}."
            )


def workspace_directory(raw_path: str, home: Optional[Path] = None) -> Path:
    path = canonical_directory(raw_path)
    validate_workspace_directory(path, home=home)
    return path


def node_modules_volume(host_path: str) -> tuple[str, str]:
    canonical_path = str(Path(host_path).expanduser().resolve(strict=True))
    digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:16]
    return (
        f"node_modules_{digest}",
        f"hermes-stack-node-modules-{digest}",
    )


def resolve_state_directory(home: Path, allow_legacy: bool = False) -> Path:
    config_dir = home / ".config"
    state_dir = config_dir / STATE_DIRECTORY_NAME
    legacy_state_dir = config_dir / LEGACY_STATE_DIRECTORY_NAME
    if legacy_state_dir.exists() and not state_dir.exists():
        if allow_legacy:
            print(
                f"Using legacy state at {legacy_state_dir} to stop the stack.\n"
                f"Move it before the next command:\n  "
                f"mv {legacy_state_dir} {state_dir}",
                file=sys.stderr,
            )
            return legacy_state_dir
        raise CliError(
            f"Legacy state directory found at {legacy_state_dir}. "
            "Stop the running stack first if necessary, then move it with:\n  "
            f"mv {legacy_state_dir} {state_dir}"
        )
    return state_dir


def print_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> None:
    materialized = [tuple(row) for row in rows]
    widths = [len(header) for header in headers]
    for row in materialized:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("─" * width for width in widths))
    for row in materialized:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


class HermesStack:
    def __init__(self, stack_dir: Path, store: StateStore, command_name: str) -> None:
        self.stack_dir = stack_dir
        self.store = store
        self.command_name = command_name
        self.compose_file = stack_dir / "compose.yaml"
        self.env_file = stack_dir / ".env"

    def compose_command(self, arguments: Sequence[str]) -> List[str]:
        command = [
            "docker",
            "compose",
            "--project-directory",
            str(self.stack_dir),
            "-f",
            str(self.compose_file),
            "-f",
            str(self.store.override_file),
        ]
        if self.env_file.is_file():
            command.extend(("--env-file", str(self.env_file)))
        command.extend(arguments)
        return command

    def compose(
        self,
        *arguments: str,
        capture: bool = False,
        quiet: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                self.compose_command(arguments),
                cwd=str(self.stack_dir),
                check=check,
                text=True,
                stdout=(
                    subprocess.PIPE
                    if capture
                    else subprocess.DEVNULL
                    if quiet
                    else None
                ),
                stderr=subprocess.DEVNULL if capture else None,
            )
        except FileNotFoundError as error:
            raise CliError("Docker is not installed or is not available on PATH.") from error
        except subprocess.CalledProcessError as error:
            raise CliError(f"Command failed with exit code {error.returncode}.") from error

    def tailscale_fqdn(self) -> str:
        result = self.compose(
            "exec", "-T", "tailscale", "tailscale", "status", "--json",
            capture=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return ""
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ""
        return str(document.get("Self", {}).get("DNSName", "")).rstrip(".")

    def tailscale_backend_state(self) -> str:
        result = self.compose(
            "exec", "-T", "tailscale", "tailscale", "status", "--json",
            capture=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return ""
        try:
            return str(json.loads(result.stdout).get("BackendState", ""))
        except json.JSONDecodeError:
            return ""

    def configure_remote_origin(self) -> bool:
        fqdn = self.tailscale_fqdn()
        if fqdn:
            os.environ["OPEN_DESIGN_ALLOWED_ORIGINS"] = f"https://{fqdn}"
            return True
        os.environ.pop("OPEN_DESIGN_ALLOWED_ORIGINS", None)
        return False

    def wait_for_remote_origin(self) -> bool:
        for _ in range(30):
            if self.configure_remote_origin():
                return True
            if self.tailscale_backend_state() in ("NeedsLogin", "Stopped"):
                return False
            time.sleep(1)
        return False

    def show_urls(self) -> bool:
        print("Hermes:      http://127.0.0.1:9119")
        print("Open Design: http://127.0.0.1:7456")
        fqdn = self.tailscale_fqdn()
        if not fqdn:
            print("Remote URLs unavailable: Tailscale is not authenticated.")
            print(f"Run '{self.command_name} tailscale-login' to authorize this stack.")
            return False
        print(f"Hermes:      https://{fqdn}:9119")
        print(f"Open Design: https://{fqdn}")
        print(f"Hermes OAuth callback: https://{fqdn}:9119/auth/callback")
        return True

    def configure_open_design(self) -> None:
        base = (
            "exec", "-T", "--user", "hermes", "hermes",
            "/opt/open-design-node/bin/node",
            "/opt/open-design/apps/daemon/dist/cli.js",
            "config", "set",
        )
        self.compose(
            *base, "onboardingCompleted", "true",
            "--daemon-url", "http://127.0.0.1:7456",
            quiet=True,
        )
        self.compose(
            *base, "agentId", "hermes",
            "--daemon-url", "http://127.0.0.1:7456",
            quiet=True,
        )

    def prepare(self) -> None:
        self.store.generate_override()

    def start_services(self) -> tuple[str, ...]:
        if self.store.load_obsidian_location() is not None:
            return (*BASE_STACK_SERVICES, OBSIDIAN_SERVICE)
        return BASE_STACK_SERVICES

    def start(self) -> None:
        self.prepare()
        self.compose("up", "-d", "tailscale")
        self.wait_for_remote_origin()
        self.compose(
            "up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "180",
            *self.start_services(),
        )
        # Caddy does not automatically reload a changed bind-mounted Caddyfile.
        self.compose("up", "-d", "--force-recreate", "tailscale-proxy")
        self.compose("up", "-d", "--wait", "--wait-timeout", "180", "tailscale-proxy")
        self.configure_open_design()
        print("Hermes stack is ready.")
        self.show_urls()

    def tailscale_login(self) -> None:
        self.prepare()
        self.compose("up", "-d", "tailscale")
        self.compose("exec", "tailscale", "tailscale", "up")
        if not self.wait_for_remote_origin():
            raise CliError(
                "Tailscale authorized, but its tailnet DNS name is not available yet."
            )
        self.compose("up", "-d", "--force-recreate", "hermes")
        self.compose(
            "up", "-d", "--wait", "--wait-timeout", "180",
            *self.start_services(),
        )
        self.compose("up", "-d", "--force-recreate", "tailscale-proxy")
        self.compose("up", "-d", "--wait", "--wait-timeout", "180", "tailscale-proxy")
        self.configure_open_design()
        self.show_urls()

    def obsidian_run(self, *arguments: str) -> None:
        self.prepare()
        self.compose("run", "--rm", "--no-deps", OBSIDIAN_SERVICE, "ob", *arguments)

    def obsidian_status(self) -> None:
        self.prepare()
        running = self.compose(
            "ps", "-q", "--status", "running", OBSIDIAN_SERVICE,
            capture=True,
            check=False,
        )
        if running.returncode == 0 and running.stdout.strip():
            self.compose(
                "exec", "-T", OBSIDIAN_SERVICE,
                "ob", "sync-status", "--path", "/vault",
            )
        else:
            self.obsidian_run("sync-status", "--path", "/vault")

    def reset_node_modules(self, location: Location) -> None:
        _, volume_name = node_modules_volume(location.path)
        in_use = self.compose(
            "ps", "-q", capture=True, check=False,
        )
        if in_use.returncode == 0 and in_use.stdout.strip():
            result = subprocess.run(
                ["docker", "ps", "-q", "--filter", f"volume={volume_name}"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
            )
            if result.stdout.strip():
                raise CliError(
                    f"Node dependency volume is in use. Stop the stack before resetting it."
                )
        result = subprocess.run(
            ["docker", "volume", "rm", volume_name],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0 and "no such volume" not in result.stderr.lower():
            raise CliError(result.stderr.strip() or "Could not remove dependency volume.")
        print(f"Reset container node_modules for {location.path}.")


def build_parser(command_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=command_name,
        description="Manage the local Hermes and Open Design stack.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    start = subparsers.add_parser("start", help="start the stack with saved project locations")
    start.add_argument(
        "projects", nargs="*", metavar="NAME[=PATH]",
        help="saved name, or name=/path to register or update a location",
    )
    subparsers.add_parser("stop", help="stop all stack services")
    subparsers.add_parser("restart", help="restart with the last project selection")
    subparsers.add_parser("status", help="show service status")

    logs = subparsers.add_parser("logs", help="follow service logs")
    logs.add_argument(
        "target", choices=("hermes", "open-design", "obsidian", "tailscale", "all")
    )

    subparsers.add_parser("projects", help="show the current project selection")
    locations = subparsers.add_parser("locations", help="list or edit saved project locations")
    locations_subparsers = locations.add_subparsers(
        dest="locations_command", metavar="ACTION"
    )
    locations_subparsers.add_parser("list", help="list saved locations")
    add = locations_subparsers.add_parser("add", help="add or update a saved location")
    add.add_argument("name")
    add.add_argument("path")
    add.add_argument("--node", choices=NODE_MODES)
    location_set = locations_subparsers.add_parser(
        "set", help="change saved location options"
    )
    location_set.add_argument("name")
    location_set.add_argument("--node", choices=NODE_MODES, required=True)
    reset_node = locations_subparsers.add_parser(
        "reset-node-modules", help="remove a location's Linux dependency volume"
    )
    reset_node.add_argument("name")
    remove = locations_subparsers.add_parser("remove", help="remove a saved location")
    remove.add_argument("name")

    obsidian = subparsers.add_parser(
        "obsidian", help="configure the optional Obsidian Headless mirror"
    )
    obsidian_subparsers = obsidian.add_subparsers(
        dest="obsidian_command", metavar="ACTION", required=True
    )
    obsidian_configure = obsidian_subparsers.add_parser(
        "configure", help="use a saved location as the headless vault mirror"
    )
    obsidian_configure.add_argument("location")
    obsidian_subparsers.add_parser("login", help="log in interactively")
    obsidian_setup = obsidian_subparsers.add_parser(
        "setup", help="connect the mirror to a remote vault"
    )
    obsidian_setup.add_argument("--vault", required=True)
    obsidian_setup.add_argument("--device-name", default="hermes-stack")
    obsidian_subparsers.add_parser("status", help="show headless sync status")
    obsidian_subparsers.add_parser("disable", help="disable automatic headless sync")

    subparsers.add_parser("shell", help="open Fish as the unprivileged Hermes user")
    subparsers.add_parser("update", help="pull, rebuild, and restart the stack")
    subparsers.add_parser("tailscale-login", help="authorize the persistent Tailscale node")
    subparsers.add_parser("tailscale-status", help="show Tailscale status")
    subparsers.add_parser("tailscale-urls", help="show local and tailnet URLs")
    return parser


def list_locations(store: StateStore) -> None:
    locations = store.load_locations()
    if not locations:
        print("No saved locations.")
        return
    selection = set(store.load_selection())
    obsidian_location = store.load_obsidian_location()
    print_table(
        ("ACTIVE", "OBSIDIAN", "NAME", "NODE", "WORKSPACE PATH", "PORTABLE PATH"),
        (
            (
                "●" if name in selection else "",
                "●" if name == obsidian_location else "",
                name,
                f"{location.node} ({'yes' if location.is_node_project() else 'no'})",
                f"/workspace/{name}",
                location.path,
            )
            for name, location in locations.items()
        ),
    )


def list_projects(store: StateStore) -> None:
    locations = store.load_locations()
    selection = store.load_selection()
    if not selection:
        print("No project set configured.")
        return
    print_table(
        ("NAME", "NODE", "WORKSPACE PATH", "PORTABLE PATH"),
        (
            (
                name,
                (
                    "<missing>"
                    if locations.get(name) is None
                    else "yes" if locations[name].is_node_project() else "no"
                ),
                f"/workspace/{name}",
                locations[name].path if name in locations else "<missing>",
            )
            for name in selection
        ),
    )


def update_location(
    store: StateStore, name: str, raw_path: str, node_mode: Optional[str] = None
) -> None:
    validate_name(name)
    project_path = str(workspace_directory(raw_path))
    locations = store.load_locations()
    selection = store.load_selection()
    displaced = [
        saved
        for saved, location in locations.items()
        if location.path == project_path and saved != name
    ]
    inherited = locations.get(displaced[0]) if displaced else locations.get(name)
    for saved in displaced:
        del locations[saved]
        selection = [name if selected == saved else selected for selected in selection]
    locations[name] = Location(
        project_path,
        node_mode or (inherited.node if inherited is not None else "auto"),
    )
    obsidian_location = store.load_obsidian_location()
    if displaced and obsidian_location == displaced[0]:
        store.save_obsidian_location(name)
    store.save_locations(locations)
    store.save_selection(list(dict.fromkeys(selection)))
    if displaced:
        print(f"Moved {project_path} from '{displaced[0]}' to '{name}'.")
    else:
        print(f"Saved '{name}' → {project_path}")


def set_location_node_mode(store: StateStore, name: str, node_mode: str) -> None:
    locations = store.load_locations()
    location = locations.get(name)
    if location is None:
        raise CliError(f"Unknown location '{name}'.")
    locations[name] = Location(location.path, node_mode)
    store.save_locations(locations)
    print(f"Set '{name}' node mode to {node_mode}.")


def remove_location(store: StateStore, name: str) -> None:
    locations = store.load_locations()
    if name not in locations:
        raise CliError(f"Unknown location '{name}'.")
    if store.load_obsidian_location() == name:
        raise CliError(
            f"Location '{name}' is the Obsidian mirror. Run 'obsidian disable' first."
        )
    del locations[name]
    selection = [selected for selected in store.load_selection() if selected != name]
    store.save_locations(locations)
    store.save_selection(selection)
    print(f"Removed saved location '{name}'.")
    print("The running container is unchanged; run restart to apply the updated selection.")


def configure_obsidian(store: StateStore, name: str) -> None:
    location = store.load_locations().get(name)
    if location is None:
        raise CliError(f"Unknown location '{name}'. Register the mirror first.")
    vault_path = workspace_directory(location.path)
    (vault_path / ".obsidian").mkdir(mode=0o755, exist_ok=True)
    store.save_obsidian_location(name)
    print(f"Configured '{name}' as the Obsidian Headless mirror.")
    print("The running stack is unchanged; run obsidian login and obsidian setup next.")


def run(arguments: Optional[Sequence[str]] = None) -> int:
    script_path = Path(__file__).resolve()
    stack_dir = script_path.parent
    command_name = Path(sys.argv[0]).name
    parser = build_parser(command_name)
    args = parser.parse_args(arguments)
    if args.command is None:
        parser.print_help()
        return 1

    state_dir = resolve_state_directory(
        Path.home(), allow_legacy=args.command == "stop"
    )
    store = StateStore(state_dir)
    store.ensure_directories()
    store.migrate_legacy_state()
    store.migrate_location_registry()
    stack = HermesStack(stack_dir, store, command_name)

    if args.command == "start":
        if args.projects:
            store.resolve_selection(args.projects)
        stack.start()
    elif args.command == "stop":
        stack.prepare()
        stack.compose("stop", *BASE_STACK_SERVICES, OBSIDIAN_SERVICE)
        print("Hermes stack stopped.")
    elif args.command == "restart":
        stack.prepare()
        stack.compose("stop", *BASE_STACK_SERVICES, OBSIDIAN_SERVICE)
        stack.start()
    elif args.command == "status":
        stack.prepare()
        stack.compose("ps", "--all")
    elif args.command == "logs":
        stack.prepare()
        if args.target == "open-design":
            print("Open Design is supervised inside the Hermes container; logs are combined.")
            stack.compose("logs", "-f", "hermes")
        elif args.target == "tailscale":
            stack.compose("logs", "-f", "tailscale", "tailscale-proxy")
        elif args.target == "obsidian":
            stack.compose("logs", "-f", OBSIDIAN_SERVICE)
        elif args.target == "all":
            stack.compose("logs", "-f", *stack.start_services())
        else:
            stack.compose("logs", "-f", "hermes")
    elif args.command == "projects":
        list_projects(store)
    elif args.command == "locations":
        if args.locations_command in (None, "list"):
            list_locations(store)
        elif args.locations_command == "add":
            update_location(store, args.name, args.path, args.node)
        elif args.locations_command == "set":
            set_location_node_mode(store, args.name, args.node)
        elif args.locations_command == "reset-node-modules":
            location = store.load_locations().get(args.name)
            if location is None:
                raise CliError(f"Unknown location '{args.name}'.")
            stack.prepare()
            stack.reset_node_modules(location)
        elif args.locations_command == "remove":
            remove_location(store, args.name)
    elif args.command == "obsidian":
        if args.obsidian_command == "configure":
            configure_obsidian(store, args.location)
        elif args.obsidian_command == "login":
            if store.load_obsidian_location() is None:
                raise CliError("Configure an Obsidian mirror location first.")
            stack.obsidian_run("login")
        elif args.obsidian_command == "setup":
            if store.load_obsidian_location() is None:
                raise CliError("Configure an Obsidian mirror location first.")
            stack.obsidian_run(
                "sync-setup", "--vault", args.vault,
                "--path", "/vault",
                "--device-name", args.device_name,
                "--config-dir", ".obsidian",
            )
            stack.obsidian_run(
                "sync-config", "--path", "/vault",
                "--mode", "bidirectional",
                "--conflict-strategy", "conflict",
                "--file-types", OBSIDIAN_FILE_TYPES,
                "--configs", OBSIDIAN_CONFIG_CATEGORIES,
                "--device-name", args.device_name,
                "--config-dir", ".obsidian",
            )
            print("Obsidian mirror configured. Restart the stack when ready to sync continuously.")
        elif args.obsidian_command == "status":
            if store.load_obsidian_location() is None:
                raise CliError("Obsidian Headless is not configured.")
            stack.obsidian_status()
        elif args.obsidian_command == "disable":
            store.disable_obsidian()
            print("Obsidian Headless disabled. The running stack is unchanged.")
    elif args.command == "shell":
        stack.prepare()
        stack.compose("exec", "--user", "hermes", "hermes", "/usr/bin/fish", "--login")
    elif args.command == "update":
        stack.prepare()
        stack.compose("pull", "tailscale", "tailscale-proxy")
        stack.compose("build", "--pull", "hermes", OBSIDIAN_SERVICE)
        stack.start()
        print("Hermes stack updated.")
    elif args.command == "tailscale-login":
        stack.tailscale_login()
    elif args.command == "tailscale-status":
        stack.prepare()
        stack.compose("exec", "tailscale", "tailscale", "status")
    elif args.command == "tailscale-urls":
        stack.prepare()
        return 0 if stack.show_urls() else 1
    return 0


def main() -> int:
    try:
        return run()
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
