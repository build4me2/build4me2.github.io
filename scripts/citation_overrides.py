#!/usr/bin/env python3
"""Strict, deterministic handling of reviewed citation overrides.

Overrides are deliberately a last-mile review mechanism, not a fuzzy matcher.
They may connect one blocking citation record to one already-recovered candidate;
they never remove or broadly waive other blocking dispositions.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence
from urllib.parse import urlsplit, urlunsplit

SCHEMA_NAME = "citation-overrides/v1"
_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_DOCUMENT_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CITATION_ID = re.compile(r"(?:numeric:[1-9][0-9]{0,2}|author-year:[a-z0-9'’-]+:(?:18|19|20)[0-9]{2}[a-z]?|destination:https?://\S+)")
_ALLOWED_ROOT = {"schema", "overrides"}
_ALLOWED_OVERRIDE = {
    "id", "documentIdentity", "citationIdentity", "intent", "destination",
    "reviewer", "evidenceText", "evidenceSource", "rationale",
}


class OverrideValidationError(ValueError):
    """A stable, actionable override validation failure."""


class CitationOverride(NamedTuple):
    override_id: str
    document_identity: str
    citation_identity: str
    intent: str
    destination: str
    reviewer: str
    evidence_text: str | None
    evidence_source: str | None
    rationale: str


class OverrideUse(NamedTuple):
    override_id: str
    document_identity: str
    citation_id: str
    candidate_id: str
    destination: str


class OverrideApplicationResult(NamedTuple):
    results: Mapping[str, Any]
    uses: tuple[OverrideUse, ...]

    @property
    def blocking(self) -> bool:
        return any(result.blocking for result in self.results.values())


def document_identity(raw_text: str | bytes) -> str:
    """Return the content identity used by committed override records."""
    data = raw_text if isinstance(raw_text, bytes) else raw_text.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise OverrideValidationError(f"duplicate JSON member {key!r}")
        value[key] = item
    return value


def _text(value: Any, field: str, index: int, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OverrideValidationError(f"override {index}: {field} must be a non-empty string")
    if value != value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise OverrideValidationError(f"override {index}: {field} has invalid whitespace, length, or controls")
    return value


def _destination(value: Any, index: int) -> str:
    raw = _text(value, "destination", index, maximum=2048)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise OverrideValidationError(f"override {index}: destination is malformed") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname != parsed.hostname.lower():
        raise OverrideValidationError(
            f"override {index}: destination must be an exact normalized HTTP(S) URL with lowercase host"
        )
    host = parsed.hostname
    if "." not in host and host != "localhost":
        raise OverrideValidationError(f"override {index}: destination host is invalid")
    netloc = host
    if parsed.username is not None:
        credentials = parsed.username + ((":" + parsed.password) if parsed.password is not None else "")
        netloc = credentials + "@" + netloc
    if port is not None:
        netloc += f":{port}"
    normalized = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    if normalized != raw:
        raise OverrideValidationError(f"override {index}: destination is not in exact normalized form")
    return raw


def validate_override_document(value: Any) -> tuple[CitationOverride, ...]:
    """Validate a decoded override document with no coercion or ignored fields."""
    if not isinstance(value, dict) or set(value) != _ALLOWED_ROOT:
        raise OverrideValidationError("override document must contain exactly 'schema' and 'overrides'")
    if value["schema"] != SCHEMA_NAME:
        raise OverrideValidationError(f"schema must be exactly {SCHEMA_NAME!r}")
    if not isinstance(value["overrides"], list):
        raise OverrideValidationError("overrides must be an array")

    records: list[CitationOverride] = []
    ids: set[str] = set()
    targets: dict[tuple[str, str], str] = {}
    for index, raw in enumerate(value["overrides"], 1):
        if not isinstance(raw, dict) or set(raw) - _ALLOWED_OVERRIDE:
            unknown = sorted(set(raw) - _ALLOWED_OVERRIDE) if isinstance(raw, dict) else []
            detail = f"; unknown members: {', '.join(unknown)}" if unknown else ""
            raise OverrideValidationError(f"override {index}: must be an object with only schema-defined members{detail}")
        required = _ALLOWED_OVERRIDE - {"evidenceText", "evidenceSource"}
        missing = sorted(required - set(raw))
        if missing:
            raise OverrideValidationError(f"override {index}: missing required members: {', '.join(missing)}")
        if ("evidenceText" in raw) == ("evidenceSource" in raw):
            raise OverrideValidationError(
                f"override {index}: provide exactly one of evidenceText or evidenceSource"
            )
        override_id = _text(raw["id"], "id", index, maximum=128)
        if _ID.fullmatch(override_id) is None:
            raise OverrideValidationError(f"override {index}: id is not a stable lowercase identifier")
        if override_id in ids:
            raise OverrideValidationError(f"duplicate override id {override_id!r}")
        ids.add(override_id)
        doc = _text(raw["documentIdentity"], "documentIdentity", index)
        if _DOCUMENT_ID.fullmatch(doc) is None:
            raise OverrideValidationError(f"override {index}: documentIdentity must be sha256:<64 lowercase hex>")
        citation = _text(raw["citationIdentity"], "citationIdentity", index)
        if _CITATION_ID.fullmatch(citation) is None:
            raise OverrideValidationError(f"override {index}: citationIdentity is malformed or not exact")
        intent = _text(raw["intent"], "intent", index)
        if intent != "resolve-citation-destination":
            raise OverrideValidationError(f"override {index}: unsupported reviewer intent {intent!r}")
        destination = _destination(raw["destination"], index)
        reviewer = _text(raw["reviewer"], "reviewer", index, maximum=300)
        rationale = _text(raw["rationale"], "rationale", index)
        evidence_text = _text(raw["evidenceText"], "evidenceText", index) if "evidenceText" in raw else None
        evidence_source = _text(raw["evidenceSource"], "evidenceSource", index, maximum=2048) if "evidenceSource" in raw else None
        target = (doc, citation)
        if target in targets:
            kind = "conflicting" if targets[target] != destination else "duplicate"
            raise OverrideValidationError(f"{kind} overrides for document/citation target {doc}/{citation}")
        targets[target] = destination
        records.append(CitationOverride(
            override_id, doc, citation, intent, destination, reviewer,
            evidence_text, evidence_source, rationale,
        ))
    return tuple(records)


def load_citation_overrides(path: Path | str) -> tuple[CitationOverride, ...]:
    """Load strict UTF-8 JSON, rejecting duplicate JSON object members."""
    try:
        payload = Path(path).read_text(encoding="utf-8", errors="strict")
        decoded = json.loads(payload, object_pairs_hook=_pairs_without_duplicates)
    except OverrideValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OverrideValidationError(f"cannot load citation overrides: {exc}") from None
    return validate_override_document(decoded)


def apply_citation_overrides(
    results: Mapping[str, Any], overrides: Sequence[CitationOverride]
) -> OverrideApplicationResult:
    """Consume every override exactly once across a complete document result set.

    The mapping keys must be ``document_identity`` values. An override can only
    resolve one currently blocking citation and one unique, currently orphaned
    recovered candidate. Other blocking records are retained, so a narrow review
    cannot conceal an unrelated unresolved/ambiguous candidate or reference.
    """
    updated = dict(results)
    uses: list[OverrideUse] = []
    for override in overrides:
        result = updated.get(override.document_identity)
        if result is None:
            raise OverrideValidationError(f"unused override {override.override_id!r}: document identity is stale")
        citations = [item for item in result.citations if item.identity == override.citation_identity]
        if len(citations) != 1:
            raise OverrideValidationError(
                f"unused override {override.override_id!r}: citation identity matched {len(citations)} records"
            )
        citation = citations[0]
        dispositions = list(result.dispositions)
        citation_positions = [i for i, item in enumerate(dispositions)
                              if item.subject_type == "citation" and item.subject_id == citation.record_id]
        if len(citation_positions) != 1 or dispositions[citation_positions[0]].status == "matched":
            raise OverrideValidationError(f"unused override {override.override_id!r}: citation is absent or already matched")
        candidates = [(i, item) for i, item in enumerate(result.candidates)
                      if item.normalized_destination == override.destination]
        if len(candidates) != 1:
            raise OverrideValidationError(
                f"unused override {override.override_id!r}: destination matched {len(candidates)} candidates"
            )
        candidate_index, _ = candidates[0]
        candidate_id = f"candidate-{candidate_index + 1:04d}"
        candidate_positions = [i for i, item in enumerate(dispositions)
                               if item.subject_type == "candidate" and item.subject_id == candidate_id]
        if len(candidate_positions) != 1 or dispositions[candidate_positions[0]].status != "orphaned":
            raise OverrideValidationError(
                f"conflicting override {override.override_id!r}: destination candidate is not uniquely orphaned"
            )
        if override.evidence_text is not None:
            sentence = next(item for item in result.sentences if item.record_id == citation.sentence_id)
            if override.evidence_text not in sentence.text and override.evidence_text not in citation.raw_evidence:
                raise OverrideValidationError(
                    f"unused override {override.override_id!r}: evidenceText is stale for the cited sentence"
                )

        citation_position = citation_positions[0]
        candidate_position = candidate_positions[0]
        disposition_type = type(dispositions[citation_position])
        dispositions[citation_position] = disposition_type(
            "citation", citation.record_id, "matched", candidate_id,
            f"reviewed override {override.override_id}: {override.rationale}",
        )
        dispositions[candidate_position] = disposition_type(
            "candidate", candidate_id, "matched", citation.record_id,
            f"consumed exactly once by reviewed override {override.override_id}",
        )
        # Recompute only the owning sentence. Every unrelated disposition is
        # preserved byte-for-byte and therefore remains blocking when defective.
        for position, item in enumerate(dispositions):
            if item.subject_type == "sentence" and item.subject_id == citation.sentence_id:
                member_ids = {record.record_id for record in result.citations
                              if record.sentence_id == citation.sentence_id}
                failures = [record for record in dispositions
                            if record.subject_type == "citation"
                            and record.subject_id in member_ids and record.status != "matched"]
                dispositions[position] = disposition_type(
                    "sentence", citation.sentence_id,
                    "unresolved" if failures else "matched", None,
                    "contains unresolved or ambiguous citation records" if failures
                    else "all citation records matched (including reviewed override)",
                )
                break
        updated[override.document_identity] = result._replace(dispositions=tuple(dispositions))
        uses.append(OverrideUse(
            override.override_id, override.document_identity, citation.record_id,
            candidate_id, override.destination,
        ))
    return OverrideApplicationResult(updated, tuple(uses))
