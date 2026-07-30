#!/usr/bin/env python3
"""Validate the established content and presentation preservation baseline.

This check is intentionally network-free. It compares source files first, then
builds the site into a temporary directory and checks the public contract of the
home page and four established essays.
"""
from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "tests" / "baselines" / "preservation.json"
REVIEW_RECORDS_PATH = ROOT / "tests" / "baselines" / "review-records.json"
CAPTURED_FROM_COMMIT = "8daa5220b19ec7e529d4354c77707bb882c9bce3"
PRESENTATION_ROOTS = {
    "extendedCss": Path("assets/css/extended"),
    "layouts": Path("layouts"),
}
HUGO_VERSION_PATTERN = re.compile(r"\bhugo v(?P<version>\d+\.\d+\.\d+)(?:[-+][^\s]+)?")


def digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def prose_segments(body: str) -> list[str]:
    """Return stable paragraph-sized units for actionable prose diagnostics."""
    return [normalized_text(part) for part in re.split(r"\n\s*\n", body) if normalized_text(part)]


def segment_digests(body: str) -> list[str]:
    return [digest(segment) for segment in prose_segments(body)]


def describe_segment_change(relative: str, actual: list[str], expected: list[str]) -> str:
    common = min(len(actual), len(expected))
    for index in range(common):
        if actual[index] != expected[index]:
            return f"essay prose segment {index + 1} changed without baseline review: {relative}"
    return (
        f"essay prose segment count changed from {len(expected)} to {len(actual)}: {relative}"
    )


def split_post_text(text: str, label: str) -> tuple[dict[str, Any], str]:
    match = re.fullmatch(r"\+\+\+\n(.*?)\n\+\+\+\n(?:\n)?(.*)", text, re.S)
    if not match:
        raise ValueError(f"{label}: expected TOML front matter")
    return tomllib.loads(match.group(1)), match.group(2).rstrip("\n")


def split_post(path: Path) -> tuple[dict[str, Any], str]:
    try:
        label = path.relative_to(ROOT).as_posix()
    except ValueError:
        label = path.as_posix()
    return split_post_text(path.read_text(encoding="utf-8"), label)


def source_links(body: str) -> list[str]:
    return re.findall(r"<a\s+[^>]*href=[\"']([^\"']+)[\"']", body, re.I)


def body_without_link_destinations(body: str) -> str:
    """Retain exact essay wording/markup while ignoring only href values."""
    return re.sub(r"(?i)(<a\s+[^>]*href=)([\"'])[^\"']*\2", r"\1\2<CITATION>\2", body)


def json_front_matter(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, (date, datetime)) else value
        for key, value in values.items()
    }


class PageParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []
        self.structure: list[str] = []
        self.title_depth = 0
        self.content_depth = 0
        self.paper_depth = 0
        self.current_listing: dict[str, str] | None = None
        self.title_text: list[str] = []
        self.content_text: list[str] = []
        self.content_links: list[str] = []
        self.listing: list[dict[str, str]] = []

    @staticmethod
    def _classes(attrs: dict[str, str]) -> set[str]:
        return set(attrs.get("class", "").split())

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        classes = self._classes(attrs)
        self.stack.append((tag, classes))
        marker = tag
        if attrs.get("id"):
            marker += "#" + attrs["id"]
        if classes:
            marker += "." + ".".join(sorted(classes))
        self.structure.append("<" + marker + ">")
        if tag == "h1" and "post-title" in classes:
            self.title_depth = len(self.stack)
        if tag == "div" and {"post-content", "md-content"} <= classes:
            self.content_depth = len(self.stack)
        if tag == "ul" and "paper-list" in classes:
            self.paper_depth = len(self.stack)
        if self.paper_depth and tag == "li" and "paper-list-item" in classes:
            self.current_listing = {"route": "", "title": "", "date": ""}
        if self.content_depth and tag == "a":
            self.content_links.append(attrs.get("href", ""))
        if self.current_listing is not None and tag == "a":
            href = attrs.get("href", "")
            self.current_listing["route"] = urllib.parse.urlsplit(href).path

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        depth = len(self.stack)
        _, classes = self.stack[-1]
        self.structure.append(f"</{tag}>")
        if self.title_depth == depth:
            self.title_depth = 0
        if self.content_depth == depth:
            self.content_depth = 0
        if self.current_listing is not None and tag == "li" and "paper-list-item" in classes:
            self.listing.append(self.current_listing)
            self.current_listing = None
        if self.paper_depth == depth:
            self.paper_depth = 0
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.title_depth:
            self.title_text.append(data)
        if self.content_depth:
            self.content_text.append(data)
        if self.current_listing is not None and self.stack:
            tag, classes = self.stack[-1]
            if tag == "a":
                self.current_listing["title"] += data
            elif "paper-list-date" in classes:
                self.current_listing["date"] += data


def parse_html(path: Path) -> PageParser:
    parser = PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def is_empty_page(path: Path) -> bool:
    """Treat zero-length and whitespace-only output as a failed route render."""
    return not path.read_text(encoding="utf-8").strip()


def hugo_diagnostic(output: str, destination: Path) -> str:
    """Keep Hugo failures useful while removing machine-specific and volatile data."""
    value = output.replace(str(destination), "<destination>").replace(str(ROOT), "<repository>")
    lines: list[str] = []
    for line in value.splitlines():
        line = line.strip()
        if not line or re.match(r"^(Start building sites|Total in) ", line):
            continue
        line = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds|seconds?)\b", "<duration>", line)
        if line not in lines:
            lines.append(line)
    return "\n".join(lines) or "Hugo returned a non-zero status without a diagnostic"


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def validate_hugo_toolchain(expected_version: str, require_extended: bool, errors: list[str]) -> bool:
    """Fail before rendering when the local Hugo binary differs from the contract."""
    expected_name = f"Hugo{' Extended' if require_extended else ''} {expected_version}"
    try:
        result = subprocess.run(
            ["hugo", "env", "--logLevel", "debug"],
            cwd=ROOT, text=True, capture_output=True, timeout=15,
        )
    except FileNotFoundError:
        fail(errors, f"Hugo toolchain mismatch: {expected_name} is required, but 'hugo' was not found")
        return False
    except subprocess.TimeoutExpired:
        fail(errors, f"Hugo toolchain mismatch: could not identify installed Hugo within 15 seconds; expected {expected_name}")
        return False

    output = result.stdout or result.stderr
    first_line = output.splitlines()[0].strip() if output.splitlines() else ""
    match = HUGO_VERSION_PATTERN.search(first_line)
    if result.returncode or match is None:
        observed = first_line or f"hugo env exited with status {result.returncode}"
        fail(errors, f"Hugo toolchain mismatch: could not parse {observed!r}; expected {expected_name}")
        return False

    actual_version = match.group("version")
    # Older Extended releases identify themselves with "+extended". Newer
    # releases expose the same capability through the embedded LibSass module.
    is_extended = "+extended" in first_line or "github.com/bep/golibsass=" in output
    if actual_version != expected_version or (require_extended and not is_extended):
        actual_name = f"Hugo{' Extended' if is_extended else ''} {actual_version}"
        fail(errors, f"Hugo toolchain mismatch: found {actual_name}; expected {expected_name}")
        return False
    return True


def json_type(value: Any) -> str:
    """Return JSON terminology rather than Python implementation types."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def validate_baseline_schema(baseline: Any) -> list[str]:
    """Validate every field consumed by the preservation checks.

    Paths and findings are sorted so a damaged fixture has the same diagnostic on
    every machine.  Callers must not use the fixture when this returns findings.
    """
    errors: list[str] = []

    def expect_object(value: Any, path: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            fail(errors, f"{path}: expected object, got {json_type(value)}")
            return None
        return value

    def field(obj: dict[str, Any], path: str, name: str, expected: type) -> Any | None:
        child_path = f"{path}.{name}"
        if name not in obj:
            fail(errors, f"{child_path}: missing required field")
            return None
        value = obj[name]
        # bool is an int in Python; schema types must remain exact.
        valid = isinstance(value, expected) and not (expected is int and isinstance(value, bool))
        if not valid:
            expected_name = {dict: "object", list: "array", str: "string", bool: "boolean"}.get(
                expected, expected.__name__
            )
            fail(errors, f"{child_path}: expected {expected_name}, got {json_type(value)}")
            return None
        return value

    def string_field(obj: dict[str, Any], path: str, name: str) -> str | None:
        value = field(obj, path, name, str)
        if isinstance(value, str) and not value:
            fail(errors, f"{path}.{name}: must not be empty")
            return None
        return value

    def string_array(obj: dict[str, Any], path: str, name: str) -> list[str] | None:
        value = field(obj, path, name, list)
        if not isinstance(value, list):
            return None
        for index, item in enumerate(value):
            if not isinstance(item, str):
                fail(errors, f"{path}.{name}[{index}]: expected string, got {json_type(item)}")
            elif not item:
                fail(errors, f"{path}.{name}[{index}]: must not be empty")
        return value

    root = expect_object(baseline, "$")
    if root is None:
        return sorted(set(errors))

    schema = string_field(root, "$", "schema")
    if schema is not None and schema != "hugo-preservation-baseline/v1":
        fail(errors, f"$.schema: unsupported value {schema!r}; expected 'hugo-preservation-baseline/v1'")
    captured_from = string_field(root, "$", "capturedFrom")
    if captured_from is not None and captured_from != CAPTURED_FROM_COMMIT:
        fail(errors, f"$.capturedFrom: expected pinned source commit {CAPTURED_FROM_COMMIT!r}")
    policy = field(root, "$", "policy", dict)
    if isinstance(policy, dict):
        for name in ("purpose", "approvedReconciliation", "prohibitedChanges"):
            string_field(policy, "$.policy", name)

    hugo_version = string_field(root, "$", "hugoVersion")
    if hugo_version is not None and not re.fullmatch(r"\d+\.\d+\.\d+", hugo_version):
        fail(errors, "$.hugoVersion: expected an exact semantic version such as '0.162.0'")
    field(root, "$", "hugoExtended", bool)

    paper_commit = string_field(root, "$", "paperModCommit")
    if paper_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", paper_commit):
        fail(errors, "$.paperModCommit: expected a 40-character lowercase hexadecimal commit")

    file_sets = field(root, "$", "presentationFileSets", dict)
    if isinstance(file_sets, dict):
        for name in sorted(PRESENTATION_ROOTS):
            string_array(file_sets, "$.presentationFileSets", name)

    protected = field(root, "$", "protectedFiles", dict)
    if isinstance(protected, dict):
        for relative in sorted(protected, key=str):
            path = f"$.protectedFiles[{relative!r}]"
            if not isinstance(relative, str) or not relative:
                fail(errors, f"{path}: file name must be a non-empty string")
            value = protected[relative]
            if not isinstance(value, str):
                fail(errors, f"{path}: expected string, got {json_type(value)}")
            elif not re.fullmatch(r"[0-9a-f]{64}", value):
                fail(errors, f"{path}: expected a 64-character lowercase hexadecimal SHA-256")

    configuration = field(root, "$", "hugoConfiguration", dict)
    if isinstance(configuration, dict):
        for dotted in sorted(configuration, key=str):
            if not isinstance(dotted, str) or not dotted or any(not part for part in dotted.split(".")):
                fail(errors, f"$.hugoConfiguration[{dotted!r}]: setting name must be a valid dotted string")
            value = configuration[dotted]
            if value is None or isinstance(value, (dict, list)):
                fail(errors, f"$.hugoConfiguration[{dotted!r}]: expected scalar, got {json_type(value)}")

    listing = field(root, "$", "homeListing", list)
    if isinstance(listing, list):
        if len(listing) != 4:
            fail(errors, f"$.homeListing: expected exactly 4 established entries, got {len(listing)}")
        for index, item in enumerate(listing):
            path = f"$.homeListing[{index}]"
            obj = expect_object(item, path)
            if obj is not None:
                for name in ("route", "title", "date"):
                    string_field(obj, path, name)

    articles = field(root, "$", "articles", list)
    if isinstance(articles, list):
        if len(articles) != 4:
            fail(errors, f"$.articles: expected exactly 4 established entries, got {len(articles)}")
        for index, item in enumerate(articles):
            path = f"$.articles[{index}]"
            obj = expect_object(item, path)
            if obj is None:
                continue
            for name in (
                "source", "route", "proseSha256", "renderedProseSha256",
                "renderedStructureSha256",
            ):
                value = string_field(obj, path, name)
                if name.endswith("Sha256") and value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                    fail(errors, f"{path}.{name}: expected a 64-character lowercase hexadecimal SHA-256")
            front_matter = field(obj, path, "frontMatter", dict)
            if isinstance(front_matter, dict):
                front_path = f"{path}.frontMatter"
                for name in ("title", "date", "slug"):
                    string_field(front_matter, front_path, name)
                for name in ("draft", "hideSummary", "ShowToc"):
                    field(front_matter, front_path, name, bool)
            string_array(obj, path, "citationDestinations")
            segments = string_array(obj, path, "proseSegmentSha256")
            if isinstance(segments, list):
                for segment_index, value in enumerate(segments):
                    if isinstance(value, str) and not re.fullmatch(r"[0-9a-f]{64}", value):
                        fail(
                            errors,
                            f"{path}.proseSegmentSha256[{segment_index}]: expected a 64-character lowercase hexadecimal SHA-256",
                        )

    rendered = field(root, "$", "renderedContract", dict)
    if isinstance(rendered, dict):
        for name in ("homeMarkers", "articleMarkers", "siteMarkers"):
            string_array(rendered, "$.renderedContract", name)
        home_hash = string_field(rendered, "$.renderedContract", "homeStructureSha256")
        if home_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", home_hash):
            fail(errors, "$.renderedContract.homeStructureSha256: expected a 64-character lowercase hexadecimal SHA-256")

    return sorted(set(errors))


def validate_review_records_schema(document: Any) -> list[str]:
    """Validate the audit records used to authorize citation reconciliation."""
    errors: list[str] = []
    if not isinstance(document, dict):
        return [f"$: expected object, got {json_type(document)}"]
    if document.get("schema") != "preservation-review-records/v1":
        fail(errors, "$.schema: expected 'preservation-review-records/v1'")
    records = document.get("records")
    if not isinstance(records, list):
        fail(errors, f"$.records: expected array, got {json_type(records)}")
        return sorted(errors)
    ids: set[str] = set()
    for index, record in enumerate(records):
        path = f"$.records[{index}]"
        if not isinstance(record, dict):
            fail(errors, f"{path}: expected object, got {json_type(record)}")
            continue
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            fail(errors, f"{path}.id: expected non-empty string")
        elif record_id in ids:
            fail(errors, f"{path}.id: duplicate record id {record_id!r}")
        else:
            ids.add(record_id)
        kind = record.get("kind")
        if kind == "baseline-capture":
            continue
        if kind not in {"citation-reconciliation", "front-matter-reconciliation"}:
            fail(errors, f"{path}.kind: unsupported review kind {kind!r}")
            continue
        for name in ("article", "reason"):
            if not isinstance(record.get(name), str) or not record[name].strip():
                fail(errors, f"{path}.{name}: expected non-empty string")
        if kind == "citation-reconciliation":
            for name in ("before", "after"):
                if not isinstance(record.get(name), str) or not record[name].strip():
                    fail(errors, f"{path}.{name}: expected non-empty string")
            if record.get("before") == record.get("after"):
                fail(errors, f"{path}: before and after must differ")
            citation_index = record.get("citationIndex")
            if not isinstance(citation_index, int) or isinstance(citation_index, bool) or citation_index < 1:
                fail(errors, f"{path}.citationIndex: expected positive integer")
        else:
            field_name = record.get("field")
            if field_name not in ("date", "draft", "hideSummary", "ShowToc"):
                fail(errors, f"{path}.field: expected a reconcilable front-matter field")
            expected_type = bool if field_name in ("draft", "hideSummary", "ShowToc") else str
            for name in ("before", "after"):
                value = record.get(name)
                if not isinstance(value, expected_type) or (expected_type is str and not value):
                    type_name = "boolean" if expected_type is bool else "non-empty string"
                    fail(errors, f"{path}.{name}: expected {type_name}")
            if record.get("before") == record.get("after"):
                fail(errors, f"{path}: before and after must differ")
        evidence = record.get("verificationEvidence")
        if not isinstance(evidence, list) or not evidence or not all(
            isinstance(item, str) and item.strip() for item in evidence
        ):
            fail(errors, f"{path}.verificationEvidence: expected non-empty array of non-empty strings")
        if record.get("proseArgumentRoutePresentationUnchanged") is not True:
            fail(errors, f"{path}.proseArgumentRoutePresentationUnchanged: expected true")
    return sorted(set(errors))


def validate_article_history(
    article: dict[str, Any], original_body: str, records: list[dict[str, Any]], errors: list[str]
) -> set[str]:
    """Anchor prose and citation history to the inherited source commit."""
    relative = article["source"]
    _, current_body = split_post(ROOT / relative)
    if body_without_link_destinations(current_body) != body_without_link_destinations(original_body):
        fail(errors, f"essay prose/argument differs from captured source commit: {relative}")

    original_links = source_links(original_body)
    current_links = source_links(current_body)
    applicable = [
        record for record in records
        if record.get("kind") == "citation-reconciliation" and record.get("article") == relative
    ]
    consumed: set[str] = set()
    for index in range(max(len(original_links), len(current_links))):
        before = original_links[index] if index < len(original_links) else None
        after = current_links[index] if index < len(current_links) else None
        if before == after:
            continue
        matches = [
            record for record in applicable
            if record.get("citationIndex") == index + 1
            and record.get("before") == before and record.get("after") == after
        ]
        if len(matches) != 1:
            fail(errors, f"citation destination {index + 1} changed without one exact review record: {relative}")
        else:
            consumed.add(matches[0]["id"])
    for record in applicable:
        if record.get("id") not in consumed:
            fail(errors, f"stale or non-matching citation review record {record.get('id')!r}: {relative}")
    return consumed


def captured_file(commit: str, relative: str, errors: list[str]) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative}"], cwd=ROOT, capture_output=True,
    )
    if result.returncode:
        fail(errors, f"cannot read captured file {relative} at commit {commit!r}")
        return None
    return result.stdout


def display_date(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def validate_preservation_history(
    baseline: dict[str, Any], review_document: dict[str, Any], errors: list[str]
) -> None:
    """Anchor editable fixtures and current sources to the captured Git tree."""
    captured = baseline["capturedFrom"]
    records = review_document["records"]
    expected_listing: list[tuple[str, dict[str, str]]] = []
    consumed_front_matter_records: set[str] = set()
    consumed_citation_records: set[str] = set()

    for article in baseline["articles"]:
        relative = article["source"]
        original = captured_file(captured, relative, errors)
        if original is None:
            continue
        try:
            original_front_matter, original_body = split_post_text(
                original.decode("utf-8"), f"{captured}:{relative}"
            )
            expected_front_matter = json_front_matter(original_front_matter)
            applicable = [
                record for record in records
                if record.get("kind") == "front-matter-reconciliation"
                and record.get("article") == relative
            ]
            for record in applicable:
                field_name = record["field"]
                if expected_front_matter.get(field_name) != record["before"]:
                    fail(errors, f"stale or non-matching front-matter review record {record['id']!r}: {relative}")
                    continue
                expected_front_matter[field_name] = record["after"]
                consumed_front_matter_records.add(record["id"])
            if article["frontMatter"] != expected_front_matter:
                fail(errors, f"front matter differs from captured history without exact review: {relative}")

            # Slugs/routes and established titles are identities, not reconcilable metadata.
            expected_route = f"/{json_front_matter(original_front_matter)['slug']}/"
            if article["route"] != expected_route:
                fail(errors, f"route identity differs from captured history: {relative}")
            expected_listing.append((expected_front_matter["date"], {
                "route": expected_route,
                "title": expected_front_matter["title"],
                "date": display_date(expected_front_matter["date"]),
            }))
            consumed_citation_records.update(
                validate_article_history(article, original_body, records, errors)
            )
        except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as exc:
            fail(errors, f"cannot compare captured source {relative} ({type(exc).__name__})")

    for record in records:
        kind = record.get("kind")
        record_id = record.get("id")
        if kind == "front-matter-reconciliation" and record_id not in consumed_front_matter_records:
            # Records for unknown articles and duplicate/out-of-order edits cannot silently authorize anything.
            article = record.get("article", "<unknown>")
            message = f"stale or non-matching front-matter review record {record_id!r}: {article}"
            if message not in errors:
                fail(errors, message)
        if kind == "citation-reconciliation" and record_id not in consumed_citation_records:
            article = record.get("article", "<unknown>")
            message = f"stale or non-matching citation review record {record_id!r}: {article}"
            if message not in errors:
                fail(errors, message)

    historical_listing = [item for _, item in sorted(expected_listing, key=lambda item: item[0], reverse=True)]
    if baseline["homeListing"] != historical_listing:
        fail(errors, "home listing routes, titles, dates, or order differ from captured history")

    # Presentation, configuration, and the theme gitlink are prohibited changes,
    # so compare them directly to Git history rather than trusting editable hashes.
    historical_paths_result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", captured, "layouts", "assets/css/extended"],
        cwd=ROOT, text=True, capture_output=True,
    )
    if historical_paths_result.returncode:
        fail(errors, f"cannot inventory presentation files at commit {captured!r}")
    else:
        historical_paths = sorted(filter(None, historical_paths_result.stdout.splitlines()))
        baseline_paths = sorted(
            path for paths in baseline["presentationFileSets"].values() for path in paths
        )
        if historical_paths != baseline_paths:
            fail(errors, "presentation file inventory differs from captured history")
        for relative in historical_paths:
            historical = captured_file(captured, relative, errors)
            current = ROOT / relative
            if historical is not None and (not current.is_file() or current.read_bytes() != historical):
                fail(errors, f"protected presentation differs from captured history: {relative}")

    historical_config = captured_file(captured, "hugo.toml", errors)
    if historical_config is not None and (ROOT / "hugo.toml").read_bytes() != historical_config:
        fail(errors, "Hugo configuration differs from captured history")

    gitlink = subprocess.run(
        ["git", "ls-tree", captured, "themes/PaperMod"], cwd=ROOT,
        text=True, capture_output=True,
    )
    fields = gitlink.stdout.split()
    historical_commit = fields[2] if len(fields) >= 3 and fields[1] == "commit" else None
    if gitlink.returncode or historical_commit != baseline["paperModCommit"]:
        fail(errors, "PaperMod gitlink differs from captured history")


def validate_presentation_file_sets(baseline: dict[str, Any], errors: list[str]) -> None:
    """Reject additions and removals as well as edits to known presentation files."""
    inventory = baseline.get("presentationFileSets", {})
    for name, root in PRESENTATION_ROOTS.items():
        expected = inventory.get(name)
        if not isinstance(expected, list) or not all(isinstance(path, str) for path in expected):
            fail(errors, f"presentation file-set baseline is missing or invalid: {name}")
            continue
        canonical_expected = sorted(set(expected))
        if expected != canonical_expected:
            fail(errors, f"presentation file-set baseline is not sorted and unique: {name}")

        directory = ROOT / root
        actual = sorted(
            path.relative_to(ROOT).as_posix()
            for path in directory.rglob("*")
            if path.is_file()
        ) if directory.is_dir() else []
        for relative in sorted(set(expected) - set(actual)):
            fail(errors, f"protected presentation file missing: {relative}")
        for relative in sorted(set(actual) - set(expected)):
            fail(errors, f"unexpected presentation override: {relative}")


def validate_theme_checkout(expected_commit: str, errors: list[str]) -> bool:
    """Verify both the pinned gitlink and the initialized submodule worktree."""
    gitlink = subprocess.run(
        ["git", "ls-files", "--stage", "themes/PaperMod"], cwd=ROOT,
        text=True, capture_output=True,
    )
    fields = gitlink.stdout.split()
    pinned_commit = fields[1] if len(fields) >= 2 and fields[0] == "160000" else None
    if pinned_commit != expected_commit:
        fail(
            errors,
            f"PaperMod gitlink is {pinned_commit!r}; expected pinned commit {expected_commit}",
        )

    theme = ROOT / "themes" / "PaperMod"
    # Git creates the submodule directory even when it has not populated the
    # worktree, so directory existence alone is not sufficient.
    if not theme.is_dir() or not any(theme.iterdir()):
        fail(
            errors,
            "PaperMod worktree is not initialized; run "
            "'git submodule update --init --recursive' before validation",
        )
        return False

    checkout = subprocess.run(
        ["git", "-C", str(theme), "rev-parse", "--verify", "HEAD"], cwd=ROOT,
        text=True, capture_output=True,
    )
    actual_commit = checkout.stdout.strip() if checkout.returncode == 0 else None
    if actual_commit != expected_commit:
        fail(
            errors,
            f"PaperMod worktree is at {actual_commit!r}; expected pinned commit "
            f"{expected_commit}; run 'git submodule update --init --recursive'",
        )
        return False
    return pinned_commit == expected_commit


def validate_sources(baseline: dict[str, Any], errors: list[str]) -> bool:
    validate_presentation_file_sets(baseline, errors)
    for relative, expected in baseline["protectedFiles"].items():
        path = ROOT / relative
        if not path.is_file():
            fail(errors, f"protected file missing: {relative}")
        elif digest(path.read_bytes()) != expected:
            fail(errors, f"protected presentation/configuration changed: {relative}")

    theme_ready = validate_theme_checkout(baseline["paperModCommit"], errors)

    try:
        config = tomllib.loads((ROOT / "hugo.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail(errors, f"Hugo configuration cannot be read: {type(exc).__name__}")
        config = {}
    for dotted, expected in baseline["hugoConfiguration"].items():
        value: Any = config
        missing = False
        for component in dotted.split("."):
            if not isinstance(value, dict) or component not in value:
                missing = True
                break
            value = value[component]
        if missing:
            fail(errors, f"Hugo setting {dotted} is missing; expected {expected!r}")
        elif value != expected:
            fail(errors, f"Hugo setting {dotted} is {value!r}; expected {expected!r}")

    for article in baseline["articles"]:
        relative = article["source"]
        try:
            front_matter, body = split_post(ROOT / relative)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            fail(errors, f"{relative}: cannot read or parse post ({type(exc).__name__})")
            continue
        if json_front_matter(front_matter) != article["frontMatter"]:
            fail(errors, f"front matter changed: {relative}")
        if digest(body) != article["proseSha256"]:
            actual_segments = segment_digests(body)
            expected_segments = article.get("proseSegmentSha256", [])
            if expected_segments:
                fail(errors, describe_segment_change(relative, actual_segments, expected_segments))
            else:
                fail(errors, f"essay prose/argument changed without baseline review: {relative}")
        if source_links(body) != article["citationDestinations"]:
            fail(errors, f"citation destinations or their order changed: {relative}")
    return theme_ready


def validate_rendered(baseline: dict[str, Any], destination: Path, errors: list[str]) -> None:
    home_path = destination / "index.html"
    if not home_path.is_file():
        fail(errors, "home route / did not render index.html")
        return
    if is_empty_page(home_path):
        fail(errors, "home route / rendered an empty HTTP response (index.html)")
        return
    home = parse_html(home_path)
    established_routes = {item["route"] for item in baseline["homeListing"]}
    actual_listing = [
        {key: normalized_text(value) for key, value in item.items()}
        for item in home.listing if item["route"] in established_routes
    ]
    if actual_listing != baseline["homeListing"]:
        fail(errors, "home listing presence, title, date, or established post ordering changed")
    if digest("\n".join(home.structure)) != baseline["renderedContract"]["homeStructureSha256"]:
        fail(errors, "home rendered element structure changed")
    for marker in baseline["renderedContract"]["homeMarkers"]:
        if marker not in home.structure:
            fail(errors, f"home page missing rendered structure marker {marker}")

    rendered_pages: list[tuple[str, PageParser]] = [("home route /", home)]
    for article in baseline["articles"]:
        route = article["route"]
        page_path = destination / route.strip("/") / "index.html"
        if not page_path.is_file():
            fail(errors, f"article route {route} did not render index.html")
            continue
        if is_empty_page(page_path):
            fail(errors, f"article route {route} rendered an empty HTTP response (index.html)")
            continue
        page = parse_html(page_path)
        rendered_pages.append((f"article route {route}", page))
        if normalized_text("".join(page.title_text)) != article["frontMatter"]["title"]:
            fail(errors, f"rendered title changed at {article['route']}")
        if digest(normalized_text("".join(page.content_text))) != article["renderedProseSha256"]:
            fail(errors, f"rendered essay prose changed at {article['route']}")
        if page.content_links != article["citationDestinations"]:
            fail(errors, f"rendered citation destinations changed at {article['route']}")
        if digest("\n".join(page.structure)) != article["renderedStructureSha256"]:
            fail(errors, f"rendered element structure changed at {article['route']}")
        for marker in baseline["renderedContract"]["articleMarkers"]:
            if marker not in page.structure:
                fail(errors, f"{article['route']} missing rendered structure marker {marker}")

    # Header, footer, and light/dark toggle behavior are a per-route contract,
    # not merely a home-page contract.  Checking every rendered route prevents a
    # valid home shell from hiding a blank or stripped article template.
    for route_name, page in rendered_pages:
        for marker in baseline["renderedContract"]["siteMarkers"]:
            if marker not in page.structure:
                fail(errors, f"{route_name} missing header/footer/theme marker {marker}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-only", action="store_true", help="skip the Hugo render checks")
    parser.add_argument(
        "--baseline", type=Path, default=BASELINE_PATH, metavar="PATH",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Preservation baseline cannot be read: {type(exc).__name__}", file=sys.stderr)
        return 1
    try:
        review_document = json.loads(REVIEW_RECORDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Preservation review records cannot be read: {type(exc).__name__}", file=sys.stderr)
        return 1
    schema_errors = validate_baseline_schema(baseline)
    review_schema_errors = validate_review_records_schema(review_document)
    if schema_errors or review_schema_errors:
        heading = "Preservation baseline schema is invalid:" if schema_errors else "Preservation review-record schema is invalid:"
        print(heading, file=sys.stderr)
        for error in schema_errors or review_schema_errors:
            print(f"- {error}", file=sys.stderr)
        target = "tests/baselines/preservation.json" if schema_errors else "tests/baselines/review-records.json"
        print(f"Fix {target} before running preservation checks.", file=sys.stderr)
        return 1

    errors: list[str] = []
    toolchain_ready = validate_hugo_toolchain(
        baseline["hugoVersion"], baseline["hugoExtended"], errors
    )
    theme_ready = validate_sources(baseline, errors)
    validate_preservation_history(baseline, review_document, errors)

    # Do not let Hugo's template lookup obscure an absent or stale submodule.
    # Source-only diagnostics still verify the worktree because it is a pinned
    # build input, while the full render only starts after this preflight passes.
    if not args.source_only and toolchain_ready and theme_ready:
        with tempfile.TemporaryDirectory(prefix="hugo-preservation-") as temp:
            build_root = Path(temp)
            destination = build_root / "site"
            command = [
                "hugo", "--cleanDestinationDir", "--noBuildLock",
                "--cacheDir", str(build_root / "cache"),
                "--destination", str(destination),
            ]
            try:
                result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=120)
            except FileNotFoundError:
                fail(errors, "Hugo is required for rendered baseline validation")
            except subprocess.TimeoutExpired:
                fail(errors, "Hugo baseline build timed out after 120 seconds")
            else:
                if result.returncode:
                    fail(errors, "Hugo build failed:\n" + hugo_diagnostic(result.stderr or result.stdout, build_root))
                else:
                    validate_rendered(baseline, destination, errors)

    if errors:
        # Stable output even if future checks discover findings via unordered inputs.
        errors = sorted(set(errors))
        print("Preservation baseline FAILED:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print("See docs/preservation-baselines.md before approving any baseline change.", file=sys.stderr)
        return 1
    print("Preservation baseline passed (four routes, prose, citations, ordering, and presentation).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
