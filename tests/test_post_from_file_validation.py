from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

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


class MetadataAndOutputValidationTests(unittest.TestCase):
    def assert_category(self, category: str, action) -> None:
        with self.assertRaises(converter.IngestionError) as caught:
            action()
        self.assertEqual(category, caught.exception.category)

    def test_validates_title_slug_and_date(self) -> None:
        self.assert_category("invalid_title", lambda: converter.validate_title("  "))
        self.assert_category("invalid_slug", lambda: converter.validate_slug("../escape"))
        self.assert_category("invalid_slug", lambda: converter.validate_slug("Two-Words"))
        self.assert_category("invalid_date", lambda: converter.validate_date("2026-02-30"))
        self.assert_category("invalid_date", lambda: converter.validate_date("2026-01-01T09:00:00"))
        self.assertEqual("safe-post", converter.validate_slug("safe-post"))
        self.assertEqual("2026-01-01T09:00:00Z", converter.validate_date("2026-01-01T09:00:00Z"))

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
