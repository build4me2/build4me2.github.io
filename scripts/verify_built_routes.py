#!/usr/bin/env python3
"""Verify that deployment output contains the established home and article routes."""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "baselines" / "preservation.json"
_SPACE = re.compile(r"\s+")


class RouteError(RuntimeError):
    """The built deployment tree does not satisfy the route contract."""


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href is not None:
                self.links.append(href)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def normalized(value: str) -> str:
    return _SPACE.sub(" ", html.unescape(value)).strip()


def route_file(site: Path, route: str) -> Path:
    if route == "/":
        return site / "index.html"
    if not route.startswith("/") or not route.endswith("/") or ".." in route.split("/"):
        raise RouteError(f"invalid route in preservation baseline: {route!r}")
    return site.joinpath(*route.strip("/").split("/"), "index.html")


def verify(site: Path, baseline_path: Path = BASELINE) -> int:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        listings = baseline["homeListing"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RouteError(f"could not read route contract: {exc}") from exc
    if not isinstance(listings, list) or len(listings) != 4:
        raise RouteError("route contract must contain exactly four home-listing entries")

    findings: list[str] = []
    pages: dict[str, PageParser] = {}
    for route, title in [("/", baseline.get("hugoConfiguration", {}).get("title"))] + [
        (entry.get("route"), entry.get("title")) for entry in listings if isinstance(entry, dict)
    ]:
        if not isinstance(route, str) or not isinstance(title, str):
            findings.append("baseline route and title values must be strings")
            continue
        path = route_file(site, route)
        relative = path.relative_to(site).as_posix()
        try:
            payload = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(f"{route}: cannot read {relative}: {exc.__class__.__name__}")
            continue
        if not payload.strip():
            findings.append(f"{route}: {relative} is empty")
            continue
        parser = PageParser()
        parser.feed(payload)
        pages[route] = parser
        if normalized(title) not in normalized(" ".join(parser.text)):
            findings.append(f"{route}: established title is missing from {relative}: {title!r}")

    home = pages.get("/")
    if home is not None:
        for entry in listings:
            if not isinstance(entry, dict) or not isinstance(entry.get("route"), str):
                continue
            route = entry["route"]
            linked_routes = {
                parsed.path
                for href in home.links
                for parsed in [urlsplit(href)]
                if not parsed.query and not parsed.fragment
            }
            if route not in linked_routes:
                findings.append(f"/: established listing link is missing: {route}")

    if findings:
        raise RouteError("built route verification failed:\n" + "\n".join(f"- {item}" for item in sorted(set(findings))))
    return len(listings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=ROOT / "public")
    args = parser.parse_args()
    try:
        count = verify(args.site.resolve())
    except RouteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Built route verification passed: home and {count} established article routes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
