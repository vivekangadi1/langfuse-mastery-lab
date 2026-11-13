"""Module 10 lab support: the clean candidate prompt, the seed items, the workflow checker."""
from pathlib import Path

import pytest

from langfuse_lab import prompts, regression
from langfuse_lab.ci_workflow import workflow_problems

GOOD = (Path(__file__).parent / "data" / "release_gate_ok.yml").read_text(encoding="utf-8")


def test_clean_candidate_differs_only_in_the_login_rule():
    clean = prompts.CLEAN_CANDIDATE_TEMPLATE
    assert clean not in (prompts.SYSTEM_PROMPT_TEMPLATE, prompts.WRONG_HANDOFF_TEMPLATE)
    assert "ALWAYS create_handoff to account_security" in clean
    assert "{{company}}" in clean and "{{max_sentences}}" in clean
    assert prompts.CANDIDATE_LABEL not in ("production", "staging")


def test_seed_items_match_module_09_ids_and_labels():
    items = regression.seed_items("support-regression")
    assert len(items) == 7 and len({i["id"] for i in items}) == 7
    for i in items:
        assert i["id"] == regression.dataset_item_id("support-regression", i["input"]["message"])
    by_case = {i["metadata"]["case_id"]: i for i in items}
    assert by_case["invoice_request"]["expected_output"]["handoff"] == "none"  # module 09 fixes it later
    assert by_case["reset_password_wrong_handoff"]["expected_output"]["handoff"] == "account_security"
    assert by_case["reset_password_wrong_handoff"]["metadata"]["source"] == "seed"
    assert by_case["locked_out_cust_03"]["metadata"] == {"case_id": "locked_out_cust_03", "source": "curated",
                                                         "label": "curated"}


def test_reference_workflow_has_no_problems():
    assert workflow_problems(GOOD) == []


@pytest.mark.parametrize("old,new,fragment", [
    ("  workflow_dispatch:\n    inputs:\n      candidate_label:", "  workflow_dispatch:\n    inputs:\n      label:",
     "candidate_label"),
    ("  pull_request:\n", "  push:\n", "pull_request"),
    ("timeout-minutes: 20", "timeout-minutes: 120", "timeout-minutes"),
    ("${{ secrets.LANGFUSE_SECRET_KEY }}", "sk-lf-0123456789abcdef", "secrets.LANGFUSE_SECRET_KEY"),
    ("${{ github.event.pull_request.head.sha || github.sha }}", "${{ github.sha }}", "GATE_HEAD_SHA"),
    ("      - uses: actions/upload-artifact@v4\n        if: always()\n", "      - uses: actions/upload-artifact@v4\n",
     "always()"),
    ("concurrency:\n  group: release-gate-${{ github.event.pull_request.number || github.ref }}\n  cancel-in-progress: true\n",
     "", "concurrency"),
    ("permissions:\n  contents: read\n", "", "permissions"),
    ("      GATE_ARTIFACTS: ${{ github.workspace }}/gate-artifacts\n", "", "GATE_ARTIFACTS"),
    ("python -m pytest langfuse_mastery/ci", "python -m pytest langfuse_mastery/tests", "pytest"),
])
def test_workflow_problems_catch_each_gap(old, new, fragment):
    assert old in GOOD
    problems = workflow_problems(GOOD.replace(old, new))
    assert problems and any(fragment in p for p in problems), problems


def test_hard_coded_key_is_flagged():
    assert any("key" in p for p in workflow_problems(GOOD + "\n# pk-lf-2fa7a7ec-1b20\n"))


def test_garbage_is_reported_not_raised():
    assert workflow_problems("::: not yaml :::\n  - [") and workflow_problems("") == ["the workflow is empty or not a mapping"]
