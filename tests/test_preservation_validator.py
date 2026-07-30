#!/usr/bin/env python3
"""Offline mutation tests for the deterministic preservation validator."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "preservation_validator", ROOT / "scripts" / "validate_preservation_baseline.py"
)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class SourcePreservationTests(unittest.TestCase):
    def test_committed_sources_match_paragraph_inventory(self) -> None:
        baseline = json.loads((ROOT / "tests/baselines/preservation.json").read_text(encoding="utf-8"))
        errors: list[str] = []
        validator.validate_sources(baseline, errors)
        self.assertEqual(errors, [])
        for article in baseline["articles"]:
            _, body = validator.split_post(ROOT / article["source"])
            self.assertEqual(validator.segment_digests(body), article["proseSegmentSha256"])

    def test_mutations_name_configuration_assets_and_prose_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            post = root / "content/posts/example.md"
            layout = root / "layouts/partials/header.html"
            style = root / "assets/css/extended/layout.css"
            post.parent.mkdir(parents=True)
            layout.parent.mkdir(parents=True)
            style.parent.mkdir(parents=True)
            body = "First established paragraph.\n\nSecond established paragraph."
            post.write_text(
                '+++\ntitle = "Example"\ndate = 2026-01-01T09:00:00-07:00\n'
                'draft = false\nslug = "example"\n+++\n\n' + body + "\n",
                encoding="utf-8",
            )
            layout.write_text("<header>established</header>\n", encoding="utf-8")
            style.write_text(":root { color: black; }\n", encoding="utf-8")
            (root / "hugo.toml").write_text("baseURL = 'https://example.invalid/'\n", encoding="utf-8")
            front_matter, exact_body = validator.split_post(post)
            baseline = {
                "protectedFiles": {
                    "layouts/partials/header.html": validator.digest(layout.read_bytes()),
                    "assets/css/extended/layout.css": validator.digest(style.read_bytes()),
                },
                "paperModCommit": "a" * 40,
                "hugoConfiguration": {"baseURL": "https://example.invalid/"},
                "articles": [{
                    "source": "content/posts/example.md",
                    "frontMatter": validator.json_front_matter(front_matter),
                    "proseSha256": validator.digest(exact_body),
                    "proseSegmentSha256": validator.segment_digests(exact_body),
                    "citationDestinations": [],
                }],
            }

            # Independent changes exercise diagnostics without relying on mtime or path order.
            layout.write_text("<header>changed</header>\n", encoding="utf-8")
            style.write_text(":root { color: red; }\n", encoding="utf-8")
            (root / "hugo.toml").write_text("title = 'Missing base URL'\n", encoding="utf-8")
            post.write_text(post.read_text(encoding="utf-8").replace("Second established", "Second altered"), encoding="utf-8")
            git_result = SimpleNamespace(stdout=f"160000 {'a' * 40} 0\tthemes/PaperMod\n")
            errors: list[str] = []
            with mock.patch.object(validator, "ROOT", root), mock.patch.object(
                validator.subprocess, "run", return_value=git_result
            ):
                validator.validate_sources(baseline, errors)

            self.assertIn("protected presentation/configuration changed: layouts/partials/header.html", errors)
            self.assertIn("protected presentation/configuration changed: assets/css/extended/layout.css", errors)
            self.assertIn("Hugo setting baseURL is missing; expected 'https://example.invalid/'", errors)
            self.assertIn(
                "essay prose segment 2 changed without baseline review: content/posts/example.md", errors
            )


class RenderedPreservationTests(unittest.TestCase):
    def test_missing_article_output_names_the_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            (destination / "index.html").write_text("<html><body></body></html>", encoding="utf-8")
            home = validator.parse_html(destination / "index.html")
            baseline = {
                "homeListing": [],
                "articles": [{
                    "route": "/established-route/",
                    "frontMatter": {"title": "Established title"},
                    "renderedProseSha256": validator.digest(""),
                    "citationDestinations": [],
                    "renderedStructureSha256": validator.digest(""),
                }],
                "renderedContract": {
                    "homeStructureSha256": validator.digest("\n".join(home.structure)),
                    "homeMarkers": [], "articleMarkers": [], "siteMarkers": [],
                },
            }
            errors: list[str] = []
            validator.validate_rendered(baseline, destination, errors)
            self.assertEqual(errors, ["article route /established-route/ did not render index.html"])

    def test_hugo_failure_removes_absolute_paths_and_durations(self) -> None:
        destination = Path("/tmp/random-build-123")
        message = (
            f"Error: {validator.ROOT}/layouts/index.html: render failed in {destination}\n"
            "Total in 247 ms\n"
        )
        diagnostic = validator.hugo_diagnostic(message, destination)
        self.assertEqual(
            diagnostic,
            "Error: <repository>/layouts/index.html: render failed in <destination>",
        )


class CliTests(unittest.TestCase):
    def test_source_only_cli_is_network_free_and_successful(self) -> None:
        result = subprocess.run(
            ["python3", "scripts/validate_preservation_baseline.py", "--source-only"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Preservation baseline passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
