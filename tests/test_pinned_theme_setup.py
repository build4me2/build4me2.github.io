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

    def test_payload_digests_are_pinned(self) -> None:
        setup.verify_payload(setup.ARCHIVE, setup.ARCHIVE_SHA256, "snapshot")
        setup.verify_payload(setup.COMMIT_OBJECT, setup.COMMIT_OBJECT_SHA256, "commit object")


if __name__ == "__main__":
    unittest.main()
