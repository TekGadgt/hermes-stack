import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hermes_stack_cli import (
    CliError,
    DEV_SERVER_PORT,
    Location,
    StateStore,
    VSCODE_PORT,
    WORKSPACE_MANIFEST_CONTAINER_PATH,
    WORKSPACE_RUNTIME_CONTAINER_DIRECTORY,
    WORKSPACE_SYSTEM_PROMPT,
    build_parser,
    completion_location_names,
    configure_obsidian,
    node_modules_volume,
    remove_location,
    render_completions,
    resolve_state_directory,
    run,
    tailscale_serve_document,
    update_location,
    validate_workspace_directory,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.store = StateStore(self.state)
        self.store.ensure_directories()
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_explicit_entries_register_and_bare_names_resolve(self):
        selection = self.store.resolve_selection(
            [f"alpha={self.alpha}", f"beta={self.beta}"]
        )
        self.assertEqual(selection, ["alpha", "beta"])
        self.assertEqual(
            self.store.resolve_selection(["beta", "alpha"]),
            ["beta", "alpha"],
        )
        self.assertEqual(self.store.load_selection(), ["beta", "alpha"])

    def test_new_name_transfers_an_existing_path(self):
        self.store.resolve_selection([f"old-name={self.alpha}"])
        self.store.resolve_selection([f"new-name={self.alpha}"])
        self.assertEqual(
            self.store.load_locations(),
            {"new-name": Location(str(self.alpha.resolve()))},
        )
        self.assertEqual(self.store.load_selection(), ["new-name"])

    def test_reassigning_name_overwrites_its_path(self):
        self.store.resolve_selection([f"app={self.alpha}"])
        self.store.resolve_selection([f"app={self.beta}"])
        self.assertEqual(
            self.store.load_locations()["app"], Location(str(self.beta.resolve()))
        )

    def test_unknown_bare_name_does_not_change_state(self):
        self.store.resolve_selection([f"alpha={self.alpha}"])
        with self.assertRaises(CliError):
            self.store.resolve_selection(["missing"])
        self.assertEqual(self.store.load_selection(), ["alpha"])

    def test_additive_selection_preserves_order_and_adds_registered_locations(self):
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        self.store.resolve_selection(["alpha"])
        self.assertEqual(
            self.store.resolve_selection(["beta", "alpha"], add=True),
            ["alpha", "beta"],
        )

    def test_additive_selection_registers_new_location(self):
        gamma = self.root / "gamma"
        gamma.mkdir()
        self.store.resolve_selection([f"alpha={self.alpha}"])
        self.assertEqual(
            self.store.resolve_selection([f"gamma={gamma}"], add=True),
            ["alpha", "gamma"],
        )

    def test_additive_rename_replaces_active_alias_in_place(self):
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        self.assertEqual(
            self.store.resolve_selection([f"renamed={self.alpha}"], add=True),
            ["renamed", "beta"],
        )
        self.assertNotIn("alpha", self.store.load_locations())

    def test_normal_selection_still_replaces_and_rejects_duplicates(self):
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        self.assertEqual(self.store.resolve_selection(["beta"]), ["beta"])
        with self.assertRaisesRegex(CliError, "Duplicate project name"):
            self.store.resolve_selection(["beta", "beta"])

    def test_override_uses_dual_paths_and_runtime_contract(self):
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        self.store.generate_override()
        document = json.loads(self.store.override_file.read_text())
        alpha = str(self.alpha.resolve())
        beta = str(self.beta.resolve())
        self.assertEqual(
            document["services"]["hermes"]["volumes"],
            [
                {"type": "bind", "source": alpha, "target": "/workspace/alpha"},
                {"type": "bind", "source": alpha, "target": alpha},
                {"type": "bind", "source": beta, "target": "/workspace/beta"},
                {"type": "bind", "source": beta, "target": beta},
                {
                    "type": "bind",
                    "source": str(self.store.runtime_directory.resolve()),
                    "target": WORKSPACE_RUNTIME_CONTAINER_DIRECTORY,
                    "read_only": True,
                },
            ],
        )
        self.assertEqual(
            document["services"]["hermes"]["environment"],
            {
                "HERMES_EPHEMERAL_SYSTEM_PROMPT": WORKSPACE_SYSTEM_PROMPT,
                "HERMES_STACK_WORKSPACE_MANIFEST": WORKSPACE_MANIFEST_CONTAINER_PATH,
                "HERMES_WRITE_SAFE_ROOT": f"/workspace:/opt/data:{alpha}:{beta}",
                "HERMES_STACK_DEV_SERVER_HOST": "0.0.0.0",
                "HERMES_STACK_DEV_SERVER_PORT": str(DEV_SERVER_PORT),
            },
        )
        self.assertEqual(
            document["services"]["tailscale"]["volumes"],
            [
                {
                    "type": "bind",
                    "source": str(self.store.tailscale_runtime_directory.resolve()),
                    "target": "/run/hermes-stack/tailscale",
                    "read_only": True,
                }
            ],
        )
        self.assertEqual(
            json.loads(self.store.workspace_manifest_file.read_text()),
            {
                "version": 1,
                "workspaces": [
                    {
                        "name": "alpha",
                        "workspace_path": "/workspace/alpha",
                        "host_path": alpha,
                        "node_project": False,
                        "obsidian_vault": False,
                    },
                    {
                        "name": "beta",
                        "workspace_path": "/workspace/beta",
                        "host_path": beta,
                        "node_project": False,
                        "obsidian_vault": False,
                    },
                ],
            },
        )
        self.assertEqual(
            stat.S_IMODE(self.store.workspace_manifest_file.stat().st_mode),
            0o644,
        )
        self.assertEqual(
            self.store.runtime_manifest_file.read_text(),
            self.store.workspace_manifest_file.read_text(),
        )
        self.assertEqual(
            stat.S_IMODE(self.store.runtime_manifest_file.stat().st_mode),
            0o644,
        )

    def test_tailscale_serve_preview_is_always_private_and_vscode_is_optional(self):
        disabled = tailscale_serve_document(False)
        self.assertIn(str(DEV_SERVER_PORT), disabled["TCP"])
        self.assertNotIn(str(VSCODE_PORT), disabled["TCP"])
        self.assertFalse(
            disabled["AllowFunnel"][f"${{TS_CERT_DOMAIN}}:{DEV_SERVER_PORT}"]
        )
        enabled = tailscale_serve_document(True)
        self.assertIn(str(VSCODE_PORT), enabled["TCP"])
        self.assertEqual(
            enabled["Web"][f"${{TS_CERT_DOMAIN}}:{VSCODE_PORT}"]["Handlers"]["/"]["Proxy"],
            f"http://127.0.0.1:{VSCODE_PORT}",
        )
        fixture = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "runtime"
            / "tailscale"
            / "serve.json"
        )
        self.assertEqual(json.loads(fixture.read_text()), disabled)

    def test_vscode_password_is_private_stable_and_rotatable(self):
        first = self.store.ensure_vscode_config()
        self.assertEqual(first, self.store.ensure_vscode_config())
        self.assertEqual(stat.S_IMODE(self.store.vscode_config_file.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.store.vscode_state_directory.stat().st_mode), 0o700
        )
        second = self.store.ensure_vscode_config(reset=True)
        self.assertNotEqual(first, second)

    def test_vscode_mounts_only_selected_workspace_aliases(self):
        (self.alpha / "package.json").write_text("{}")
        (self.alpha / ".obsidian").mkdir()
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        self.store.resolve_selection(["alpha"])
        configure_obsidian(self.store, "alpha")
        self.store.ensure_vscode_config()
        self.store.save_vscode_enabled(True)
        self.store.generate_override()

        document = json.loads(self.store.override_file.read_text())
        volumes = document["services"]["vscode"]["volumes"]
        targets = [volume["target"] for volume in volumes]
        serialized = json.dumps(volumes)
        self.assertEqual(
            targets,
            [
                "/state",
                "/run/secrets/code-server-config.json",
                "/workspace/alpha",
                "/workspace/alpha/node_modules",
                "/workspace/alpha/.obsidian",
            ],
        )
        self.assertNotIn(str(self.beta.resolve()), serialized)
        self.assertNotIn(f'"target": "{self.alpha.resolve()}"', serialized)
        self.assertNotIn("/opt/data", serialized)
        self.assertTrue(volumes[1]["read_only"])
        self.assertTrue(volumes[3]["read_only"])
        self.assertTrue(volumes[4]["read_only"])
        self.assertNotIn(
            str(self.store.vscode_config_file.resolve()),
            json.dumps(volumes[0]),
        )

    def test_disabled_vscode_has_no_service_or_tailscale_listener(self):
        self.store.save_vscode_enabled(False)
        self.store.generate_override()
        document = json.loads(self.store.override_file.read_text())
        self.assertNotIn("vscode", document["services"])
        serve = json.loads(self.store.tailscale_serve_file.read_text())
        self.assertNotIn(str(VSCODE_PORT), serve["TCP"])

    def test_vscode_route_change_is_deferred_until_refresh(self):
        self.store.generate_override()
        self.store.ensure_vscode_config()
        self.store.save_vscode_enabled(True)
        self.store.generate_override()
        serve = json.loads(self.store.tailscale_serve_file.read_text())
        self.assertNotIn(str(VSCODE_PORT), serve["TCP"])
        self.store.generate_override(refresh_tailscale=True)
        serve = json.loads(self.store.tailscale_serve_file.read_text())
        self.assertIn(str(VSCODE_PORT), serve["TCP"])

    def test_unselected_locations_are_absent_from_runtime_files(self):
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        self.store.resolve_selection(["alpha"])
        self.store.generate_override()
        override = self.store.override_file.read_text()
        manifest = self.store.workspace_manifest_file.read_text()
        self.assertNotIn(str(self.beta.resolve()), override)
        self.assertNotIn(str(self.beta.resolve()), manifest)

    def test_empty_selection_generates_manifest_only_mount(self):
        self.store.generate_override()
        document = json.loads(self.store.override_file.read_text())
        service = document["services"]["hermes"]
        self.assertEqual(len(service["volumes"]), 1)
        self.assertEqual(
            service["environment"]["HERMES_WRITE_SAFE_ROOT"],
            "/workspace:/opt/data",
        )
        self.assertEqual(
            json.loads(self.store.workspace_manifest_file.read_text())["workspaces"],
            [],
        )

    def test_paths_with_spaces_are_preserved_in_long_volume_syntax(self):
        spaced = self.root / "project with spaces"
        spaced.mkdir()
        self.store.resolve_selection([f"spaced={spaced}"])
        self.store.generate_override()
        volumes = json.loads(self.store.override_file.read_text())["services"]["hermes"][
            "volumes"
        ]
        self.assertEqual(volumes[0]["source"], str(spaced.resolve()))
        self.assertEqual(volumes[1]["target"], str(spaced.resolve()))

    def test_legacy_selection_is_migrated(self):
        legacy_store = StateStore(self.root / "legacy")
        legacy_store.ensure_directories()
        legacy_store.legacy_selection_file.write_text(
            f"alpha={self.alpha}\nbeta={self.beta}\n"
        )
        legacy_store.migrate_legacy_state()
        self.assertEqual(legacy_store.load_selection(), ["alpha", "beta"])
        self.assertEqual(
            list(legacy_store.load_locations()),
            ["alpha", "beta"],
        )

    def test_version_one_registry_is_migrated_to_structured_entries(self):
        self.store.locations_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "locations": {"alpha": str(self.alpha.resolve())},
                }
            )
        )
        self.store.migrate_location_registry()
        self.assertEqual(
            json.loads(self.store.locations_file.read_text()),
            {
                "version": 2,
                "locations": {
                    "alpha": {"path": str(self.alpha.resolve()), "node": "auto"}
                },
            },
        )

    def test_version_two_registry_rejects_invalid_node_mode(self):
        self.store.locations_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "locations": {
                        "alpha": {
                            "path": str(self.alpha.resolve()),
                            "node": "sometimes",
                        }
                    },
                }
            )
        )
        with self.assertRaisesRegex(CliError, "Invalid location entry"):
            self.store.load_locations()

    def test_node_modes_generate_persistent_dual_overlays(self):
        (self.alpha / "package.json").write_text("{}")
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        locations = self.store.load_locations()
        locations["beta"] = Location(locations["beta"].path, "on")
        self.store.save_locations(locations)
        self.store.generate_override()
        document = json.loads(self.store.override_file.read_text())
        alpha_key, alpha_volume = node_modules_volume(str(self.alpha.resolve()))
        beta_key, beta_volume = node_modules_volume(str(self.beta.resolve()))
        self.assertEqual(
            document["volumes"],
            {
                alpha_key: {"name": alpha_volume},
                beta_key: {"name": beta_volume},
            },
        )
        module_mounts = [
            volume
            for volume in document["services"]["hermes"]["volumes"]
            if volume["type"] == "volume"
        ]
        self.assertEqual(
            [mount["target"] for mount in module_mounts],
            [
                "/workspace/alpha/node_modules",
                f"{self.alpha.resolve()}/node_modules",
                "/workspace/beta/node_modules",
                f"{self.beta.resolve()}/node_modules",
            ],
        )
        self.assertTrue(all(mount["volume"] == {"nocopy": True} for mount in module_mounts))

    def test_auto_node_detection_is_refreshed_when_package_json_changes(self):
        self.store.resolve_selection([f"alpha={self.alpha}"])
        self.assertFalse(self.store.selected_workspaces()[0].node_project)
        (self.alpha / "package.json").write_text("{}")
        self.assertTrue(self.store.selected_workspaces()[0].node_project)
        (self.alpha / "package.json").unlink()
        self.assertFalse(self.store.selected_workspaces()[0].node_project)

    def test_node_off_suppresses_detection_and_volume_mounts(self):
        (self.alpha / "package.json").write_text("{}")
        self.store.resolve_selection([f"alpha={self.alpha}"])
        locations = self.store.load_locations()
        locations["alpha"] = Location(locations["alpha"].path, "off")
        self.store.save_locations(locations)
        self.store.generate_override()
        document = json.loads(self.store.override_file.read_text())
        self.assertFalse(self.store.selected_workspaces()[0].node_project)
        self.assertNotIn("volumes", document)

    def test_node_volume_identity_survives_location_rename(self):
        self.store.resolve_selection([f"old={self.alpha}"])
        locations = self.store.load_locations()
        locations["old"] = Location(locations["old"].path, "on")
        self.store.save_locations(locations)
        self.store.generate_override()
        before = json.loads(self.store.override_file.read_text())["volumes"]
        update_location(self.store, "new", str(self.alpha))
        self.store.generate_override()
        after = json.loads(self.store.override_file.read_text())["volumes"]
        self.assertEqual(after, before)
        self.assertEqual(
            node_modules_volume(str(self.alpha / ".." / self.alpha.name)),
            node_modules_volume(str(self.alpha.resolve())),
        )

    def test_obsidian_sidecar_isolated_and_config_read_only_to_hermes(self):
        self.store.resolve_selection([f"alpha={self.alpha}"])
        configure_obsidian(self.store, "alpha")
        self.store.generate_override()
        document = json.loads(self.store.override_file.read_text())
        hermes_volumes = document["services"]["hermes"]["volumes"]
        protected = [
            volume for volume in hermes_volumes
            if volume["target"].endswith("/.obsidian")
        ]
        self.assertEqual(
            [volume["target"] for volume in protected],
            ["/workspace/alpha/.obsidian", f"{self.alpha.resolve()}/.obsidian"],
        )
        self.assertTrue(all(volume["read_only"] for volume in protected))
        sidecar = document["services"]["obsidian-sync"]
        self.assertEqual(sidecar["volumes"][0]["source"], str(self.alpha.resolve()))
        self.assertEqual(sidecar["volumes"][0]["target"], "/vault")
        self.assertNotIn("read_only", sidecar["volumes"][0])
        self.assertEqual(
            sidecar["volumes"][1],
            {
                "type": "bind",
                "source": str(self.store.obsidian_state_directory.resolve()),
                "target": "/state/obsidian-headless",
            },
        )
        self.assertNotIn(
            str(self.store.obsidian_state_directory.resolve()),
            json.dumps(hermes_volumes),
        )

    def test_unselected_obsidian_mirror_only_appears_in_sidecar(self):
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        configure_obsidian(self.store, "beta")
        self.store.resolve_selection(["alpha"])
        self.store.generate_override()
        document = json.loads(self.store.override_file.read_text())
        self.assertEqual(
            document["services"]["obsidian-sync"]["volumes"][0]["source"],
            str(self.beta.resolve()),
        )
        self.assertNotIn(
            str(self.beta.resolve()),
            json.dumps(document["services"]["hermes"]),
        )
        manifest = json.loads(self.store.workspace_manifest_file.read_text())
        self.assertEqual([entry["name"] for entry in manifest["workspaces"]], ["alpha"])

    def test_location_commands_keep_selection_consistent(self):
        self.store.resolve_selection([f"old={self.alpha}"])
        update_location(self.store, "new", str(self.alpha))
        self.assertEqual(self.store.load_selection(), ["new"])
        remove_location(self.store, "new")
        self.assertEqual(self.store.load_selection(), [])


class StateDirectoryTests(unittest.TestCase):
    def test_new_install_uses_hermes_stack_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertEqual(
                resolve_state_directory(home),
                home / ".config" / "hermes-stack",
            )

    def test_legacy_directory_requires_explicit_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            legacy = home / ".config" / "hermes-docker"
            legacy.mkdir(parents=True)
            with self.assertRaisesRegex(CliError, r"mv .*hermes-docker .*hermes-stack"):
                resolve_state_directory(home)

    def test_stop_can_use_legacy_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            legacy = home / ".config" / "hermes-docker"
            legacy.mkdir(parents=True)
            self.assertEqual(
                resolve_state_directory(home, allow_legacy=True),
                legacy,
            )

    def test_new_directory_wins_when_both_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            legacy = home / ".config" / "hermes-docker"
            current = home / ".config" / "hermes-stack"
            legacy.mkdir(parents=True)
            current.mkdir()
            self.assertEqual(resolve_state_directory(home), current)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.store = StateStore(self.home / ".config" / "hermes-stack")
        self.store.ensure_directories()

    def tearDown(self):
        self.temporary.cleanup()

    def test_location_names_are_live_ordered_and_read_only(self):
        alpha = self.root / "alpha"
        beta = self.root / "beta"
        alpha.mkdir()
        beta.mkdir()
        self.store.resolve_selection([f"alpha={alpha}", f"beta={beta}"])
        registry_before = self.store.locations_file.read_text()
        self.assertEqual(completion_location_names(self.home), ["alpha", "beta"])
        self.assertEqual(self.store.locations_file.read_text(), registry_before)

    def test_legacy_location_names_are_available_without_migration(self):
        legacy_home = self.root / "legacy-home"
        legacy = legacy_home / ".config" / "hermes-docker"
        legacy.mkdir(parents=True)
        legacy_file = legacy / "current-projects"
        legacy_file.write_text("alpha=/tmp/alpha\nbeta=/tmp/beta\nalpha=/tmp/other\n")
        self.assertEqual(completion_location_names(legacy_home), ["alpha", "beta"])
        self.assertFalse((legacy / "locations.json").exists())

    def test_invalid_registry_does_not_break_shell_completion(self):
        self.store.locations_file.write_text("not json")
        self.assertEqual(completion_location_names(self.home), [])

    def test_templates_cover_supported_shells_and_dynamic_locations(self):
        stack_dir = Path(__file__).resolve().parents[1]
        for shell in ("fish", "bash", "zsh"):
            with self.subTest(shell=shell):
                script = render_completions(shell, stack_dir)
                self.assertIn("hermes-stack completions", script)
                self.assertIn("--location-names", script)
                self.assertIn("reset-node-modules", script)
                self.assertIn("obsidian", script)
                self.assertIn("vscode", script)
                self.assertTrue("--add" in script or "-l add" in script)

    def test_completion_output_does_not_initialize_state(self):
        empty_home = self.root / "empty-home"
        empty_home.mkdir()
        output = io.StringIO()
        with patch("hermes_stack_cli.Path.home", return_value=empty_home):
            with redirect_stdout(output):
                self.assertEqual(run(["completions", "fish"]), 0)
        self.assertIn("complete -c hermes-stack", output.getvalue())
        self.assertFalse((empty_home / ".config").exists())

    def test_parser_accepts_additive_start_and_vscode_commands(self):
        parser = build_parser("hermes-stack")
        start = parser.parse_args(["start", "--add", "alpha"])
        self.assertTrue(start.add)
        self.assertEqual(start.projects, ["alpha"])
        vscode = parser.parse_args(["vscode", "reset-password"])
        self.assertEqual(vscode.vscode_command, "reset-password")

    def test_additive_start_requires_a_location(self):
        empty_home = self.root / "add-home"
        empty_home.mkdir()
        with patch("hermes_stack_cli.Path.home", return_value=empty_home):
            with self.assertRaisesRegex(CliError, "requires at least one project"):
                run(["start", "--add"])


class WorkspacePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def assert_rejected(self, path, message):
        path.mkdir(parents=True, exist_ok=True)
        with self.assertRaisesRegex(CliError, message):
            validate_workspace_directory(path.resolve(), home=self.home)

    def test_project_and_external_vault_paths_are_allowed(self):
        project = self.home / "projects" / "app"
        vault = self.root / "external" / "obsidian-vault"
        project.mkdir(parents=True)
        vault.mkdir(parents=True)
        validate_workspace_directory(project.resolve(), home=self.home)
        validate_workspace_directory(vault.resolve(), home=self.home)

    def test_filesystem_root_and_home_are_rejected(self):
        with self.assertRaisesRegex(CliError, "filesystem root"):
            validate_workspace_directory(Path("/"), home=self.home)
        with self.assertRaisesRegex(CliError, "home directory"):
            validate_workspace_directory(self.home, home=self.home)
        with self.assertRaisesRegex(CliError, "home directory"):
            validate_workspace_directory(self.root, home=self.home)

    def test_credential_and_config_trees_are_rejected(self):
        for relative in (".ssh", ".config/hermes-stack", ".docker", ".hermes"):
            with self.subTest(relative=relative):
                self.assert_rejected(self.home / relative, "sensitive path")

    def test_docker_runtime_and_reserved_container_paths_are_rejected(self):
        for path in (Path("/var/run"), Path("/private/var/run")):
            with self.subTest(path=path):
                with self.assertRaisesRegex(CliError, "sensitive path"):
                    validate_workspace_directory(path, home=self.home)
        for path in (
            Path("/workspace/project"),
            Path("/opt/data/project"),
            Path("/opt/hermes/project"),
            Path("/opt/open-design/project"),
            Path("/command/project"),
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(CliError, "reserved container path"):
                    validate_workspace_directory(path, home=self.home)

    def test_path_separator_is_rejected_before_safe_root_serialization(self):
        unsafe = self.root / "project:opt"
        unsafe.mkdir()
        with self.assertRaisesRegex(CliError, "HERMES_WRITE_SAFE_ROOT"):
            validate_workspace_directory(unsafe.resolve(), home=self.home)


if __name__ == "__main__":
    unittest.main()
