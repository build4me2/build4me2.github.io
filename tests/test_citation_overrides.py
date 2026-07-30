from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name, file in (("converter", "post_from_file.py"), ("overrides", "citation_overrides.py")):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    globals()[name] = module


class CitationOverrideTests(unittest.TestCase):
    def record(self, **changes):
        raw = {
            "id": "review-1", "documentIdentity": "sha256:" + "a" * 64,
            "citationIdentity": "numeric:7", "intent": "resolve-citation-destination",
            "destination": "https://source.example/item", "reviewer": "A. Reviewer",
            "evidenceText": "unsupported claim", "rationale": "PDF margin link verifies the claim",
        }
        raw.update(changes)
        return raw

    def test_committed_override_document_is_valid_and_fully_consumable(self):
        records = overrides.load_citation_overrides(ROOT / "citation-overrides.json")
        results = {}
        for path in sorted((ROOT / "content" / "posts").glob("*.md")):
            raw_bytes = path.read_bytes()
            raw_text = raw_bytes.decode("utf-8", errors="strict")
            results[overrides.document_identity(raw_bytes)] = converter.match_citation_candidates(
                raw_text, source=path.relative_to(ROOT).as_posix()
            )
        applied = overrides.apply_citation_overrides(results, records)
        self.assertEqual(len(records), len(applied.uses))

    def test_schema_rejects_wildcards_duplicates_conflicts_and_missing_evidence(self):
        base = self.record()
        bad_documents = [
            {"schema": "citation-overrides/v1", "overrides": [self.record(citationIdentity="numeric:*")]},
            {"schema": "citation-overrides/v1", "overrides": [{k: v for k, v in base.items() if k != "evidenceText"}]},
            {"schema": "citation-overrides/v1", "overrides": [base, dict(base)]},
            {"schema": "citation-overrides/v1", "overrides": [base, self.record(id="review-2", destination="https://other.example/")]},
        ]
        for document in bad_documents:
            with self.subTest(document=document), self.assertRaises(overrides.OverrideValidationError):
                overrides.validate_override_document(document)

    def test_duplicate_json_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"
            path.write_text('{"schema":"citation-overrides/v1","schema":"citation-overrides/v1","overrides":[]}', encoding="utf-8")
            with self.assertRaisesRegex(overrides.OverrideValidationError, "duplicate JSON member"):
                overrides.load_citation_overrides(path)

    def test_override_is_consumed_once_without_hiding_unrelated_failure(self):
        text = (
            "An unsupported claim.[7] Another unsupported claim.[8]\n"
            "Loose https://source.example/item\n"
            "References\n[1] https://known.example/source\n"
        )
        result = converter.match_citation_candidates(text)
        identity = overrides.document_identity(text)
        raw = self.record(documentIdentity=identity)
        record = overrides.validate_override_document({"schema": "citation-overrides/v1", "overrides": [raw]})
        applied = overrides.apply_citation_overrides({identity: result}, record)
        self.assertEqual(1, len(applied.uses))
        states = {(d.subject_type, d.subject_id): d.status for d in applied.results[identity].dispositions}
        self.assertEqual("matched", states[("citation", "citation-0001")])
        self.assertEqual("unresolved", states[("citation", "citation-0002")])
        self.assertTrue(applied.blocking)

    def test_stale_and_already_matched_overrides_fail(self):
        text = "Claim.[1]\nReferences\n[1] https://source.example/item\n"
        result = converter.match_citation_candidates(text)
        identity = overrides.document_identity(text)
        raw = self.record(documentIdentity=identity, citationIdentity="numeric:1", evidenceText="Claim")
        records = overrides.validate_override_document({"schema": "citation-overrides/v1", "overrides": [raw]})
        with self.assertRaisesRegex(overrides.OverrideValidationError, "already matched"):
            overrides.apply_citation_overrides({identity: result}, records)
        stale = records[0]._replace(document_identity="sha256:" + "b" * 64)
        with self.assertRaisesRegex(overrides.OverrideValidationError, "stale"):
            overrides.apply_citation_overrides({identity: result}, [stale])


if __name__ == "__main__":
    unittest.main()
