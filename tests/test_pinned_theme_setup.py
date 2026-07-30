#!/usr/bin/env python3
"""Tests for deterministic, network-free PaperMod setup."""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pinned_theme_setup", ROOT / "scripts" / "setup_pinned_theme.py"
)
assert SPEC and SPEC.loader
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class PinnedThemeSetupTests(unittest.TestCase):
    def test_snapshot_materializes_exact_commit_and_tree_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "themes" / "PaperMod"
            real_run = setup.subprocess.run

            def offline_run(command, *args, **kwargs):
                self.assertEqual(command[0], "git")
                self.assertNotIn("clone", command)
                self.assertNotIn("fetch", command)
                return real_run(command, *args, **kwargs)

            with mock.patch.object(setup.subprocess, "run", side_effect=offline_run):
                self.assertTrue(setup.materialize(destination))
                self.assertFalse(setup.materialize(destination))

            head = subprocess.run(
                ["git", "-C", str(destination), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", str(destination), "rev-parse", "HEAD^{tree}"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            self.assertEqual(head, setup.PINNED_COMMIT)
            self.assertEqual(tree, setup.PINNED_TREE)
            self.assertTrue((destination / "layouts" / "baseof.html").is_file())

    def test_unchanged_head_with_css_mutation_cannot_pass_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "themes" / "PaperMod"
            self.assertTrue(setup.materialize(destination))
            css = destination / "assets/css/core/theme-vars.css"
            css.write_text(css.read_text(encoding="utf-8") + "\n:root { --theme: red; }\n", encoding="utf-8")

            with self.assertRaises(setup.SetupError) as raised:
                setup.materialize(destination)

            self.assertEqual(
                str(raised.exception),
                "PaperMod worktree differs from the pinned commit:\n"
                "- modified: assets/css/core/theme-vars.css\n"
                "Restore or remove these paths before rerunning setup",
            )
            self.assertEqual(setup.checked_out_commit(destination), setup.PINNED_COMMIT)

    def test_dirty_path_diagnostics_are_complete_and_path_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "PaperMod"
            setup.materialize(destination)
            (destination / "assets/css/common/404.css").unlink()
            (destination / "assets/css/common/footer.css").write_text("changed\n", encoding="utf-8")
            (destination / "assets/css/aa-untracked.css").write_text("untracked\n", encoding="utf-8")

            self.assertEqual(setup.worktree_changes(destination), [
                ("assets/css/aa-untracked.css", "untracked"),
                ("assets/css/common/404.css", "deleted"),
                ("assets/css/common/footer.css", "modified"),
            ])

    def test_payload_digests_are_pinned(self) -> None:
        setup.verify_payload(setup.ARCHIVE, setup.ARCHIVE_SHA256, "snapshot")
        setup.verify_payload(setup.COMMIT_OBJECT, setup.COMMIT_OBJECT_SHA256, "commit object")


if __name__ == "__main__":
    unittest.main()
