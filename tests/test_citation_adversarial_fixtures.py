from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts import citation_audit
from scripts import post_from_file as converter

FIXTURE = Path(__file__).parent / "fixtures" / "citations" / "adversarial.json"


class AdversarialCitationFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_explicit_evidence_recovery_preserves_provenance_without_redirect_resolution(self) -> None:
        case = self.cases["recovery"]
        candidates = converter.extract_citation_candidates(
            case["text"], case["annotations"], source=case["source"]
        )

        self.assertEqual(case["methods"], [item.extraction_method for item in candidates])
        self.assertEqual(case["destinations"], [item.normalized_destination for item in candidates])
        self.assertEqual(case["annotations"][1][1], candidates[1].raw_evidence)
        self.assertEqual("pdf_annotation", candidates[0].extraction_method)
        self.assertEqual(2, candidates[0].source_location.page)
        self.assertIn("annotation only – résumé", candidates[0].provenance)
        self.assertIn("café", candidates[3].raw_evidence)

    def test_repeated_references_and_duplicate_urls_fail_closed(self) -> None:
        case = self.cases["duplicateReferences"]
        result = converter.match_citation_candidates(case["text"])
        statuses = {item.status for item in result.dispositions}

        self.assertTrue(set(case["requiredStatuses"]) <= statuses)
        self.assertTrue(result.blocking)
        ambiguous = [
            item for item in result.dispositions
            if item.subject_type == "citation" and item.status == "ambiguous"
        ]
        self.assertEqual(2, len(ambiguous))
        self.assertTrue(all(item.matched_id is None for item in ambiguous))

    def test_ambiguous_author_year_is_never_broken_by_source_order(self) -> None:
        case = self.cases["ambiguousAuthorYear"]
        first = converter.match_citation_candidates(case["text"])
        second = converter.match_citation_candidates(case["text"])
        citation = next(item for item in first.dispositions if item.subject_type == "citation")

        self.assertEqual(case["citationStatus"], citation.status)
        self.assertIsNone(citation.matched_id)
        self.assertEqual(first, second)

    def test_orphan_and_missing_reference_states_have_explicit_dispositions(self) -> None:
        case = self.cases["orphansAndMissing"]
        result = converter.match_citation_candidates(case["text"])
        report = citation_audit.build_audit_report(
            source="orphan.txt",
            source_identity=converter.document_identity(case["text"]),
            result=result,
            raw_text=case["text"],
        )
        statuses = {item["status"] for item in report["dispositions"]}

        self.assertTrue(set(case["requiredStatuses"]) <= statuses)
        self.assertEqual("failure", report["summary"]["outcome"])
        self.assertGreater(report["summary"]["blockingCount"], 0)

    def test_malformed_schemes_and_suspicious_domains_are_reported_not_dropped(self) -> None:
        case = self.cases["unsafeLinks"]
        result = converter.match_citation_candidates(case["text"], source="unsafe.txt")
        report = citation_audit.build_audit_report(
            source="unsafe.txt",
            source_identity=converter.document_identity(case["text"]),
            result=result,
            raw_text=case["text"],
        )
        statuses = {item["status"] for item in report["dispositions"]}
        malformed_evidence = {
            item.get("evidence") for item in report["dispositions"]
            if item["status"] == "malformed"
        }

        self.assertTrue(set(case["requiredStatuses"]) <= statuses)
        self.assertIn("ftp://files.example/source", malformed_evidence)
        self.assertEqual("failure", report["summary"]["outcome"])

    def test_numbers_that_are_not_citations_do_not_create_records(self) -> None:
        case = self.cases["falsePositives"]
        _, citations, _ = converter.parse_citation_records(case["text"])
        self.assertEqual(case["expectedCitationCount"], len(citations))

    def test_author_title_and_domain_prose_never_invent_a_destination(self) -> None:
        case = self.cases["noInventedFallback"]
        candidates = converter.extract_citation_candidates(case["text"])
        result = converter.match_citation_candidates(case["text"], candidates)
        statuses = {item.status for item in result.dispositions}

        self.assertEqual(case["expectedCandidateCount"], len(candidates))
        self.assertTrue(set(case["requiredStatuses"]) <= statuses)
        self.assertTrue(result.blocking)


if __name__ == "__main__":
    unittest.main()
