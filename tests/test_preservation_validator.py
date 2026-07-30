#!/usr/bin/env python3
"""Offline mutation tests for the deterministic preservation validator."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
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
    def test_hugo_toolchain_preflight_reports_version_and_edition_mismatches(self) -> None:
        expected = "0.162.0"
        cases = [
            (
                "hugo v0.161.0+extended linux/amd64\n",
                "Hugo toolchain mismatch: found Hugo Extended 0.161.0; "
                "expected Hugo Extended 0.162.0",
            ),
            (
                "hugo v0.162.0 linux/amd64\n",
                "Hugo toolchain mismatch: found Hugo 0.162.0; "
                "expected Hugo Extended 0.162.0",
            ),
        ]
        for output, diagnostic in cases:
            with self.subTest(output=output):
                errors: list[str] = []
                result = subprocess.CompletedProcess([], 0, output, "")
                with mock.patch.object(validator.subprocess, "run", return_value=result):
                    self.assertFalse(validator.validate_hugo_toolchain(expected, True, errors))
                self.assertEqual(errors, [diagnostic])

        extended_env = (
            "hugo v0.162.0 linux/amd64\n"
            'github.com/bep/golibsass="v1.2.0"\n'
        )
        errors = []
        with mock.patch.object(
            validator.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, extended_env, ""),
        ):
            self.assertTrue(validator.validate_hugo_toolchain(expected, True, errors))
        self.assertEqual(errors, [])

    def test_theme_checkout_preflight_names_uninitialized_and_mismatched_worktrees(self) -> None:
        expected = "a" * 40
        gitlink = subprocess.CompletedProcess([], 0, f"160000 {expected} 0\tthemes/PaperMod\n", "")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            theme = root / "themes/PaperMod"
            theme.mkdir(parents=True)
            errors: list[str] = []
            with mock.patch.object(validator, "ROOT", root), mock.patch.object(
                validator.subprocess, "run", return_value=gitlink
            ):
                self.assertFalse(validator.validate_theme_checkout(expected, errors))
            self.assertEqual(errors, [
                "PaperMod worktree is not initialized; run "
                "'git submodule update --init --recursive' before validation"
            ])

            (theme / "README.md").write_text("initialized\n", encoding="utf-8")
            checkout = subprocess.CompletedProcess([], 0, "b" * 40 + "\n", "")
            errors = []
            with mock.patch.object(validator, "ROOT", root), mock.patch.object(
                validator.subprocess, "run", side_effect=[gitlink, checkout]
            ):
                self.assertFalse(validator.validate_theme_checkout(expected, errors))
            self.assertEqual(errors, [
                f"PaperMod worktree is at {'b' * 40!r}; expected pinned commit {expected}; "
                "run 'git submodule update --init --recursive'"
            ])

    def test_committed_sources_match_paragraph_inventory(self) -> None:
        baseline = json.loads((ROOT / "tests/baselines/preservation.json").read_text(encoding="utf-8"))
        self.assertEqual(validator.validate_baseline_schema(baseline), [])
        errors: list[str] = []
        # Source inventory is a unit concern; the real submodule checkout is an
        # integration preflight covered independently below and by the CLI.
        with mock.patch.object(validator, "validate_theme_checkout", return_value=True):
            validator.validate_sources(baseline, errors)
        self.assertEqual(errors, [])
        for article in baseline["articles"]:
            _, body = validator.split_post(ROOT / article["source"])
            self.assertEqual(validator.segment_digests(body), article["proseSegmentSha256"])
        for name, relative_root in validator.PRESENTATION_ROOTS.items():
            inventory = baseline["presentationFileSets"][name]
            actual = sorted(
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / relative_root).rglob("*")
                if path.is_file()
            )
            self.assertEqual(inventory, sorted(set(inventory)))
            self.assertEqual(actual, inventory)

    def test_review_records_require_a_real_change_and_meaningful_evidence(self) -> None:
        record = {
            "id": "   ",
            "kind": "citation-reconciliation",
            "article": "content/posts/example.md",
            "citationIndex": 1,
            "before": "https://example.invalid/source",
            "after": "https://example.invalid/source",
            "reason": "   ",
            "verificationEvidence": ["\t"],
            "proseArgumentRoutePresentationUnchanged": True,
        }
        errors = validator.validate_review_records_schema({
            "schema": "preservation-review-records/v1", "records": [record]
        })
        self.assertEqual(errors, [
            "$.records[0].id: expected non-empty string",
            "$.records[0].reason: expected non-empty string",
            "$.records[0].verificationEvidence: expected non-empty array of non-empty strings",
            "$.records[0]: before and after must differ",
        ])

    def test_captured_history_rejects_rebaselined_prose_and_unreviewed_links(self) -> None:
        baseline = json.loads((ROOT / "tests/baselines/preservation.json").read_text(encoding="utf-8"))
        baseline["capturedFrom"] = "a" * 40
        self.assertIn(
            f"$.capturedFrom: expected pinned source commit {validator.CAPTURED_FROM_COMMIT!r}",
            validator.validate_baseline_schema(baseline),
        )

        original = (
            "Established argument with "
            '<a href="https://old.example/source">verified evidence</a>.'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            post = root / "content/posts/example.md"
            post.parent.mkdir(parents=True)
            prefix = '+++\ntitle = "Example"\ndate = 2026-01-01\ndraft = false\nslug = "example"\n+++\n\n'
            article = {"source": "content/posts/example.md"}

            post.write_text(prefix + original + "\n", encoding="utf-8")
            with mock.patch.object(validator, "ROOT", root):
                errors: list[str] = []
                validator.validate_article_history(article, original, [], errors)
            self.assertEqual(errors, [])

            post.write_text(prefix + original.replace("Established argument", "Altered argument") + "\n", encoding="utf-8")
            with mock.patch.object(validator, "ROOT", root):
                errors = []
                validator.validate_article_history(article, original, [], errors)
            self.assertEqual(errors, [
                "essay prose/argument differs from captured source commit: content/posts/example.md"
            ])

            changed = original.replace("https://old.example/source", "https://verified.example/source")
            post.write_text(prefix + changed + "\n", encoding="utf-8")
            with mock.patch.object(validator, "ROOT", root):
                errors = []
                validator.validate_article_history(article, original, [], errors)
            self.assertEqual(errors, [
                "citation destination 1 changed without one exact review record: content/posts/example.md"
            ])

            record = {
                "id": "example-citation-fix",
                "kind": "citation-reconciliation",
                "article": "content/posts/example.md",
                "citationIndex": 1,
                "before": "https://old.example/source",
                "after": "https://verified.example/source",
                "reason": "The inherited destination was defective.",
                "verificationEvidence": ["Publisher canonical record checked."],
                "proseArgumentRoutePresentationUnchanged": True,
            }
            self.assertEqual(validator.validate_review_records_schema({
                "schema": "preservation-review-records/v1", "records": [record]
            }), [])
            with mock.patch.object(validator, "ROOT", root):
                errors = []
                validator.validate_article_history(article, original, [record], errors)
            self.assertEqual(errors, [])

            record["after"] = "https://wrong.example/source"
            with mock.patch.object(validator, "ROOT", root):
                errors = []
                validator.validate_article_history(article, original, [record], errors)
            self.assertIn("stale or non-matching citation review record 'example-citation-fix'", errors[1])

    def test_captured_tree_rejects_rebaselined_front_matter_routes_and_presentation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            post = root / "content/posts/example.md"
            layout = root / "layouts/example.html"
            style = root / "assets/css/extended/example.css"
            post.parent.mkdir(parents=True)
            layout.parent.mkdir(parents=True)
            style.parent.mkdir(parents=True)
            original_post = (
                '+++\ntitle = "Established"\ndate = 2026-01-02T09:00:00-07:00\n'
                'draft = false\nslug = "established"\nhideSummary = true\nShowToc = false\n'
                '+++\n\nEstablished prose.\n'
            )
            post.write_text(original_post, encoding="utf-8")
            layout.write_text("established layout\n", encoding="utf-8")
            style.write_text("established style\n", encoding="utf-8")
            (root / "hugo.toml").write_text("title = 'Established'\n", encoding="utf-8")
            front, body = validator.split_post(post)
            article = {
                "source": "content/posts/example.md",
                "route": "/established/",
                "frontMatter": validator.json_front_matter(front),
            }
            baseline = {
                "capturedFrom": "a" * 40,
                "paperModCommit": "b" * 40,
                "presentationFileSets": {
                    "layouts": ["layouts/example.html"],
                    "extendedCss": ["assets/css/extended/example.css"],
                },
                "homeListing": [{
                    "route": "/established/", "title": "Established", "date": "Jan 2, 2026",
                }],
                "articles": [article],
            }
            historical = {
                "content/posts/example.md": original_post.encode(),
                "layouts/example.html": layout.read_bytes(),
                "assets/css/extended/example.css": style.read_bytes(),
                "hugo.toml": (root / "hugo.toml").read_bytes(),
            }

            # Rebaseline every editable fixture alongside the changes. History must still win.
            post.write_text(original_post.replace("Established\"", "Changed\"", 1), encoding="utf-8")
            changed_front, _ = validator.split_post(post)
            article["frontMatter"] = validator.json_front_matter(changed_front)
            baseline["homeListing"][0]["title"] = "Changed"
            article["route"] = "/changed-route/"
            layout.write_text("changed layout\n", encoding="utf-8")
            (root / "hugo.toml").write_text("title = 'Changed'\n", encoding="utf-8")

            def fake_run(command, **_kwargs):
                if command[1:4] == ["ls-tree", "-r", "--name-only"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="assets/css/extended/example.css\nlayouts/example.html\n",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"160000 commit {'b' * 40}\tthemes/PaperMod\n",
                )

            def fake_captured(_commit, relative, _errors):
                return historical[relative]

            errors: list[str] = []
            with mock.patch.object(validator, "ROOT", root), mock.patch.object(
                validator, "captured_file", side_effect=fake_captured
            ), mock.patch.object(validator.subprocess, "run", side_effect=fake_run):
                validator.validate_preservation_history(
                    baseline,
                    {"records": [
                        {"id": "initial", "kind": "baseline-capture"},
                        {
                            "id": "orphaned-citation-review",
                            "kind": "citation-reconciliation",
                            "article": "content/posts/not-established.md",
                            "citationIndex": 1,
                            "before": "https://old.example/source",
                            "after": "https://verified.example/source",
                            "reason": "A claimed correction must target an established article.",
                            "verificationEvidence": ["Publisher record checked."],
                            "proseArgumentRoutePresentationUnchanged": True,
                        },
                    ]},
                    errors,
                )
            self.assertIn(
                "stale or non-matching citation review record "
                "'orphaned-citation-review': content/posts/not-established.md",
                errors,
            )
            self.assertIn(
                "front matter differs from captured history without exact review: content/posts/example.md",
                errors,
            )
            self.assertIn("route identity differs from captured history: content/posts/example.md", errors)
            self.assertIn("home listing routes, titles, dates, or order differ from captured history", errors)
            self.assertIn("protected presentation differs from captured history: layouts/example.html", errors)
            self.assertIn("Hugo configuration differs from captured history", errors)

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
                "presentationFileSets": {
                    "extendedCss": ["assets/css/extended/layout.css"],
                    "layouts": ["layouts/partials/header.html"],
                },
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
            # New files are presentation changes even though no baseline hash exists for them.
            (root / "layouts/zz-unapproved.html").write_text("override\n", encoding="utf-8")
            (root / "assets/css/extended/aa-unapproved.css").write_text("body {}\n", encoding="utf-8")
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
            self.assertIn(
                "unexpected presentation override: assets/css/extended/aa-unapproved.css", errors
            )
            self.assertIn("unexpected presentation override: layouts/zz-unapproved.html", errors)
            self.assertIn("Hugo setting baseURL is missing; expected 'https://example.invalid/'", errors)
            self.assertIn(
                "essay prose segment 2 changed without baseline review: content/posts/example.md", errors
            )


class RenderedPreservationTests(unittest.TestCase):
    @staticmethod
    def _write_smoke_fixture(destination: Path) -> dict[str, object]:
        route = "/established-route/"
        home_html = """<html><body><header class="header"><nav class="header-nav"></nav><button id="theme-toggle" class="theme-toggle"></button></header><main><header class="page-header"></header><ul class="paper-list"><li class="paper-list-item"><a href="/established-route/">Established title</a><span class="paper-list-date">Jan 2, 2026</span></li></ul></main><footer class="footer site-footer"></footer></body></html>"""
        article_html = """<html><body><header class="header"><nav class="header-nav"></nav><button id="theme-toggle" class="theme-toggle"></button></header><main><article class="post-single"><header class="post-header"><h1 class="post-title entry-hint-parent">Established title</h1></header><div class="post-content md-content">Established prose.</div></article></main><footer class="footer site-footer"></footer></body></html>"""
        article_path = destination / route.strip("/") / "index.html"
        article_path.parent.mkdir(parents=True)
        (destination / "index.html").write_text(home_html, encoding="utf-8")
        article_path.write_text(article_html, encoding="utf-8")
        home = validator.parse_html(destination / "index.html")
        article = validator.parse_html(article_path)
        return {
            "homeListing": [{
                "route": route, "title": "Established title", "date": "Jan 2, 2026",
            }],
            "articles": [{
                "route": route,
                "frontMatter": {"title": "Established title"},
                "renderedProseSha256": validator.digest("Established prose."),
                "citationDestinations": [],
                "renderedStructureSha256": validator.digest("\n".join(article.structure)),
            }],
            "renderedContract": {
                "homeStructureSha256": validator.digest("\n".join(home.structure)),
                "homeMarkers": ["<header.page-header>", "<ul.paper-list>", "<li.paper-list-item>"],
                "articleMarkers": [
                    "<article.post-single>", "<header.post-header>",
                    "<h1.entry-hint-parent.post-title>", "<div.md-content.post-content>",
                ],
                "siteMarkers": [
                    "<header.header>", "<nav.header-nav>",
                    "<button#theme-toggle.theme-toggle>", "<footer.footer.site-footer>",
                ],
            },
        }

    def test_route_smoke_contract_rejects_title_listing_and_article_shell_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            baseline = self._write_smoke_fixture(destination)
            errors: list[str] = []
            validator.validate_rendered(baseline, destination, errors)
            self.assertEqual(errors, [])

            article_path = destination / "established-route" / "index.html"
            original_article = article_path.read_text(encoding="utf-8")
            article_path.write_text(
                original_article.replace("Established title", "Changed title"), encoding="utf-8"
            )
            errors = []
            validator.validate_rendered(baseline, destination, errors)
            self.assertIn("rendered title changed at /established-route/", errors)

            shell_mutations = [
                ('<header class="header">', "<header>", "<header.header>"),
                ('<nav class="header-nav">', "<nav>", "<nav.header-nav>"),
                (
                    '<button id="theme-toggle" class="theme-toggle"></button>',
                    "<button></button>",
                    "<button#theme-toggle.theme-toggle>",
                ),
                ('<footer class="footer site-footer">', "<footer>", "<footer.footer.site-footer>"),
            ]
            for established, changed, marker in shell_mutations:
                with self.subTest(marker=marker):
                    article_path.write_text(
                        original_article.replace(established, changed), encoding="utf-8"
                    )
                    errors = []
                    validator.validate_rendered(baseline, destination, errors)
                    self.assertIn(
                        f"article route /established-route/ missing header/footer/theme marker {marker}",
                        errors,
                    )

            article_path.write_text(original_article, encoding="utf-8")
            home_path = destination / "index.html"
            home_path.write_text(
                home_path.read_text(encoding="utf-8").replace(
                    "Established title</a>", "Changed listing title</a>"
                ),
                encoding="utf-8",
            )
            errors = []
            validator.validate_rendered(baseline, destination, errors)
            self.assertIn(
                "home listing presence, title, date, or established post ordering changed", errors
            )

    def test_empty_rendered_pages_are_rejected_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            baseline = self._write_smoke_fixture(destination)
            (destination / "established-route" / "index.html").write_text(" \n", encoding="utf-8")
            errors: list[str] = []
            validator.validate_rendered(baseline, destination, errors)
            self.assertIn(
                "article route /established-route/ rendered an empty HTTP response (index.html)", errors
            )

            (destination / "index.html").write_text("\n", encoding="utf-8")
            errors = []
            validator.validate_rendered(baseline, destination, errors)
            self.assertEqual(errors, [
                "home route / rendered an empty HTTP response (index.html)"
            ])

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
    def test_malformed_baseline_reports_sorted_schema_errors_without_traceback(self) -> None:
        malformed = {
            "schema": "hugo-preservation-baseline/v1",
            "paperModCommit": 42,
            "presentationFileSets": {"extendedCss": "not-an-array"},
            "protectedFiles": [],
            "hugoConfiguration": {},
            "homeListing": [{"route": "/example/", "title": False}],
            "articles": [{"source": "content/posts/example.md", "route": []}],
            "renderedContract": None,
        }
        with tempfile.TemporaryDirectory() as temporary:
            baseline_path = Path(temporary) / "malformed.json"
            baseline_path.write_text(json.dumps(malformed), encoding="utf-8")
            result = subprocess.run(
                [
                    "python3", "scripts/validate_preservation_baseline.py", "--source-only",
                    "--baseline", str(baseline_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=30,
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.startswith("Preservation baseline schema is invalid:\n"))
        findings = result.stderr.splitlines()[1:-1]
        self.assertEqual(findings, sorted(set(findings)))
        self.assertIn("- $.articles[0].frontMatter: missing required field", findings)
        self.assertIn("- $.homeListing[0].date: missing required field", findings)
        self.assertIn("- $.paperModCommit: expected string, got number", findings)
        self.assertIn("- $.renderedContract: expected object, got null", findings)

    def test_non_object_baseline_has_actionable_diagnostic(self) -> None:
        self.assertEqual(
            validator.validate_baseline_schema([]),
            ["$: expected object, got array"],
        )

    def test_full_cli_orchestration_is_offline_with_fixture_build_dependencies(self) -> None:
        # Unit discovery must work in a fresh worktree, where the PaperMod
        # gitlink exists but its worktree has not been initialized. The real
        # checkout/toolchain gate remains part of the separately run CLI.
        real_run = subprocess.run

        def fixture_run(command, **kwargs):
            if command[0] == "hugo":
                return subprocess.CompletedProcess(command, 0, "", "")
            return real_run(command, **kwargs)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["validate_preservation_baseline.py"]), \
                mock.patch.object(validator, "validate_hugo_toolchain", return_value=True) as toolchain, \
                mock.patch.object(validator, "validate_theme_checkout", return_value=True) as theme, \
                mock.patch.object(validator, "validate_rendered") as rendered, \
                mock.patch.object(validator.subprocess, "run", side_effect=fixture_run), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = validator.main()

        self.assertEqual(status, 0, stderr.getvalue())
        self.assertIn("Preservation baseline passed", stdout.getvalue())
        toolchain.assert_called_once_with("0.162.0", True, mock.ANY)
        theme.assert_called_once_with("154d006e0182dfc7da38008323976b02e6bfab4a", mock.ANY)
        rendered.assert_called_once()


if __name__ == "__main__":
    unittest.main()
