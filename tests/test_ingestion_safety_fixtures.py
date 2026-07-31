from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ingestion"
SPEC = importlib.util.spec_from_file_location("fixture_converter", ROOT / "scripts/post_from_file.py")
assert SPEC and SPEC.loader
converter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(converter)


class IngestionFixtureTests(unittest.TestCase):
    """Exercise the complete transaction boundary with controlled source files."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ingestion-fixture-")
        self.root = Path(self.temporary.name)
        self.posts = self.root / "content" / "posts"
        self.posts.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_with_fixture(self, source: Path, destination: Path | None = None) -> Path:
        destination = destination or self.posts / "fixture-post.md"
        real_validate = converter.validate_output
        with mock.patch.object(
            converter,
            "validate_output",
            side_effect=lambda path: real_validate(path, self.posts),
        ):
            return converter.run(
                [
                    str(source),
                    "--title",
                    "Fixture Paper",
                    "--date",
                    "2026-01-02",
                    "--output",
                    str(destination),
                ]
            )

    def assert_failure_clean(self, category: str, source: Path, destination: Path | None = None) -> None:
        destination = destination or self.posts / "fixture-post.md"
        before = destination.read_bytes() if destination.exists() else None
        with self.assertRaises(converter.IngestionError) as caught:
            self.run_with_fixture(source, destination)
        self.assertEqual(category, caught.exception.category)
        if before is None:
            self.assertFalse(destination.exists())
        else:
            self.assertEqual(before, destination.read_bytes())
        self.assertEqual([], list(self.posts.rglob(".*.tmp")))

    def test_txt_success_installs_one_complete_post(self) -> None:
        destination = self.run_with_fixture(FIXTURES / "success.txt")
        rendered = destination.read_text(encoding="utf-8")
        self.assertIn('title = "Fixture Paper"', rendered)
        self.assertIn("controlled UTF-8 fixture", rendered)
        self.assertIn("successful ingestion preserves complete output", rendered)
        self.assertEqual([destination], list(self.posts.glob("*.md")))
        self.assertEqual([], list(self.posts.glob(".*.tmp")))

    def test_input_name_replacement_cannot_change_validated_snapshot(self) -> None:
        source = self.root / "raced.txt"
        source.write_text("The validated original body.\n", encoding="utf-8")
        snapshot = converter.validate_input(source)
        try:
            source.unlink()
            source.write_text("An attacker replacement body.\n", encoding="utf-8")
            self.assertEqual("The validated original body.\n", converter.read_input(snapshot))
        finally:
            snapshot.close()

    def test_output_parent_replacement_cannot_redirect_installation(self) -> None:
        parent = self.posts / "bound-parent"
        parent.mkdir()
        output_path = parent / "post.md"
        target = converter.validate_output(output_path, self.posts)
        held_parent = self.posts / "held-parent"
        parent.rename(held_parent)
        parent.mkdir()
        try:
            converter._atomic_create_post(target, "complete validated post\n")
        finally:
            target.close()

        self.assertFalse(output_path.exists(), "replacement directory received the post")
        self.assertEqual("complete validated post\n", (held_parent / "post.md").read_text())
        self.assertEqual([], list(self.posts.rglob(".*.tmp")))

    def test_invalid_encoding_empty_and_unsupported_sources_do_not_publish(self) -> None:
        invalid = self.root / "invalid.txt"
        invalid.write_bytes(b"valid prefix\n\xffinvalid")
        self.assert_failure_clean("encoding_error", invalid)

        empty = self.root / "empty.txt"
        empty.write_bytes(b"")
        self.assert_failure_clean("empty_extraction", empty)

        unsupported = self.root / "paper.docx"
        unsupported.write_text("not supported", encoding="utf-8")
        self.assert_failure_clean("unsupported_input", unsupported)

    def test_symlink_and_parent_traversal_sources_do_not_publish(self) -> None:
        symlink = self.root / "linked.txt"
        symlink.symlink_to(FIXTURES / "success.txt")
        self.assert_failure_clean("unsafe_input", symlink)

        traversal = self.root / "child" / ".." / "success.txt"
        self.assert_failure_clean("unsafe_input", traversal)

    def test_unsafe_output_paths_and_symlinked_parents_do_not_publish(self) -> None:
        outside = self.root / "outside.md"
        self.assert_failure_clean("unsafe_output", FIXTURES / "success.txt", outside)

        traversal = self.posts / "child" / ".." / "escape.md"
        self.assert_failure_clean("unsafe_output", FIXTURES / "success.txt", traversal)

        real_directory = self.posts / "real"
        real_directory.mkdir()
        alias = self.posts / "alias"
        alias.symlink_to(real_directory, target_is_directory=True)
        self.assert_failure_clean("unsafe_output", FIXTURES / "success.txt", alias / "post.md")

    def test_c1_title_fixtures_fail_through_complete_transaction(self) -> None:
        fixtures = json.loads(
            (FIXTURES / "c1-metadata.json").read_text(encoding="utf-8")
        )
        for fixture in fixtures:
            destination = self.posts / f"{fixture['name']}.md"
            real_validate = converter.validate_output
            with self.subTest(name=fixture["name"]), mock.patch.object(
                converter,
                "validate_output",
                side_effect=lambda path: real_validate(path, self.posts),
            ), mock.patch.object(converter, "_atomic_create_post") as atomic_create:
                with self.assertRaises(converter.IngestionError) as caught:
                    converter.run([
                        str(FIXTURES / "success.txt"),
                        "--title", fixture["title"],
                        "--date", "2026-01-02",
                        "--output", str(destination),
                    ])
                self.assertEqual("invalid_title", caught.exception.category)
                atomic_create.assert_not_called()
                self.assertFalse(destination.exists())
                self.assertEqual([], list(self.posts.rglob(".*.tmp")))

    def test_generated_toml_failure_blocks_before_atomic_staging(self) -> None:
        destination = self.posts / "invalid-generated-front-matter.md"
        real_validate = converter.validate_output
        with mock.patch.object(
            converter,
            "validate_output",
            side_effect=lambda path: real_validate(path, self.posts),
        ), mock.patch.object(
            converter, "front_matter", return_value='+++\ntitle = "unterminated\n+++\n\n'
        ), mock.patch.object(converter, "_atomic_create_post") as atomic_create:
            with self.assertRaises(converter.IngestionError) as caught:
                converter.run([
                    str(FIXTURES / "success.txt"),
                    "--title", "Fixture Paper",
                    "--date", "2026-01-02",
                    "--output", str(destination),
                ])
        self.assertEqual("invalid_front_matter", caught.exception.category)
        atomic_create.assert_not_called()
        self.assertFalse(destination.exists())
        self.assertEqual([], list(self.posts.rglob(".*.tmp")))

    def test_invalid_metadata_does_not_publish(self) -> None:
        destination = self.posts / "metadata.md"
        real_validate = converter.validate_output
        invalid_arguments = (
            ("--title", " ", "invalid_title"),
            ("--title", "Fixture", "--slug", "../escape", "invalid_slug"),
            ("--title", "Fixture", "--date", "2026-02-30", "invalid_date"),
        )
        for arguments in invalid_arguments:
            category = arguments[-1]
            argv = [str(FIXTURES / "success.txt"), *arguments[:-1], "--output", str(destination)]
            with self.subTest(category=category), mock.patch.object(
                converter,
                "validate_output",
                side_effect=lambda path: real_validate(path, self.posts),
            ):
                with self.assertRaises(converter.IngestionError) as caught:
                    converter.run(argv)
                self.assertEqual(category, caught.exception.category)
                self.assertFalse(destination.exists())
                self.assertEqual([], list(self.posts.rglob(".*.tmp")))

    def test_overwrite_attempt_preserves_destination_byte_for_byte(self) -> None:
        destination = self.posts / "fixture-post.md"
        destination.write_bytes(b"existing post\x00must remain unchanged")
        self.assert_failure_clean("output_exists", FIXTURES / "success.txt", destination)

    def run_with_link(self, source: Path, value: str, destination: Path) -> Path:
        real_validate = converter.validate_output
        with mock.patch.object(
            converter, "validate_output", side_effect=lambda path: real_validate(path, self.posts)
        ):
            return converter.run([
                str(source), "--title", "Fixture Paper", "--date", "2026-01-02",
                "--output", str(destination), "--link", value,
            ])

    def test_explicit_link_validation_failures_leave_transaction_clean(self) -> None:
        source = self.root / "links.txt"
        source.write_text("A unique cited sentence.\n", encoding="utf-8")
        cases = (
            "A unique cited sentence.=javascript:alert(1)",
            "A unique cited sentence.=https://example.com/\" onclick=\"bad",
            "A unique cited sentence.=https://user:secret@example.com/source",
            "A unique cited sentence.=https://example.com/source\nheader",
            "missing label=https://example.com/source",
            # Historical inet/WHATWG spellings must not disguise loopback or
            # RFC 1918 destinations as apparently qualified DNS hosts.
            "A unique cited sentence.=http://127.1/source",
            "A unique cited sentence.=http://127.1./source",
            "A unique cited sentence.=http://%31%32%37.1/source",
            "A unique cited sentence.=http://0177.0.0.1/source",
            "A unique cited sentence.=http://0x7f.0.0.1/source",
            "A unique cited sentence.=http://0x7f000001/source",
            "A unique cited sentence.=http://2130706433/source",
            "A unique cited sentence.=http://0300.0250.0001.0001/source",
            "A unique cited sentence.=http://3232235777/source",
        )
        for index, value in enumerate(cases):
            destination = self.posts / f"invalid-link-{index}.md"
            with self.subTest(value=value), self.assertRaises(converter.IngestionError) as caught:
                self.run_with_link(source, value, destination)
            self.assertEqual("invalid_link", caught.exception.category)
            self.assertFalse(destination.exists())
            self.assertEqual([], list(self.posts.rglob(".*.tmp")))

    def test_ambiguous_explicit_label_does_not_publish(self) -> None:
        source = self.root / "ambiguous.txt"
        source.write_text("Repeated citation. Repeated citation.\n", encoding="utf-8")
        destination = self.posts / "ambiguous.md"
        with self.assertRaises(converter.IngestionError) as caught:
            self.run_with_link(source, "Repeated citation=https://example.com/source", destination)
        self.assertEqual("invalid_link", caught.exception.category)
        self.assertIn("ambiguous", str(caught.exception))
        self.assertFalse(destination.exists())

    def test_valid_explicit_link_is_normalized_and_safely_embedded(self) -> None:
        source = self.root / "valid-link.txt"
        source.write_text("A unique cited sentence.\n", encoding="utf-8")
        destination = self.posts / "valid-link.md"
        self.run_with_link(
            source,
            "A unique cited sentence.=HTTPS://EXAMPLE.COM:443/source?first=1&second=2",
            destination,
        )
        rendered = destination.read_text(encoding="utf-8")
        self.assertIn(
            '<a href="https://example.com/source?first=1&amp;second=2">A unique cited sentence.</a>',
            rendered,
        )
        self.assertNotIn('onclick=', rendered)
        self.assertEqual([destination], list(self.posts.glob("*.md")))

    @mock.patch.object(converter, "_run_pdf_tool")
    def test_controlled_pdf_success_installs_complete_post(self, tool: mock.Mock) -> None:
        tool.side_effect = [
            b"Pages: 1\n",
            b"Controlled PDF text has substantially more than twenty visible characters.\x0c",
            b"",
            b"<html><body>No annotations in this fixture</body></html>",
        ]
        destination = self.run_with_fixture(FIXTURES / "synthetic.pdf")
        self.assertIn("Controlled PDF text", destination.read_text(encoding="utf-8"))
        self.assertEqual([], list(self.posts.glob(".*.tmp")))

    @mock.patch.object(converter, "_atomic_create_post")
    @mock.patch.object(converter, "_run_pdf_tool")
    def test_missing_embedded_citation_blocks_before_staging_and_leaves_no_debris(
        self, tool: mock.Mock, atomic_create: mock.Mock
    ) -> None:
        tool.side_effect = [
            b"Pages: 1\n",
            b"Controlled PDF body has enough visible characters but no citation link.\x0c",
            b"Page Type URL\n1 Annotation https://example.com/required-source\n",
            b'<html><body><a href="https://example.com/required-source">Reference</a></body></html>',
        ]

        self.assert_failure_clean("missing_citations", FIXTURES / "synthetic.pdf")
        atomic_create.assert_not_called()
        self.assertEqual([], list(self.posts.iterdir()))

    @mock.patch.object(converter, "_atomic_create_post")
    @mock.patch.object(converter, "extract_pdf_links")
    @mock.patch.object(converter, "_run_pdf_tool")
    def test_aggregate_padding_cannot_hide_an_empty_pdf_page_or_stage_output(
        self, tool: mock.Mock, extract_links: mock.Mock, atomic_create: mock.Mock
    ) -> None:
        tool.side_effect = [
            b"Pages: 3\n",
            b"A" * 500 + b"\x0c  \n\x0c" + b"Z" * 500 + b"\x0c",
        ]

        self.assert_failure_clean("partial_extraction", FIXTURES / "synthetic.pdf")
        extract_links.assert_not_called()
        atomic_create.assert_not_called()
        self.assertEqual([], list(self.posts.iterdir()))

    @mock.patch.object(converter, "_run_pdf_tool")
    def test_corrupt_empty_partial_and_invalid_tool_output_do_not_publish(self, tool: mock.Mock) -> None:
        tool.side_effect = converter.IngestionError("tool_failed", "pdfinfo exited with status 1: damaged")
        self.assert_failure_clean("corrupt_pdf", FIXTURES / "synthetic.pdf")

        tool.side_effect = [b"Pages: 1\n", b" \n\x0c"]
        self.assert_failure_clean("empty_extraction", FIXTURES / "synthetic.pdf")

        tool.side_effect = [b"Pages: 3\n", b"Only the first page was emitted.\x0c"]
        self.assert_failure_clean("partial_extraction", FIXTURES / "synthetic.pdf")

        tool.side_effect = [b"Pages: 1\n", b"\xffinvalid UTF-8"]
        self.assert_failure_clean("encoding_error", FIXTURES / "synthetic.pdf")


class PdfToolDoubleTests(unittest.TestCase):
    """Use real subprocesses to verify timeout cleanup and stderr diagnostics."""

    def make_tool(self, directory: Path, body: str) -> Path:
        tool = directory / "controlled-poppler"
        tool.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        tool.chmod(0o755)
        return tool

    def test_missing_tool_and_nonzero_stderr_have_stable_categories(self) -> None:
        with mock.patch.object(converter.shutil, "which", return_value=None):
            with self.assertRaises(converter.IngestionError) as missing:
                converter._run_pdf_tool("pdftotext", [])
        self.assertEqual("missing_tool", missing.exception.category)

        with tempfile.TemporaryDirectory(prefix="tool-double-") as temporary:
            tool = self.make_tool(
                Path(temporary),
                "import sys\nsys.stderr.write('controlled extraction failure\\n')\nsys.exit(23)\n",
            )
            with mock.patch.object(converter.shutil, "which", return_value=str(tool)):
                with self.assertRaises(converter.IngestionError) as failed:
                    converter._run_pdf_tool("pdftotext", [])
            self.assertEqual("tool_failed", failed.exception.category)
            self.assertIn("status 23: controlled extraction failure", str(failed.exception))

    def test_hanging_tool_times_out_and_is_terminated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tool-double-") as temporary:
            root = Path(temporary)
            pid_file = root / "pid"
            tool = self.make_tool(
                root,
                "import os, pathlib, time\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n",
            )
            with (
                mock.patch.object(converter.shutil, "which", return_value=str(tool)),
                mock.patch.object(converter, "PDF_TOOL_TIMEOUT_SECONDS", 0.2),
            ):
                with self.assertRaises(converter.IngestionError) as timed_out:
                    converter._run_pdf_tool("pdftotext", [])
            self.assertEqual("tool_timeout", timed_out.exception.category)
            self.assertTrue(pid_file.exists(), "the controlled tool never started")
            pid = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
