#!/usr/bin/env python3
"""Tests for stable unittest ordering and report normalization."""
from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validation_runner", ROOT / "scripts" / "run_validation.py"
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def passing_suite() -> unittest.TestSuite:
    class Passing(unittest.TestCase):
        def test_z_last(self) -> None:
            pass

        def test_a_first(self) -> None:
            pass

    # Deliberately reverse the cases to verify that the runner, not the input,
    # controls report ordering.
    return unittest.TestSuite([Passing("test_z_last"), Passing("test_a_first")])


def failing_suite() -> unittest.TestSuite:
    class Failing(unittest.TestCase):
        def test_failure(self) -> None:
            self.fail("stable diagnostic")

    return unittest.TestSuite([Failing("test_failure")])


def temporary_path_suite(root: Path, *, fail: bool) -> unittest.TestSuite:
    """Create a report that exposes a real randomized root and useful suffix."""
    fixture = root / "fixtures" / "article.txt"

    class TemporaryPathCase(unittest.TestCase):
        def __str__(self) -> str:
            return f"test_fixture ({fixture})"

        def test_fixture(self) -> None:
            if fail:
                self.fail(f"could not validate {fixture}")

    return unittest.TestSuite([TemporaryPathCase("test_fixture")])


class DeterministicValidationRunnerTests(unittest.TestCase):
    def test_success_reports_are_equivalent_across_repeated_runs(self) -> None:
        first_result, first = runner.render_report(passing_suite())
        second_result, second = runner.render_report(passing_suite())

        self.assertTrue(first_result.wasSuccessful())
        self.assertTrue(second_result.wasSuccessful())
        self.assertEqual(first, second)
        self.assertLess(first.index("test_a_first"), first.index("test_z_last"))
        self.assertIn("Ran 2 tests\n", first)
        self.assertNotRegex(first, r"Ran 2 tests in ")

    def test_failure_reports_are_equivalent_across_repeated_runs(self) -> None:
        first_result, first = runner.render_report(failing_suite())
        second_result, second = runner.render_report(failing_suite())

        self.assertFalse(first_result.wasSuccessful())
        self.assertFalse(second_result.wasSuccessful())
        self.assertEqual(first, second)
        self.assertIn("stable diagnostic", first)
        self.assertIn("FAILED (failures=1)", first)
        self.assertNotRegex(first, r"\bat 0x[0-9a-fA-F]+\b")

    def test_success_reports_match_across_distinct_real_temporary_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="validation-fixture-") as first_root, tempfile.TemporaryDirectory(
            prefix="validation-fixture-"
        ) as second_root:
            self.assertNotEqual(first_root, second_root)
            first_result, first = runner.render_report(temporary_path_suite(Path(first_root), fail=False))
            second_result, second = runner.render_report(temporary_path_suite(Path(second_root), fail=False))

        self.assertTrue(first_result.wasSuccessful())
        self.assertTrue(second_result.wasSuccessful())
        self.assertEqual(first, second)
        self.assertIn("<temp>/fixtures/article.txt", first)
        self.assertNotIn(first_root, first)
        self.assertNotIn(second_root, second)

    def test_failure_reports_match_across_distinct_real_temporary_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hugo-preservation-") as first_root, tempfile.TemporaryDirectory(
            prefix="hugo-preservation-"
        ) as second_root:
            self.assertNotEqual(first_root, second_root)
            first_result, first = runner.render_report(temporary_path_suite(Path(first_root), fail=True))
            second_result, second = runner.render_report(temporary_path_suite(Path(second_root), fail=True))

        self.assertFalse(first_result.wasSuccessful())
        self.assertFalse(second_result.wasSuccessful())
        self.assertEqual(first, second)
        self.assertIn("could not validate <temp>/fixtures/article.txt", first)
        self.assertNotIn(first_root, first)
        self.assertNotIn(second_root, second)

    def test_normalization_removes_checkout_runtime_address_and_build_roots(self) -> None:
        raw = (
            f"{ROOT}/tests/example.py at 0xABC123\r\n"
            f"{tempfile.gettempdir()}/hugo-reproducible-r4nd0m/first/index.html\r\n"
            "Ran 1 test in 3.141s\r\n"
        )
        normalized = runner.normalize_report(raw)
        self.assertEqual(
            normalized,
            "<repo>/tests/example.py at <address>\n"
            "<temp>/first/index.html\n"
            "Ran 1 test\n",
        )
        self.assertIsNone(re.search(r"\d+\.\d+s", normalized))


if __name__ == "__main__":
    unittest.main()
