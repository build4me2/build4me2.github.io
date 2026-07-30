#!/usr/bin/env python3
"""Offline unit tests for the deterministic Hugo build entry point."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deterministic_build", ROOT / "scripts/build_site.py")
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class DeterministicBuildTests(unittest.TestCase):
    def test_environment_and_hugo_arguments_are_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "site"
            cache = root / "cache"
            destination.mkdir()
            (destination / "stale.html").write_text("stale", encoding="utf-8")

            def fake_run(command, **kwargs):
                self.assertFalse((destination / "stale.html").exists())
                environment = kwargs["env"]
                self.assertEqual(environment["TZ"], "UTC")
                self.assertEqual(environment["LC_ALL"], "C.UTF-8")
                self.assertEqual(environment["SOURCE_DATE_EPOCH"], builder.SOURCE_DATE_EPOCH)
                self.assertIn("--cleanDestinationDir", command)
                self.assertIn("--ignoreCache", command)
                self.assertEqual(command[command.index("--clock") + 1], builder.BUILD_CLOCK)
                self.assertEqual(command[command.index("--environment") + 1], "production")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(builder.subprocess, "run", side_effect=fake_run):
                builder.build(destination, cache, preflight=False)

    def test_manifest_is_path_sorted_and_ignores_only_filesystem_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "z").write_bytes(b"same")
            (root / "a").write_bytes(b"same")
            manifest = builder.tree_manifest(root)
            self.assertEqual([path for path, _digest in manifest], ["a", "z"])
            before = list(manifest)
            os.chmod(root / "a", 0o600)
            os.utime(root / "a", (1, 1))
            self.assertEqual(builder.tree_manifest(root), before)
            (root / "a").write_bytes(b"changed")
            self.assertNotEqual(builder.tree_manifest(root), before)

    def test_cleaner_refuses_repository_root(self) -> None:
        with self.assertRaisesRegex(builder.BuildError, "refusing to clean destination"):
            builder.clean_directory(ROOT, "destination")

    def test_reproducibility_diagnostic_is_stably_path_sorted(self) -> None:
        manifests = [
            [("z.html", "one"), ("a.html", "same")],
            [("a.html", "same"), ("b.html", "two")],
        ]
        with mock.patch.object(builder, "verify_hugo"), mock.patch.object(builder, "build"), mock.patch.object(
            builder, "tree_manifest", side_effect=manifests
        ):
            with self.assertRaises(builder.BuildError) as raised:
                builder.verify_reproducible()
        self.assertEqual(
            str(raised.exception),
            "clean builds are not reproducible:\n"
            "- b.html: missing from first build\n"
            "- z.html: missing from second build",
        )


if __name__ == "__main__":
    unittest.main()
