from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("citation_converter", ROOT / "scripts/post_from_file.py")
assert SPEC and SPEC.loader
converter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = converter
SPEC.loader.exec_module(converter)


class CitationCandidateTests(unittest.TestCase):
    def test_recovers_every_explicit_candidate_with_provenance(self) -> None:
        text = (
            "Body https://Example.COM/article?x=1.\n"
            "Wrapped https://example.org/research/\nresult?q=yes\n"
            "doi:10.5555/ABC.Def and bare 10.1000/XYZ-123.\n"
            "References\n"
            "Resolver https://doi.org/10.7777/Mixed.Case).\n"
            "Page https://pages.example/item/\x0ccontinued\n"
            "10.8888/\x0cPAGE.DOI\n"
        )
        annotations = [
            ("Source label", "HTTPS://Annotations.Example/Source", 2),
            ("same visible destination", "https://example.com/article?x=1"),
        ]

        candidates = converter.extract_citation_candidates(text, annotations, source="fixture.pdf")

        self.assertEqual(
            [
                "pdf_annotation", "pdf_annotation", "visible_url", "visible_url_wrapped",
                "doi_prefixed", "bare_doi", "doi_url", "visible_url_wrapped",
                "bare_doi_wrapped",
            ],
            [candidate.extraction_method for candidate in candidates],
        )
        self.assertEqual("https://annotations.example/Source", candidates[0].normalized_destination)
        self.assertEqual("https://doi.org/10.7777/mixed.case", candidates[6].normalized_destination)
        self.assertIn("\x0c", candidates[7].raw_evidence)
        self.assertEqual("references", candidates[-1].source_location.section)
        self.assertEqual("fixture.pdf", candidates[0].source_location.source)
        self.assertEqual(2, candidates[0].source_location.page)
        self.assertIn("raw_evidence", candidates[0].to_dict())

    def test_does_not_infer_destinations_from_citation_prose(self) -> None:
        text = "Smith (2020), Important Research, Journal of Examples.\nexample.com/no-scheme"
        self.assertEqual([], converter.extract_citation_candidates(text))

    def test_complete_url_is_not_joined_to_following_prose(self) -> None:
        candidates = converter.extract_citation_candidates(
            "See https://example.com/complete\nordinary prose follows."
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual("https://example.com/complete", candidates[0].normalized_destination)
        self.assertEqual("visible_url", candidates[0].extraction_method)

    def test_annotation_and_visible_occurrences_remain_distinct_evidence(self) -> None:
        candidates = converter.extract_citation_candidates(
            "https://example.com/source", [("label", "https://example.com/source")]
        )
        self.assertEqual(2, len(candidates))
        self.assertEqual({"pdf_annotation", "visible_url"}, {item.extraction_method for item in candidates})

    def test_exact_numeric_and_author_year_rules_match_without_guessing(self) -> None:
        text = (
            "A numeric claim.[1]\n"
            "Smith (2020) made another claim.\n"
            "References\n"
            "[1] Numeric source https://numeric.example/source\n"
            "Smith, J. (2020). Author source https://author.example/source\n"
        )
        result = converter.match_citation_candidates(text, source="paper.txt")

        self.assertEqual(
            ["numeric:1", "author-year:smith:2020"],
            [citation.identity for citation in result.citations],
        )
        citation_states = [
            item.status for item in result.dispositions if item.subject_type == "citation"
        ]
        self.assertEqual(["matched", "matched"], citation_states)
        self.assertFalse(result.blocking)
        self.assertEqual("sentence-0001", result.sentences[0].record_id)
        self.assertEqual("reference-0001", result.references[0].record_id)

    def test_parenthetical_author_year_lists_and_numeric_lists_are_separate_records(self) -> None:
        sentences, citations, _ = converter.parse_citation_records(
            "Prior work (Smith, 2020; Jones, 2021) supports both claims [2, 3]."
        )
        self.assertEqual(1, len(sentences))
        self.assertEqual(
            ["author-year:smith:2020", "author-year:jones:2021", "numeric:2", "numeric:3"],
            [citation.identity for citation in citations],
        )

    def test_ambiguous_duplicate_identity_and_conflicting_destinations_are_not_guessed(self) -> None:
        text = (
            "Smith (2020) supports this.\n"
            "References\n"
            "Smith, J. (2020). One https://one.example/source\n"
            "Smith, R. (2020). Two https://two.example/source\n"
        )
        result = converter.match_citation_candidates(text)
        by_subject = {(item.subject_type, item.subject_id): item for item in result.dispositions}

        self.assertEqual("ambiguous", by_subject[("citation", "citation-0001")].status)
        self.assertIsNone(by_subject[("citation", "citation-0001")].matched_id)
        self.assertEqual("duplicate_identity", by_subject[("reference", "reference-0001")].status)
        self.assertTrue(result.blocking)

    def test_duplicate_candidates_and_reference_destination_conflicts_are_explicit(self) -> None:
        duplicate_text = (
            "References\n"
            "[1] https://same.example/source https://same.example/source\n"
        )
        duplicate = converter.match_citation_candidates(duplicate_text)
        candidate_states = [
            item.status for item in duplicate.dispositions if item.subject_type == "candidate"
        ]
        self.assertEqual(["duplicate_candidate", "duplicate_candidate"], candidate_states)

        conflict_text = "References\n[1] https://one.example/a https://two.example/b\n"
        conflict = converter.match_citation_candidates(conflict_text)
        reference_state = next(
            item for item in conflict.dispositions if item.subject_type == "reference"
        )
        self.assertEqual("conflicting_destination", reference_state.status)
        self.assertIsNone(reference_state.matched_id)

    def test_zero_match_is_unresolved_and_stable_across_runs(self) -> None:
        text = "An unsupported claim.[7]\nReferences\n[1] https://one.example/source\n"
        first = converter.match_citation_candidates(text)
        second = converter.match_citation_candidates(text)
        self.assertEqual(first, second)
        citation = next(item for item in first.dispositions if item.subject_type == "citation")
        self.assertEqual("unresolved", citation.status)
        self.assertIsNone(citation.matched_id)


if __name__ == "__main__":
    unittest.main()
