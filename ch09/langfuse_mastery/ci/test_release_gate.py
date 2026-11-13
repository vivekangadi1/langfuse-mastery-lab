"""Module 10, TODO 5a: the release gate as a pytest test (`CANDIDATE_LABEL=... pytest langfuse_mastery/ci`).

Fixtures (conftest.py, given): `harness` (harness.Harness), `ci` (ci_gate.ci_context), `artifacts` (a Path).
"""
from __future__ import annotations

import json

import pytest

from langfuse_mastery import ci_gate
from langfuse_mastery.ci import release_gate


def test_candidate_has_no_confirmed_regressions(harness, ci, artifacts):
    """TODO 5a:
    1. report = release_gate.evaluate_candidate(harness, ci)
    2. Write the artifacts into `artifacts`:
       - gate-summary.md: ci_gate.markdown_summary(decision, rows, title=..., links=report["links"]), with a title
         that names the candidate label + version and the production version;
       - gate-junit.xml: ci_gate.junit_xml(decision, rows, suite=f"release-gate.{candidate label}");
       - gate-report.json: the report (json.dumps(..., default=str)).
    3. Exit like a gate: decision "error" -> pytest.exit(<summary>, returncode=decision["exit_code"]) (CI shows
       "the gate is broken", not "your PR is bad"); otherwise assert decision == "ship", with the Markdown summary
       as the assertion message (it names the regressed items in the pytest output).
    """
    pytest.fail("TODO 5a: implement the release gate test")
