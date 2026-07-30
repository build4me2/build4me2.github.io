#!/usr/bin/env python3
"""Run the repository's unittest suite with a deterministic report."""
from __future__ import annotations

import io
import re
import sys
import unittest
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
_DURATION_LINE = re.compile(r"(?m)^Ran (\d+) (test(?:s)?) in [^\r\n]+$")
_ADDRESS = re.compile(r"\bat 0x[0-9a-fA-F]+\b")


def _cases(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    """Flatten a discovered suite so its cases can be globally ordered by id."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


def ordered_suite(suite: unittest.TestSuite) -> unittest.TestSuite:
    """Return standard test cases in a stable, globally sorted order."""
    return unittest.TestSuite(sorted(_cases(suite), key=lambda case: case.id()))


def normalize_report(report: str) -> str:
    """Remove values that depend on runtime or checkout location."""
    report = report.replace("\r\n", "\n").replace(str(ROOT), "<repo>")
    report = _DURATION_LINE.sub(r"Ran \1 \2", report)
    return _ADDRESS.sub("at <address>", report)


def render_report(suite: unittest.TestSuite) -> tuple[unittest.result.TestResult, str]:
    """Execute *suite* and return its result and normalized text report."""
    stream = io.StringIO()
    result = unittest.TextTestRunner(
        stream=stream,
        verbosity=2,
        failfast=False,
        buffer=True,
    ).run(ordered_suite(suite))
    return result, normalize_report(stream.getvalue())


def main() -> int:
    loader = unittest.defaultTestLoader
    suite = loader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(TESTS))
    result, report = render_report(suite)
    sys.stdout.write(report)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
