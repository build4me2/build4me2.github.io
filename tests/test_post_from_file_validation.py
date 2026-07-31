from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("post_from_file", ROOT / "scripts/post_from_file.py")
assert SPEC and SPEC.loader
converter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(converter)


class InputValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_category(self, category: str, action) -> None:
        with self.assertRaises(converter.IngestionError) as caught:
            action()
        self.assertEqual(category, caught.exception.category)

    def test_accepts_regular_utf8_text(self) -> None:
        source = self.root / "paper.txt"
        source.write_text("A paper.\n", encoding="utf-8")
        self.assertEqual(source, converter.validate_input(source))

    def test_rejects_missing_unsupported_nonregular_and_symlink_inputs(self) -> None:
        self.assert_category("missing_input", lambda: converter.validate_input(self.root / "gone.txt"))
        unsupported = self.root / "paper.doc"
        unsupported.write_text("paper", encoding="utf-8")
        self.assert_category("unsupported_input", lambda: converter.validate_input(unsupported))
        directory = self.root / "directory.txt"
        directory.mkdir()
        self.assert_category("invalid_input", lambda: converter.validate_input(directory))
        target = self.root / "target.txt"
        target.write_text("paper", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to(target)
        self.assert_category("unsafe_input", lambda: converter.validate_input(link))

    def test_rejects_invalid_utf8_and_nul_anywhere_in_text(self) -> None:
        malformed = self.root / "malformed.txt"
        malformed.write_bytes(b"valid prefix\n" + b"x" * 5000 + b"\xff")
        self.assert_category("encoding_error", lambda: converter.validate_input(malformed))
        nul = self.root / "nul.txt"
        nul.write_bytes(b"x" * 5000 + b"\x00hidden")
        self.assert_category("encoding_error", lambda: converter.validate_input(nul))

    def test_accepts_and_removes_utf8_bom(self) -> None:
        source = self.root / "bom.txt"
        source.write_bytes(b"\xef\xbb\xbfBody text")
        converter.validate_input(source)
        self.assertEqual("Body text", converter.read_input(source))

    def test_rejects_misleading_and_oversized_inputs(self) -> None:
        fake_pdf = self.root / "fake.pdf"
        fake_pdf.write_text("not pdf", encoding="utf-8")
        self.assert_category("misleading_input", lambda: converter.validate_input(fake_pdf))
        renamed_pdf = self.root / "renamed.txt"
        renamed_pdf.write_bytes(b"%PDF-1.7\n")
        self.assert_category("misleading_input", lambda: converter.validate_input(renamed_pdf))
        large = self.root / "large.txt"
        large.write_bytes(b"1234")
        self.assert_category("input_too_large", lambda: converter.validate_input(large, max_bytes=3))

    def test_pdf_snapshot_is_unlinked_and_passed_by_descriptor(self) -> None:
        source = self.root / "paper.pdf"
        original = b"%PDF-1.7\nvalidated bytes\n"
        source.write_bytes(original)
        snapshot = converter.validate_input(source)
        try:
            descriptor_path = snapshot.pdf_path()
            self.assertTrue(str(descriptor_path).startswith("/proc/self/fd/"))
            self.assertIn("(deleted)", converter.os.readlink(descriptor_path))
            source.write_bytes(b"%PDF-1.7\nreplacement\n")
            with mock.patch.object(converter.shutil, "which", return_value="/bin/cat"):
                self.assertEqual(
                    original,
                    converter._run_pdf_tool(
                        "cat", [str(descriptor_path)], pass_fds=snapshot.pdf_pass_fds()
                    ),
                )
        finally:
            self.assertIsNone(snapshot.close())


class PdfExtractionFailureTests(unittest.TestCase):
    def assert_category(self, category: str, action) -> None:
        with self.assertRaises(converter.IngestionError) as caught:
            action()
        self.assertEqual(category, caught.exception.category)

    @mock.patch.object(converter.shutil, "which", return_value=None)
    def test_required_tool_must_exist(self, _which) -> None:
        self.assert_category("missing_tool", lambda: converter._run_pdf_tool("pdftotext", []))

    @mock.patch.object(converter.shutil, "which", return_value="/usr/bin/pdftotext")
    def test_tool_timeout_and_nonzero_exit_are_explicit(self, _which) -> None:
        timed_process = mock.Mock(pid=12345, returncode=-15)
        timed_process.communicate.side_effect = [
            subprocess.TimeoutExpired("pdftotext", 30, stderr=b"stalled"),
            (b"", b"stalled"),
            (b"", b"stalled"),
        ]
        with (
            mock.patch.object(converter.subprocess, "Popen", return_value=timed_process) as popen,
            mock.patch.object(converter.os, "killpg") as kill_group,
        ):
            self.assert_category("tool_timeout", lambda: converter._run_pdf_tool("pdftotext", []))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(
            [
                mock.call(12345, converter.signal.SIGTERM),
                mock.call(12345, converter.signal.SIGKILL),
            ],
            kill_group.call_args_list,
        )

        failed_process = mock.Mock(returncode=7)
        failed_process.communicate.return_value = (b"", b"broken object")
        with mock.patch.object(converter.subprocess, "Popen", return_value=failed_process):
            self.assert_category("tool_failed", lambda: converter._run_pdf_tool("pdftotext", []))

    @mock.patch.object(converter, "_run_pdf_tool")
    def test_unreadable_pdf_is_reported_as_corrupt(self, tool) -> None:
        tool.side_effect = converter.IngestionError("tool_failed", "pdfinfo exited with status 1")
        self.assert_category("corrupt_pdf", lambda: converter.read_input(Path("paper.pdf")))

    @mock.patch.object(converter, "_run_pdf_tool")
    def test_empty_and_partial_pdf_text_are_blocked(self, tool) -> None:
        source = Path("paper.pdf")
        tool.side_effect = [b"Pages: 1\n", b" \n\x0c"]
        self.assert_category("empty_extraction", lambda: converter.read_input(source))
        tool.side_effect = [b"Pages: 3\n", b"This is only one extracted page.\x0c"]
        self.assert_category("partial_extraction", lambda: converter.read_input(source))

    @mock.patch.object(converter, "_run_pdf_tool")
    def test_each_reported_pdf_page_must_be_present_and_substantive(self, tool) -> None:
        source = Path("paper.pdf")
        padded_page = b"A" * 200
        complete_page = b"Z" * 40
        cases = (
            (b"Pages: 3\n", padded_page + b"\x0c" + complete_page + b"\x0c", "page framing"),
            (
                b"Pages: 3\n",
                padded_page + b"\x0c   \n\x0c" + complete_page + b"\x0c",
                "page 2 appears incomplete (0 visible characters",
            ),
            (
                b"Pages: 3\n",
                padded_page + b"\x0cshort\x0c" + complete_page + b"\x0c",
                "page 2 appears incomplete (5 visible characters",
            ),
        )
        for info, extraction, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                tool.side_effect = [info, extraction]
                with self.assertRaises(converter.IngestionError) as caught:
                    converter.read_input(source)
                self.assertEqual("partial_extraction", caught.exception.category)
                self.assertIn(diagnostic, str(caught.exception))

    @mock.patch.object(converter, "_run_pdf_tool")
    def test_missing_pdf_url_annotations_are_blocked(self, tool) -> None:
        tool.side_effect = [
            b"Page Type URL\n1 Annotation https://example.com/source\n",
            b"<html><body>No link recovered</body></html>",
        ]
        self.assert_category(
            "missing_annotations", lambda: converter.extract_pdf_links(Path("paper.pdf"))
        )

    @mock.patch.object(converter, "_run_pdf_tool")
    def test_partial_loss_of_duplicate_pdf_url_annotations_is_blocked(self, tool) -> None:
        tool.side_effect = [
            b"Page Type URL\n"
            b"1 Annotation https://example.com/shared\n"
            b"2 Annotation https://example.com/shared\n",
            b'<html><body><a href="https://example.com/shared">one recovered link</a></body></html>',
        ]
        with self.assertRaises(converter.IngestionError) as caught:
            converter.extract_pdf_links(Path("paper.pdf"))

        self.assertEqual("missing_annotations", caught.exception.category)
        self.assertEqual(
            "pdftohtml omitted 1 URL annotation(s); first missing URL: "
            "https://example.com/shared",
            str(caught.exception),
        )

    def test_all_poppler_passes_share_the_same_inherited_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "paper.pdf"
            source.write_bytes(b"%PDF-1.7\nfixture\n")
            snapshot = converter.validate_input(source)
            try:
                with mock.patch.object(converter, "_run_pdf_tool") as tool:
                    tool.side_effect = [
                        b"Pages: 1\n",
                        b"Twenty visible characters in this page.\x0c",
                        b"",
                        b"<html><body></body></html>",
                    ]
                    converter.read_input(snapshot)
                    converter.extract_pdf_links(snapshot)

                descriptor_paths = {
                    argument
                    for call in tool.call_args_list
                    for argument in call.args[1]
                    if argument.startswith("/proc/self/fd/")
                }
                inherited = {call.kwargs["pass_fds"] for call in tool.call_args_list}
                self.assertEqual(1, len(descriptor_paths))
                self.assertEqual({snapshot.pdf_pass_fds()}, inherited)
            finally:
                snapshot.close()

    def test_unembedded_citation_destinations_are_a_stable_blocking_error(self) -> None:
        links = [
            ("second", "https://z.example/source"),
            ("first", "https://a.example/source"),
            ("duplicate", "https://a.example/source"),
        ]
        with self.assertRaises(converter.IngestionError) as caught:
            converter.check_all_sources_embedded("Body without citations.", links)

        self.assertEqual("missing_citations", caught.exception.category)
        self.assertEqual(
            "2 PDF citation destination(s) are not embedded; first missing URL: "
            "https://a.example/source. Add an exact TEXT=URL --link for every missing citation",
            str(caught.exception),
        )


class SnapshotLifecycleTests(unittest.TestCase):
    def test_cleanup_does_not_override_primary_extraction_error(self) -> None:
        snapshot = mock.Mock()
        snapshot.close.return_value = converter.IngestionError("snapshot_cleanup", "close failed")
        target = mock.Mock(path=Path("unused.md"))
        primary = converter.IngestionError("corrupt_pdf", "primary failure")
        with (
            mock.patch.object(converter, "validate_input", return_value=snapshot),
            mock.patch.object(converter, "validate_output", return_value=target),
            mock.patch.object(converter, "read_input", side_effect=primary),
        ):
            with self.assertRaises(converter.IngestionError) as caught:
                converter.run(["paper.pdf", "--title", "Paper"])
        self.assertIs(primary, caught.exception)

    def test_cleanup_failure_blocks_publication_with_stable_category(self) -> None:
        snapshot = mock.Mock()
        cleanup = converter.IngestionError("snapshot_cleanup", "close failed")
        snapshot.close.return_value = cleanup
        target = mock.Mock(path=Path("unused.md"))
        with (
            mock.patch.object(converter, "validate_input", return_value=snapshot),
            mock.patch.object(converter, "validate_output", return_value=target),
            mock.patch.object(converter, "read_input", return_value="Body text."),
            mock.patch.object(converter, "extract_pdf_links", return_value=[]),
            mock.patch.object(converter, "_atomic_create_post") as create,
        ):
            with self.assertRaises(converter.IngestionError) as caught:
                converter.run(["paper.txt", "--title", "Paper"])
        self.assertIs(cleanup, caught.exception)
        create.assert_not_called()


class AtomicOutputTests(unittest.TestCase):
    def staging_files(self, directory: Path) -> list[Path]:
        return list(directory.glob(".*.tmp"))

    def test_success_installs_exactly_one_complete_post_and_removes_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "post.md"
            post = "+++\ntitle = \"A post\"\n+++\n\nComplete body.\n"

            converter._atomic_create_post(output, post)

            self.assertEqual(post.encode("utf-8"), output.read_bytes())
            self.assertEqual([], self.staging_files(root))

    def test_non_utf8_representable_output_fails_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "post.md"
            with mock.patch.object(converter.os, "open") as open_file:
                with self.assertRaises(converter.IngestionError) as caught:
                    converter._atomic_create_post(output, "invalid \ud800 output")

            self.assertEqual("encoding_error", caught.exception.category)
            self.assertEqual(
                "generated output is not UTF-8-representable at character 8",
                str(caught.exception),
            )
            open_file.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual([], list(root.iterdir()))

    def test_flush_failure_leaves_no_destination_or_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "post.md"
            with mock.patch.object(converter.os, "fsync", side_effect=OSError("disk failure")):
                with self.assertRaises(converter.IngestionError) as caught:
                    converter._atomic_create_post(output, "complete post\n")

            self.assertEqual("output_write", caught.exception.category)
            self.assertFalse(output.exists())
            self.assertEqual([], list(root.iterdir()))

    def test_commit_race_preserves_existing_destination_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "post.md"
            original = b"existing post bytes\x00\xff"
            output.write_bytes(original)

            with self.assertRaises(converter.IngestionError) as caught:
                converter._atomic_create_post(output, "replacement\n")

            self.assertEqual("output_exists", caught.exception.category)
            self.assertEqual(original, output.read_bytes())
            self.assertEqual([output], list(root.iterdir()))

    @mock.patch.object(converter, "_validate_staged_post")
    def test_staged_validation_failure_cleans_up_without_publishing(self, validate) -> None:
        validate.side_effect = converter.IngestionError("output_write", "invalid staged post")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "post.md"
            with self.assertRaises(converter.IngestionError):
                converter._atomic_create_post(output, "complete post\n")

            self.assertFalse(output.exists())
            self.assertEqual([], list(root.iterdir()))

    @mock.patch.object(converter, "_validate_staged_post")
    def test_pre_install_fault_needs_no_cleanup_and_leaves_no_named_inode(self, validate) -> None:
        validate.side_effect = converter.IngestionError("output_write", "invalid staged post")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "post.md"
            with mock.patch.object(
                converter.os, "unlink", side_effect=AssertionError("cleanup must not be attempted")
            ) as unlink:
                with self.assertRaises(converter.IngestionError) as caught:
                    converter._atomic_create_post(output, "complete post\n")

            self.assertEqual("output_write", caught.exception.category)
            unlink.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual([], list(root.iterdir()))

    def test_installation_fault_needs_no_rollback_and_leaves_no_named_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "post.md"
            install_fault = converter.IngestionError("output_write", "controlled install failure")
            with (
                mock.patch.object(converter, "_install_anonymous_file", side_effect=install_fault),
                mock.patch.object(
                    converter.os, "unlink", side_effect=AssertionError("rollback must not be attempted")
                ) as unlink,
            ):
                with self.assertRaises(converter.IngestionError) as caught:
                    converter._atomic_create_post(output, "complete post\n")

            self.assertIs(install_fault, caught.exception)
            unlink.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual([], list(root.iterdir()))

    def test_cleanup_and_rollback_fault_hooks_cannot_affect_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "post.md"
            with mock.patch.object(
                converter.os, "unlink", side_effect=OSError(5, "controlled cleanup failure")
            ) as unlink:
                converter._atomic_create_post(output, "complete post\n")

            unlink.assert_not_called()
            self.assertEqual("complete post\n", output.read_text(encoding="utf-8"))
            self.assertEqual([output], list(root.iterdir()))


class MetadataAndOutputValidationTests(unittest.TestCase):
    def assert_category(self, category: str, action) -> None:
        with self.assertRaises(converter.IngestionError) as caught:
            action()
        self.assertEqual(category, caught.exception.category)

    def test_validates_title_slug_and_date(self) -> None:
        self.assert_category("invalid_title", lambda: converter.validate_title("  "))
        for surrogate in ("\ud800", "\udfff"):
            with self.subTest(surrogate=ascii(surrogate)):
                with self.assertRaises(converter.IngestionError) as caught:
                    converter.validate_title(f"safe{surrogate}title")
                self.assertEqual("invalid_title", caught.exception.category)
                self.assertEqual(
                    "title is not UTF-8-representable at character 4",
                    str(caught.exception),
                )
        for codepoint in (*range(0, 32), *range(127, 160)):
            with self.subTest(codepoint=codepoint):
                self.assert_category(
                    "invalid_title",
                    lambda codepoint=codepoint: converter.validate_title(
                        f"safe{chr(codepoint)}title"
                    ),
                )
        self.assert_category("invalid_slug", lambda: converter.validate_slug("../escape"))
        self.assert_category("invalid_slug", lambda: converter.validate_slug("Two-Words"))
        self.assert_category("invalid_date", lambda: converter.validate_date("2026-02-30"))
        self.assert_category("invalid_date", lambda: converter.validate_date("2026-01-01T09:00:00"))
        self.assertEqual("safe-post", converter.validate_slug("safe-post"))
        self.assertEqual("2026-01-01T09:00:00Z", converter.validate_date("2026-01-01T09:00:00Z"))

    def test_generated_front_matter_is_parsed_as_toml(self) -> None:
        post = converter.front_matter(
            'A "quoted" \\ title', "2026-01-01T09:00:00Z", "safe-post"
        ) + "Body.\n"
        converter.validate_generated_front_matter(post)

        with self.assertRaises(converter.IngestionError) as caught:
            converter.validate_generated_front_matter(
                '+++\ntitle = "unterminated\ndate = 2026-01-01\n+++\n\nBody.\n'
            )
        self.assertEqual("invalid_front_matter", caught.exception.category)

    def test_output_must_be_new_markdown_below_posts_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            posts = root / "content" / "posts"
            posts.mkdir(parents=True)
            destination = posts / "new.md"
            self.assertEqual(destination, converter.validate_output(destination, posts))
            self.assert_category("unsafe_output", lambda: converter.validate_output(root / "escape.md", posts))
            self.assert_category("unsafe_output", lambda: converter.validate_output(posts / "not-markdown.txt", posts))
            destination.write_text("old", encoding="utf-8")
            self.assert_category("output_exists", lambda: converter.validate_output(destination, posts))

    def test_output_rejects_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            posts = root / "posts"
            real = posts / "real"
            real.mkdir(parents=True)
            alias = posts / "alias"
            alias.symlink_to(real, target_is_directory=True)
            self.assert_category("unsafe_output", lambda: converter.validate_output(alias / "post.md", posts))


if __name__ == "__main__":
    unittest.main()
