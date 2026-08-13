"""Host-side CLI for the Hermes/Open Design Docker stack."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence


NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STACK_SERVICES = ("hermes", "tailscale", "tailscale-proxy")


class CliError(Exception):
    """An expected error that should be shown without a traceback."""


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.locations_file = state_dir / "locations.json"
        self.selection_file = state_dir / "current-projects.json"
        self.legacy_selection_file = state_dir / "current-projects"
        self.override_file = state_dir / "compose.projects.json"

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.state_dir / "open-design").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "tailscale-state").mkdir(parents=True, exist_ok=True)

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

        locations: "OrderedDict[str, str]" = OrderedDict()
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
            project_path = canonical_directory(raw_path)
            locations[name] = str(project_path)
            selection.append(name)

        self.save_locations(locations)
        self.save_selection(selection)

    def load_locations(self) -> "OrderedDict[str, str]":
        if not self.locations_file.exists():
            return OrderedDict()
        document = self._read_json(self.locations_file)
        if not isinstance(document, dict) or document.get("version") != 1:
            raise CliError(f"Unsupported location registry format: {self.locations_file}")
        raw_locations = document.get("locations")
        if not isinstance(raw_locations, dict):
            raise CliError(f"Invalid location registry: {self.locations_file}")

        locations: "OrderedDict[str, str]" = OrderedDict()
        for name, raw_path in raw_locations.items():
            if not isinstance(name, str) or not isinstance(raw_path, str):
                raise CliError(f"Invalid location entry in {self.locations_file}")
            validate_name(name)
            locations[name] = raw_path
        return locations

    def save_locations(self, locations: Mapping[str, str]) -> None:
        self._atomic_json(
            self.locations_file,
            {"version": 1, "locations": dict(locations)},
        )

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
                project_path = str(canonical_directory(raw_path))

                # A host directory has one stable container identity. Assigning
                # it a new name transfers the registration to that name.
                displaced = [
                    saved_name
                    for saved_name, saved_path in locations.items()
                    if saved_path == project_path and saved_name != name
                ]
                for saved_name in displaced:
                    del locations[saved_name]
                locations[name] = project_path
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

    def generate_override(self) -> None:
        locations = self.load_locations()
        selection = self.load_selection()
        volumes: List[str] = []
        for name in selection:
            raw_path = locations.get(name)
            if raw_path is None:
                raise CliError(
                    f"Selected location '{name}' is no longer registered. "
                    "Run start with a valid selection."
                )
            project_path = canonical_directory(raw_path)
            volumes.append(f"{project_path}:/workspace/{name}")
        self._atomic_json(
            self.override_file,
            {"services": {"hermes": {"volumes": volumes}}},
        )


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

    def start(self) -> None:
        self.prepare()
        self.compose("up", "-d", "tailscale")
        self.wait_for_remote_origin()
        self.compose(
            "up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "180",
            *STACK_SERVICES,
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
        self.compose("up", "-d", "--wait", "--wait-timeout", "180", *STACK_SERVICES)
        self.compose("up", "-d", "--force-recreate", "tailscale-proxy")
        self.compose("up", "-d", "--wait", "--wait-timeout", "180", "tailscale-proxy")
        self.configure_open_design()
        self.show_urls()


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
    logs.add_argument("target", choices=("hermes", "open-design", "tailscale", "all"))

    subparsers.add_parser("projects", help="show the current project selection")
    locations = subparsers.add_parser("locations", help="list or edit saved project locations")
    locations_subparsers = locations.add_subparsers(
        dest="locations_command", metavar="ACTION"
    )
    locations_subparsers.add_parser("list", help="list saved locations")
    add = locations_subparsers.add_parser("add", help="add or update a saved location")
    add.add_argument("name")
    add.add_argument("path")
    remove = locations_subparsers.add_parser("remove", help="remove a saved location")
    remove.add_argument("name")

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
    print_table(
        ("ACTIVE", "NAME", "HOST PATH", "CONTAINER PATH"),
        (
            ("●" if name in selection else "", name, path, f"/workspace/{name}")
            for name, path in locations.items()
        ),
    )


def list_projects(store: StateStore) -> None:
    locations = store.load_locations()
    selection = store.load_selection()
    if not selection:
        print("No project set configured.")
        return
    print_table(
        ("NAME", "HOST PATH", "CONTAINER PATH"),
        ((name, locations.get(name, "<missing>"), f"/workspace/{name}") for name in selection),
    )


def update_location(store: StateStore, name: str, raw_path: str) -> None:
    validate_name(name)
    project_path = str(canonical_directory(raw_path))
    locations = store.load_locations()
    selection = store.load_selection()
    displaced = [
        saved
        for saved, path in locations.items()
        if path == project_path and saved != name
    ]
    for saved in displaced:
        del locations[saved]
        selection = [name if selected == saved else selected for selected in selection]
    locations[name] = project_path
    store.save_locations(locations)
    store.save_selection(list(dict.fromkeys(selection)))
    if displaced:
        print(f"Moved {project_path} from '{displaced[0]}' to '{name}'.")
    else:
        print(f"Saved '{name}' → {project_path}")


def remove_location(store: StateStore, name: str) -> None:
    locations = store.load_locations()
    if name not in locations:
        raise CliError(f"Unknown location '{name}'.")
    del locations[name]
    selection = [selected for selected in store.load_selection() if selected != name]
    store.save_locations(locations)
    store.save_selection(selection)
    print(f"Removed saved location '{name}'.")
    print("The running container is unchanged; run restart to apply the updated selection.")


def run(arguments: Optional[Sequence[str]] = None) -> int:
    script_path = Path(__file__).resolve()
    stack_dir = script_path.parent
    command_name = Path(sys.argv[0]).name
    parser = build_parser(command_name)
    args = parser.parse_args(arguments)
    if args.command is None:
        parser.print_help()
        return 1

    state_dir = Path.home() / ".config" / "hermes-docker"
    store = StateStore(state_dir)
    store.ensure_directories()
    store.migrate_legacy_state()
    stack = HermesStack(stack_dir, store, command_name)

    if args.command == "start":
        if args.projects:
            store.resolve_selection(args.projects)
        stack.start()
    elif args.command == "stop":
        stack.prepare()
        stack.compose("stop", *STACK_SERVICES)
        print("Hermes stack stopped.")
    elif args.command == "restart":
        stack.prepare()
        stack.compose("stop", *STACK_SERVICES)
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
        elif args.target == "all":
            stack.compose("logs", "-f", *STACK_SERVICES)
        else:
            stack.compose("logs", "-f", "hermes")
    elif args.command == "projects":
        list_projects(store)
    elif args.command == "locations":
        if args.locations_command in (None, "list"):
            list_locations(store)
        elif args.locations_command == "add":
            update_location(store, args.name, args.path)
        elif args.locations_command == "remove":
            remove_location(store, args.name)
    elif args.command == "shell":
        stack.prepare()
        stack.compose("exec", "--user", "hermes", "hermes", "/usr/bin/fish", "--login")
    elif args.command == "update":
        stack.prepare()
        stack.compose("pull", "tailscale", "tailscale-proxy")
        stack.compose("build", "--pull", "hermes")
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
