import json
import stat
import tempfile
import unittest
from pathlib import Path

from hermes_stack_cli import (
    CliError,
    StateStore,
    WORKSPACE_MANIFEST_CONTAINER_PATH,
    WORKSPACE_RUNTIME_CONTAINER_DIRECTORY,
    WORKSPACE_SYSTEM_PROMPT,
    remove_location,
    resolve_state_directory,
    update_location,
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
            {"new-name": str(self.alpha.resolve())},
        )
        self.assertEqual(self.store.load_selection(), ["new-name"])

    def test_reassigning_name_overwrites_its_path(self):
        self.store.resolve_selection([f"app={self.alpha}"])
        self.store.resolve_selection([f"app={self.beta}"])
        self.assertEqual(self.store.load_locations()["app"], str(self.beta.resolve()))

    def test_unknown_bare_name_does_not_change_state(self):
        self.store.resolve_selection([f"alpha={self.alpha}"])
        with self.assertRaises(CliError):
            self.store.resolve_selection(["missing"])
        self.assertEqual(self.store.load_selection(), ["alpha"])

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
            },
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
                    },
                    {
                        "name": "beta",
                        "workspace_path": "/workspace/beta",
                        "host_path": beta,
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


if __name__ == "__main__":
    unittest.main()
