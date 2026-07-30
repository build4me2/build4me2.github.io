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


if __name__ == "__main__":
    unittest.main()
