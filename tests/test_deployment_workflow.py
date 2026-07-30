#!/usr/bin/env python3
"""Source-level contract tests for pull-request CI and gated Pages deployment."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "hugo.yml"


class DeploymentWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.preamble, jobs = cls.workflow.split("\njobs:\n", 1)
        matches = list(re.finditer(r"(?m)^  ([a-z][a-z0-9-]*):\n", jobs))
        cls.jobs = {
            match.group(1): jobs[match.start():matches[index + 1].start()]
            if index + 1 < len(matches) else jobs[match.start():]
            for index, match in enumerate(matches)
        }

    def test_pull_requests_and_approved_deployment_events_are_declared(self) -> None:
        self.assertRegex(self.preamble, r"(?m)^  pull_request:\s*$")
        self.assertRegex(self.preamble, r"(?ms)^  push:\n    branches:\n      - main$")
        self.assertRegex(self.preamble, r"(?m)^  workflow_dispatch:\s*$")
        self.assertIn("group: pages", self.preamble)

    def test_pull_request_job_has_read_only_access_and_runs_complete_suite(self) -> None:
        self.assertIn("permissions:\n  contents: read", self.preamble)
        self.assertNotIn("pages: write", self.preamble)
        self.assertNotIn("id-token: write", self.preamble)

        job = self.jobs["validate-build"]
        self.assertIn("permissions:\n      contents: read", job)
        self.assertNotIn("pages: write", job)
        self.assertNotIn("id-token: write", job)
        self.assertIn("name: Clean recursive-checkout acceptance", job)
        self.assertIn("submodules: recursive", job)
        self.assertIn("git submodule update --init --recursive", job)
        self.assertIn("hugo-version: '${{ env.HUGO_VERSION }}'", job)
        self.assertIn("extended: true", job)
        self.assertIn("run: make validate", job)
        self.assertIn("run: make reproducible", job)
        self.assertIn("run: make build", job)
        self.assertIn("run: make verify-routes", job)
        self.assertNotIn("actions/deploy-pages", job)
        self.assertNotIn("actions/upload-pages-artifact", job)

    def test_deployment_is_approved_event_only_and_consumes_validated_artifact(self) -> None:
        build = self.jobs["validate-build"]
        package = self.jobs["package-pages"]
        deploy = self.jobs["deploy"]
        approved_event_guard = "github.event_name == 'workflow_dispatch' ||"

        self.assertIn(approved_event_guard, build)
        self.assertIn("actions/upload-artifact@v4", build)
        self.assertIn("name: validated-site", build)
        self.assertIn(approved_event_guard, package)
        self.assertIn("needs: validate-build", package)
        self.assertIn("actions/download-artifact@v4", package)
        self.assertIn("name: validated-site", package)
        self.assertIn("actions/upload-pages-artifact@v3", package)
        self.assertIn(approved_event_guard, deploy)
        self.assertIn("needs: package-pages", deploy)
        self.assertIn("actions/deploy-pages@v4", deploy)
        self.assertNotIn("run: make build", package)
        self.assertNotIn("run: make build", deploy)


if __name__ == "__main__":
    unittest.main()
