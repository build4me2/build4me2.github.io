from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import validate_content as validator


class ContentValidatorTests(unittest.TestCase):
    def test_repository_findings_are_only_evidence_blocked_citations(self) -> None:
        findings = validator.validate()
        self.assertEqual(
            ["(Lewis, 2014)", "(Lobel, 2017)"],
            sorted(item.message for item in findings if item.code == "unresolved_citation"),
        )
        self.assertTrue(all(item.code == "unresolved_citation" for item in findings))

    def test_audit_is_deterministic_and_preserves_destination_order(self) -> None:
        path = validator.POSTS / "freedom-of-speech-safe-online.md"
        _, body = validator.split_post(path)
        first = validator.expected_audit(path, body)
        second = validator.expected_audit(path, body)
        self.assertEqual(first, second)
        self.assertEqual("pass", first["status"])
        self.assertEqual(0, first["summary"]["blockingCount"])

    def test_unsafe_links_and_retired_shortcodes_are_detected(self) -> None:
        self.assertFalse(validator.safe_destination("javascript:alert(1)"))
        self.assertFalse(validator.safe_destination("https://user:secret@example.com/"))
        self.assertTrue(validator.safe_destination("https://example.com/source"))
        self.assertIsNotNone(validator.SHORTCODE.search('{{< cite "x" "https://example.com" >}}'))

    def test_front_matter_parser_rejects_missing_delimiters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.md"
            path.write_text("title = 'bad'\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "opening TOML"):
                validator.split_post(path)

    def test_committed_audits_are_canonical_json_objects(self) -> None:
        files = sorted(validator.AUDITS.glob("*.json"))
        self.assertEqual(4, len(files))
        for path in files:
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("repository-citation-audit/v1", value["schema"])


class CitationShortcodeRetirementTests(unittest.TestCase):
    def test_existing_posts_do_not_invoke_dormant_shortcodes(self) -> None:
        for path in sorted(validator.POSTS.glob("*.md")):
            self.assertIsNone(validator.SHORTCODE.search(path.read_text(encoding="utf-8")), path)

    def test_normal_render_path_does_not_render_citation_partial(self) -> None:
        partial = (validator.ROOT / "layouts/partials/extend_post_content.html").read_text(encoding="utf-8")
        self.assertNotIn("partial \"citations.html\"", partial)
        archetype = (validator.ROOT / "archetypes/default.md").read_text(encoding="utf-8")
        self.assertIn("Do not use citation shortcodes", archetype)


if __name__ == "__main__":
    unittest.main()
