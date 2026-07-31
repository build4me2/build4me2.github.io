#!/usr/bin/env python3
"""Deterministic, fail-closed repository content validation."""
from __future__ import annotations

import html
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
POSTS = ROOT / "content" / "posts"
AUDITS = ROOT / "audits" / "citations"
REQUIRED = {"title", "date", "draft", "slug", "hideSummary", "ShowToc"}
SHORTCODE = re.compile(r"{{[<%]\s*(?:cite|citations)\b", re.I)
ANCHOR = re.compile(r"<a\b[^>]*\bhref=(?:\"([^\"]*)\"|'([^']*)')[^>]*>.*?</a>", re.I | re.S)
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
AUTHOR_YEAR = re.compile(r"\((?:[A-Z][A-Za-z'’.-]+(?:\s*&\s*[A-Z][A-Za-z'’.-]+)?),\s*(?:18|19|20)\d{2}[a-z]?\)")


@dataclass(frozen=True)
class Finding:
    path: str
    code: str
    message: str

    def text(self) -> str:
        return f"{self.path}: {self.code}: {self.message}"


def split_post(path: Path) -> tuple[dict[str, object], str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("+++\n"):
        raise ValueError("missing opening TOML delimiter")
    end = raw.find("\n+++\n", 4)
    if end < 0:
        raise ValueError("missing closing TOML delimiter")
    return tomllib.loads(raw[4:end] + "\n"), raw[end + 5 :]


def links(body: str) -> list[str]:
    values = [html.unescape(a or b) for a, b in ANCHOR.findall(body)]
    values.extend(html.unescape(item) for item in MARKDOWN_LINK.findall(body))
    return values


def safe_destination(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password


def outside_anchors(body: str) -> str:
    return ANCHOR.sub("", body)


def expected_audit(path: Path, body: str) -> dict[str, object]:
    destinations = links(body)
    unresolved = sorted(set(AUTHOR_YEAR.findall(outside_anchors(body))))
    return {
        "schema": "repository-citation-audit/v1",
        "article": path.relative_to(ROOT).as_posix(),
        "status": "blocked" if unresolved else "pass",
        "destinations": destinations,
        "unresolvedEvidence": unresolved,
        "summary": {"destinationCount": len(destinations), "blockingCount": len(unresolved)},
    }


def validate() -> list[Finding]:
    findings: list[Finding] = []
    posts = sorted(p for p in POSTS.glob("*.md") if p.name != "_index.md")
    if len(posts) != 4:
        findings.append(Finding("content/posts", "article_count", f"expected 4 posts, found {len(posts)}"))
    for path in posts:
        rel = path.relative_to(ROOT).as_posix()
        try:
            meta, body = split_post(path)
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as exc:
            findings.append(Finding(rel, "front_matter", str(exc)))
            continue
        if set(meta) != REQUIRED:
            findings.append(Finding(rel, "front_matter_schema", f"expected {sorted(REQUIRED)}, found {sorted(meta)}"))
        slug = meta.get("slug")
        if not isinstance(slug, str) or path.stem != slug:
            findings.append(Finding(rel, "route_identity", "filename must equal the explicit slug"))
        if not isinstance(meta.get("title"), str) or not str(meta.get("title")).strip():
            findings.append(Finding(rel, "title", "title must be non-empty"))
        if meta.get("draft") is not False or meta.get("hideSummary") is not True or meta.get("ShowToc") is not False:
            findings.append(Finding(rel, "front_matter_values", "draft/hideSummary/ShowToc do not match the established schema"))
        if SHORTCODE.search(body):
            findings.append(Finding(rel, "retired_shortcode", "cite/citations shortcodes are unsupported; use reviewed inline links"))
        for destination in links(body):
            if not safe_destination(destination):
                findings.append(Finding(rel, "unsafe_link", repr(destination)))
        audit = expected_audit(path, body)
        audit_path = AUDITS / f"{path.stem}.json"
        if not audit_path.is_file():
            findings.append(Finding(audit_path.relative_to(ROOT).as_posix(), "missing_audit", "citation audit is required"))
        else:
            try:
                committed = json.loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                findings.append(Finding(audit_path.relative_to(ROOT).as_posix(), "invalid_audit", str(exc)))
            else:
                if committed != audit:
                    findings.append(Finding(audit_path.relative_to(ROOT).as_posix(), "stale_audit", "regenerate and review exact citation evidence"))
        for evidence in audit["unresolvedEvidence"]:
            findings.append(Finding(rel, "unresolved_citation", str(evidence)))
    return sorted(findings, key=lambda item: (item.path, item.code, item.message))


def main() -> int:
    findings = validate()
    if findings:
        print(f"Content validation FAILED ({len(findings)} finding(s)):", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.text()}", file=sys.stderr)
        return 1
    print("Content validation passed (4 posts, front matter, links, shortcodes, and citation audits).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
