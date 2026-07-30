from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import citation_audit
from scripts import post_from_file as converter

ROOT = Path(__file__).resolve().parents[1]


class CitationAuditTests(unittest.TestCase):
    def test_success_reports_are_deterministic_and_include_equivalent_evidence(self) -> None:
        text = "Claim.[1]\nReferences\n[1] Source https://source.example/item\n"
        result = converter.match_citation_candidates(text, source="paper.txt")
        identity = converter.document_identity(text)
        first = citation_audit.build_audit_report(
            source="paper.txt", source_identity=identity, result=result, raw_text=text
        )
        second = citation_audit.build_audit_report(
            source="paper.txt", source_identity=identity, result=result, raw_text=text
        )

        self.assertEqual(citation_audit.render_json(first), citation_audit.render_json(second))
        self.assertEqual("success", first["summary"]["outcome"])
        self.assertEqual("citation-audit-report/v1", first["schema"])
        human = citation_audit.render_text(first)
        self.assertIn("https://source.example/item", human)
        self.assertIn("visible reference-section text", human)
        self.assertNotIn("timestamp", citation_audit.render_json(first).lower())

    def test_all_blocking_classes_and_operational_errors_are_counted(self) -> None:
        text = (
            "Unresolved.[7]\n"
            "Loose http://localhost/private\n"
            "Broken https://bad.example:invalid/path\n"
        )
        result = converter.match_citation_candidates(text, source="bad.txt")
        report = citation_audit.build_audit_report(
            source="bad.txt", source_identity=converter.document_identity(text),
            result=result, raw_text=text,
            errors=[{"category": "review", "message": "review failed"}],
        )
        statuses = {item["status"] for item in report["dispositions"]}
        self.assertTrue({"unresolved", "orphaned", "suspicious", "malformed"} <= statuses)
        self.assertEqual("failure", report["summary"]["outcome"])
        self.assertGreaterEqual(report["summary"]["blockingCount"], 4)
        self.assertEqual(1, report["summary"]["errorCount"])
        json.loads(citation_audit.render_json(report))

    def test_blocking_audit_cannot_install_a_post(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "paper.txt"
            posts = root / "posts"
            posts.mkdir()
            source.write_text("An unsupported claim.[7]\n", encoding="utf-8")
            destination = posts / "paper.md"
            real_validate = converter.validate_output
            with (
                mock.patch.object(
                    converter, "validate_output",
                    side_effect=lambda path: real_validate(path, posts),
                ),
                mock.patch.object(converter, "_atomic_create_post") as install,
            ):
                with self.assertRaisesRegex(converter.IngestionError, "blocking finding") as caught:
                    converter.run([
                        str(source), "--title", "Paper", "--date", "2026-01-01",
                        "--output", str(destination),
                    ])
            self.assertEqual("citation_audit", caught.exception.category)
            self.assertIsNotNone(caught.exception.audit_report)
            self.assertFalse(destination.exists())
            install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
