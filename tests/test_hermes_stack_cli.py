import json
import tempfile
import unittest
from pathlib import Path

from hermes_stack_cli import CliError, StateStore, remove_location, update_location


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

    def test_override_uses_stable_container_paths(self):
        self.store.resolve_selection([f"alpha={self.alpha}", f"beta={self.beta}"])
        self.store.generate_override()
        document = json.loads(self.store.override_file.read_text())
        self.assertEqual(
            document["services"]["hermes"]["volumes"],
            [
                f"{self.alpha.resolve()}:/workspace/alpha",
                f"{self.beta.resolve()}:/workspace/beta",
            ],
        )

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


if __name__ == "__main__":
    unittest.main()
