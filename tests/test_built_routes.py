#!/usr/bin/env python3
"""Offline tests for deployment-output route verification."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_built_routes", ROOT / "scripts" / "verify_built_routes.py"
)
assert SPEC and SPEC.loader
routes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(routes)


class BuiltRouteTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        site = root / "public"
        entries = [
            {"route": f"/article-{index}/", "title": f"Article {index}"}
            for index in range(1, 5)
        ]
        baseline = root / "preservation.json"
        baseline.write_text(
            json.dumps({"hugoConfiguration": {"title": "Site title"}, "homeListing": entries}),
            encoding="utf-8",
        )
        site.mkdir()
        links = "".join(
            f'<a href="{entry["route"]}">{entry["title"]}</a>' for entry in entries
        )
        (site / "index.html").write_text(f"<html><body>Site title{links}</body></html>", encoding="utf-8")
        for entry in entries:
            output = site / entry["route"].strip("/")
            output.mkdir()
            (output / "index.html").write_text(
                f'<html><h1>{entry["title"]}</h1></html>', encoding="utf-8"
            )
        return site, baseline

    def test_accepts_home_and_exactly_four_nonempty_article_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site, baseline = self.fixture(Path(temporary))
            self.assertEqual(routes.verify(site, baseline), 4)

    def test_missing_route_fails_with_stable_actionable_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site, baseline = self.fixture(Path(temporary))
            (site / "article-3" / "index.html").unlink()
            with self.assertRaises(routes.RouteError) as raised:
                routes.verify(site, baseline)
        self.assertEqual(
            str(raised.exception),
            "built route verification failed:\n"
            "- /article-3/: cannot read article-3/index.html: FileNotFoundError",
        )

    def test_home_must_link_every_established_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site, baseline = self.fixture(Path(temporary))
            home = site / "index.html"
            home.write_text(home.read_text(encoding="utf-8").replace('href="/article-2/"', 'href="/wrong/"'), encoding="utf-8")
            with self.assertRaisesRegex(routes.RouteError, "established listing link is missing: /article-2/"):
                routes.verify(site, baseline)


if __name__ == "__main__":
    unittest.main()
