#!/usr/bin/env python3
"""Tests for stable unittest ordering and report normalization."""
from __future__ import annotations

import importlib.util
import re
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

    def test_normalization_removes_checkout_runtime_and_address_values(self) -> None:
        raw = f"{ROOT}/tests/example.py at 0xABC123\r\nRan 1 test in 3.141s\r\n"
        normalized = runner.normalize_report(raw)
        self.assertEqual(normalized, "<repo>/tests/example.py at <address>\nRan 1 test\n")
        self.assertIsNone(re.search(r"\d+\.\d+s", normalized))


if __name__ == "__main__":
    unittest.main()
