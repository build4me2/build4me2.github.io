#!/usr/bin/env python3
"""Deterministic, network-free citation audit reports.

The JSON document is the canonical report.  The text renderer is deliberately a
lossless, line-oriented view of the same records so success and failure reports
can be compared and archived without timestamps or machine-specific paths.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

SCHEMA = "citation-audit-report/v1"
REPORT_VERSION = 1
BLOCKING_STATUSES = frozenset({
    "unresolved", "unlinked", "ambiguous", "duplicate_identity",
    "duplicate_candidate", "malformed", "orphaned", "suspicious",
    "conflicting_destination",
})


def _location(value: Any) -> dict[str, Any]:
    return {
        "source": value.source,
        "page": value.page,
        "line": value.line,
        "section": value.section,
        "annotation": value.annotation,
    }


def _suspicious_reason(destination: str) -> str | None:
    """Identify destinations that are valid URLs but unsafe to publish blindly."""
    try:
        parsed = urlsplit(destination)
    except ValueError:
        return "destination cannot be parsed"
    if parsed.username is not None or parsed.password is not None:
        return "destination contains user-information credentials"
    if parsed.hostname == "localhost" or (parsed.hostname or "").endswith(".localhost"):
        return "destination targets localhost"
    if parsed.fragment and re.search(r"[\x00-\x20]", parsed.fragment):
        return "destination fragment contains whitespace or controls"
    return None


def malformed_url_findings(raw_text: str, normalized_raw_evidence: Sequence[str]) -> list[dict[str, Any]]:
    """Report explicit HTTP(S) tokens rejected by conservative extraction."""
    known = {item.replace("\n", "").replace("\r", "").replace("\x0c", "") for item in normalized_raw_evidence}
    findings: list[dict[str, Any]] = []
    page = line = 1
    for page, page_text in enumerate(raw_text.split("\x0c"), 1):
        for line, value in enumerate(page_text.splitlines(), 1):
            for occurrence, match in enumerate(re.finditer(r"(?i)https?://[^\s<>\"'`]+", value), 1):
                token = match.group(0)
                # Extracted evidence may have terminal prose punctuation trimmed.
                if any(token.startswith(item) or item.startswith(token) for item in known):
                    continue
                findings.append({
                    "subjectType": "link", "subjectId": f"malformed-{page:04d}-{line:04d}-{occurrence:04d}",
                    "status": "malformed", "matchedId": None,
                    "reason": "explicit HTTP(S) token was rejected by URL normalization",
                    "evidence": token,
                    "sourceLocation": {"page": page, "line": line},
                })
    return findings


def build_audit_report(
    *, source: str, source_identity: str, result: Any | None = None,
    override_uses: Sequence[Any] = (), errors: Sequence[Mapping[str, str]] = (),
    raw_text: str = "",
) -> dict[str, Any]:
    """Build one canonical JSON-ready report with stable source-order records."""
    candidates: list[dict[str, Any]] = []
    sentences: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []
    if result is not None:
        candidates = [item.to_dict() for item in result.candidates]
        sentences = [{"id": item.record_id, "text": item.text, "source_location": _location(item.source_location)} for item in result.sentences]
        citations = [{
            "id": item.record_id, "sentence_id": item.sentence_id,
            "raw_evidence": item.raw_evidence, "identity": item.identity,
            "form": item.form, "source_location": _location(item.source_location),
        } for item in result.citations]
        references = [{
            "id": item.record_id, "text": item.text, "identity": item.identity,
            "source_locations": [_location(location) for location in item.source_locations],
        } for item in result.references]
        for item in result.dispositions:
            dispositions.append({
                "subjectType": item.subject_type, "subjectId": item.subject_id,
                "status": item.status, "matchedId": item.matched_id, "reason": item.reason,
            })
        for index, candidate in enumerate(result.candidates, 1):
            reason = _suspicious_reason(candidate.normalized_destination)
            if reason:
                dispositions.append({
                    "subjectType": "candidate", "subjectId": f"candidate-{index:04d}",
                    "status": "suspicious", "matchedId": None, "reason": reason,
                    "evidence": candidate.raw_evidence,
                })
        dispositions.extend(malformed_url_findings(raw_text, [item.raw_evidence for item in result.candidates]))

    normalized_errors = [
        {"category": str(item["category"]), "message": str(item["message"])}
        for item in errors
    ]
    uses = [{
        "overrideId": item.override_id, "documentIdentity": item.document_identity,
        "citationId": item.citation_id, "candidateId": item.candidate_id,
        "destination": item.destination,
    } for item in override_uses]
    statuses = Counter(item["status"] for item in dispositions)
    blocking_count = sum(count for status, count in statuses.items() if status in BLOCKING_STATUSES)
    outcome = "failure" if normalized_errors or blocking_count else "success"
    return {
        "schema": SCHEMA,
        "version": REPORT_VERSION,
        "source": {"name": source, "identity": source_identity},
        "records": {
            "sentences": sentences, "citations": citations,
            "references": references, "candidates": candidates,
        },
        "dispositions": dispositions,
        "overrideUsage": uses,
        "errors": normalized_errors,
        "summary": {
            "outcome": outcome,
            "sentenceCount": len(sentences), "citationCount": len(citations),
            "referenceCount": len(references), "candidateCount": len(candidates),
            "dispositionCount": len(dispositions), "blockingCount": blocking_count,
            "overrideUseCount": len(uses), "errorCount": len(normalized_errors),
            "statusCounts": {key: statuses[key] for key in sorted(statuses)},
        },
    }


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def render_text(report: Mapping[str, Any]) -> str:
    """Render all canonical report data in deterministic human-readable form."""
    source = report["source"]
    summary = report["summary"]
    lines = [
        f"Citation audit ({report['schema']})",
        f"Source: {source['name']}", f"Identity: {source['identity']}",
        f"Outcome: {summary['outcome']}",
    ]
    records = report["records"]
    for item in records["sentences"]:
        lines.append(f"sentence {item['id']}: text={item['text']!r} | location={item['source_location']!r}")
    for item in records["citations"]:
        lines.append(
            f"citation {item['id']}: identity={item['identity']!r} | form={item['form']} | "
            f"evidence={item['raw_evidence']!r} | sentence={item['sentence_id']} | location={item['source_location']!r}"
        )
    for item in records["references"]:
        lines.append(
            f"reference {item['id']}: identity={item['identity']!r} | text={item['text']!r} | "
            f"locations={item['source_locations']!r}"
        )
    for index, item in enumerate(records["candidates"], 1):
        lines.append(
            f"candidate candidate-{index:04d}: destination={item['normalized_destination']!r} | "
            f"method={item['extraction_method']} | evidence={item['raw_evidence']!r} | "
            f"provenance={item['provenance']!r} | location={item['source_location']!r}"
        )
    for item in report["dispositions"]:
        matched = f" -> {item['matchedId']}" if item.get("matchedId") else ""
        evidence = f" | evidence={item['evidence']!r}" if "evidence" in item else ""
        lines.append(f"{item['subjectType']} {item['subjectId']}: {item['status']}{matched} | {item['reason']}{evidence}")
    for item in report["overrideUsage"]:
        lines.append(f"override {item['overrideId']}: {item['citationId']} -> {item['candidateId']} ({item['destination']})")
    for item in report["errors"]:
        lines.append(f"ERROR[{item['category']}]: {item['message']}")
    lines.append("Summary: " + ", ".join(f"{key}={summary[key]}" for key in (
        "sentenceCount", "citationCount", "referenceCount", "candidateCount",
        "dispositionCount", "blockingCount", "overrideUseCount", "errorCount",
    )))
    lines.append("Status counts: " + (", ".join(f"{key}={value}" for key, value in summary["statusCounts"].items()) or "none"))
    return "\n".join(lines) + "\n"
