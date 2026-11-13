"""Module 10: GIVEN pytest fixtures for the release gate (`pytest langfuse_mastery/ci`). Not a TODO.

The gate spends LLM calls, so it only runs when CANDIDATE_LABEL is set (a plain `pytest` never triggers it).
Env vars: CANDIDATE_LABEL (required), GATE_PREFIX, GATE_DATASET_VERSION, GATE_RUN_ID / GATE_RUN_TAG (local runs),
GATE_ARTIFACTS, GATE_DRY_RUN, GATE_RERUNS / GATE_MAX_* (policy), plus the LANGFUSE_* / AZURE_* keys.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from langfuse_mastery import ci_gate
from langfuse_mastery.ci.harness import Harness

HERE = Path(__file__).parent


def _git(*args) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


@pytest.fixture(scope="session")
def ci() -> dict:
    if not os.environ.get("CANDIDATE_LABEL"):
        pytest.skip("set CANDIDATE_LABEL to run the release gate (it costs LLM calls)")
    return ci_gate.ci_context(os.environ, local_sha=_git("rev-parse", "HEAD"),
                              local_branch=_git("rev-parse", "--abbrev-ref", "HEAD"))


@pytest.fixture(scope="session")
def harness(ci) -> Harness:
    return Harness.from_env(os.environ)


@pytest.fixture(scope="session")
def artifacts() -> Path:
    path = Path(os.environ.get("GATE_ARTIFACTS") or HERE / "artifacts")
    path.mkdir(parents=True, exist_ok=True)
    return path
