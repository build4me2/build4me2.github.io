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


def split_post(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(r"\+\+\+\n(.*?)\n\+\+\+\n(?:\n)?(.*)", text, re.S)
    if not match:
        raise ValueError(f"{path.relative_to(ROOT)}: expected TOML front matter")
    return tomllib.loads(match.group(1)), match.group(2).rstrip("\n")


def source_links(body: str) -> list[str]:
    return re.findall(r"<a\s+[^>]*href=[\"']([^\"']+)[\"']", body, re.I)


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


def validate_sources(baseline: dict[str, Any], errors: list[str]) -> None:
    for relative, expected in baseline["protectedFiles"].items():
        path = ROOT / relative
        if not path.is_file():
            fail(errors, f"protected file missing: {relative}")
        elif digest(path.read_bytes()) != expected:
            fail(errors, f"protected presentation/configuration changed: {relative}")

    gitlink = subprocess.run(
        ["git", "ls-files", "--stage", "themes/PaperMod"], cwd=ROOT,
        text=True, capture_output=True,
    )
    fields = gitlink.stdout.split()
    actual_theme_commit = fields[1] if len(fields) >= 2 and fields[0] == "160000" else None
    if actual_theme_commit != baseline["paperModCommit"]:
        fail(errors, f"PaperMod gitlink changed: {actual_theme_commit!r}")

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


def validate_rendered(baseline: dict[str, Any], destination: Path, errors: list[str]) -> None:
    home_path = destination / "index.html"
    if not home_path.is_file():
        fail(errors, "home route / did not render index.html")
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

    for article in baseline["articles"]:
        route = article["route"]
        page_path = destination / route.strip("/") / "index.html"
        if not page_path.is_file():
            fail(errors, f"article route {route} did not render index.html")
            continue
        page = parse_html(page_path)
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

    combined = "\n".join(home.structure)
    for marker in baseline["renderedContract"]["siteMarkers"]:
        if marker not in combined:
            fail(errors, f"rendered site missing header/footer/theme marker {marker}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-only", action="store_true", help="skip the Hugo render checks")
    args = parser.parse_args()
    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Preservation baseline cannot be read: {type(exc).__name__}", file=sys.stderr)
        return 1
    errors: list[str] = []
    validate_sources(baseline, errors)

    if not args.source_only:
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
