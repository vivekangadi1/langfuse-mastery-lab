"""Module 10: the release gate's pure logic. You implement these; `pytest langfuse_mastery/tests/test_ci_gate.py`
checks them.

No Langfuse imports here: CI context, names, statistics, the decision and the reports are plain functions, so they're
cheap to test and the gate behaves the same on your laptop and on a GitHub runner. `ci/release_gate.py` (the Langfuse
side) calls them.

A **verdict map** is {dataset item id: True | False | None} for one experiment run (None = the item has no
`handoff_correct` score), as `release_gate.verdicts()` reads it back. An **attempt list** is [first run, re-run 1, ...]
of verdict maps for one side (baseline or candidate).

Decision vocabulary (and the process exit code CI sees):
    "ship"  -> 0   the candidate may be promoted
    "block" -> 1   the candidate regressed: the PR check fails
    "error" -> 2   the gate could not decide (nothing compared): fail closed, but triage it as infrastructure
"""
from __future__ import annotations

from langfuse_mastery import experiments  # module 09: compare_runs() and gate() are reused here, not rewritten

EXIT_CODES = {"ship": 0, "block": 1, "error": 2}
STATUS_ORDER = ("regressed", "flaky", "missing", "fail", "improved", "pass")


def ci_context(env, *, local_sha: str | None = None, local_branch: str | None = None) -> dict:
    """Who/what is being gated, from the environment (a mapping like os.environ).

    Returns {"git_sha", "short_sha", "branch", "pr", "ci_run", "candidate_label", "trigger", "run_tag"}:
    - git_sha: GATE_HEAD_SHA (the workflow sets it to the PR's head commit), else GITHUB_SHA, else `local_sha`;
      lower-cased; must be 7-40 hex characters, else raise ValueError. short_sha = its first 7 characters.
      (On a pull_request event GITHUB_SHA is a temporary MERGE commit, not the commit under review.)
    - branch: GITHUB_HEAD_REF (set on PRs), else GITHUB_REF_NAME, else `local_branch`, else "".
    - pr: the number in GITHUB_REF "refs/pull/<n>/merge" as a string, else "".
    - ci_run: f"{GITHUB_RUN_ID}.{GITHUB_RUN_ATTEMPT or '1'}" when GITHUB_RUN_ID is set (a "Re-run job" gets a new
      attempt, so a new run name), else GATE_RUN_ID, else raise ValueError (a local run must name itself: the run
      name is the experiment's identity, module 09).
    - candidate_label: CANDIDATE_LABEL, default "staging".
    - trigger: "ci" when GITHUB_ACTIONS == "true", else "local".
    - run_tag: GATE_RUN_TAG, else f"ci:{ci_run}".
    Empty strings count as unset.
    """
    raise NotImplementedError("TODO 1: implement ci_context")


def run_name(ctx: dict, role: str, label: str, version, attempt: int = 0) -> str:
    """The experiment run name: f"ci {candidate_label} · {role} {label} v{version} · {short_sha} · {ci_run}", plus
    f" #{attempt}" for a re-run (attempt > 0). Unique per gate run, role, side and attempt, so nothing appends to an
    older experiment."""
    raise NotImplementedError("TODO 1: implement run_name")


def experiment_metadata(ctx: dict, *, role: str, label: str, prompt_version, dataset_version, **extra) -> dict:
    """The experiment's metadata: every value a string (Langfuse stringifies them anyway; be explicit).

    Keys: run_tag, trigger, git_sha, branch, pr, ci_run, candidate_label (from ctx), role, prompt_label (= label),
    prompt_version, dataset_version (a datetime -> .isoformat(), a string kept), and every `extra` key.
    Conversion: None -> "", True/False -> "true"/"false", anything else -> str(value).
    """
    raise NotImplementedError("TODO 1: implement experiment_metadata")


def pass_rates(attempts: list[dict]) -> dict:
    """Per item id (over every id in any attempt): the share of attempts that say True, counting only attempts where
    the id has a non-None verdict; None when it has none. [{a: True}, {a: False, b: True}] -> {a: 0.5, b: 1.0}."""
    raise NotImplementedError("TODO 1: implement pass_rates")


def confirm(comparison: dict, baseline_attempts: list[dict], candidate_attempts: list[dict]) -> dict:
    """Separate real regressions from LLM noise by majority vote over the attempts.

    `comparison` = experiments.compare_runs(baseline_attempts[0], candidate_attempts[0]): its "regressed" ids are the
    SUSPECTS (the re-runs only cover those). The compared ids are those with a non-None verdict in both first attempts.
    A suspect is a confirmed regression when its baseline pass rate (all baseline attempts) is > 0.5 AND its candidate
    pass rate is < 0.5; any other suspect is "flaky". (With no re-runs a suspect is 1.0 vs 0.0: confirmed.)

    Returns {"n", "baseline_rate", "candidate_rate", "delta", "regressed", "flaky", "suspects", "improved", "missing",
    "rates"}: n = number of compared ids; the rates are the MEAN PASS RATES over the compared ids (so a flaky item
    counts half, not zero), None when n == 0, delta = candidate_rate - baseline_rate; regressed = confirmed (sorted);
    flaky, suspects sorted; improved and missing copied (sorted) from `comparison`; rates = {id: [baseline pass rate,
    candidate pass rate]} for every compared id. The result has the keys experiments.gate() reads.
    """
    raise NotImplementedError("TODO 1: implement confirm")


def decide(confirmed: dict, *, max_regressions: int = 0, max_drop: float = 0.2) -> dict:
    """The gate decision: {"decision": "ship" | "block" | "error", "reasons": [str, ...], "exit_code": int}.

    n == 0 -> "error" with one reason that says nothing was compared (never "ship" on no evidence, but don't blame
    the candidate either). Otherwise REUSE module 09: experiments.gate(confirmed, max_drop=..., max_regressions=...)
    -> its decision and reasons. exit_code from EXIT_CODES.
    """
    raise NotImplementedError("TODO 1: implement decide")


def report_rows(confirmed: dict, info: dict) -> list[dict]:
    """One row per compared or missing item, for the reports.

    `info` maps item id -> {"case_id", "message", "expected"} (missing ids fall back to case_id = id, message "",
    expected None). Each row: {"id", "case_id", "message", "expected", "baseline", "candidate", "status"} where
    baseline/candidate are the pass rates from confirmed["rates"] (None for a missing item) and status is the first
    that applies: "regressed", "flaky", "improved", "missing" (membership in those lists), else "pass" when the
    candidate pass rate is >= 0.5, else "fail" (fails on both sides: a known gap, not a regression).
    Sorted by (STATUS_ORDER.index(status), case_id).
    """
    raise NotImplementedError("TODO 1: implement report_rows")


def junit_xml(decision: dict, rows: list[dict], *, suite: str) -> str:
    """A JUnit XML report CI systems render (GitHub test reporters, GitLab, Jenkins).

    A <testsuite name=suite tests=… failures=… errors=…> with:
    - first, a <testcase classname=suite name="release gate">: with a <failure message=…> (the reasons joined by
      "; ") when the decision is "block", an <error message=…> when it is "error";
    - one <testcase classname=suite name=row["case_id"]> per row: <failure message=…> for "regressed" (the message
      names the expected team and both pass rates), <error message=…> for "missing", a <system-out> line for
      "flaky" / "fail" / "improved", nothing for "pass".
    The counts match the children. Start with '<?xml version="1.0" encoding="utf-8"?>'. Build it with
    xml.etree.ElementTree (it escapes quotes and <> in messages for you).
    """
    raise NotImplementedError("TODO 1: implement junit_xml")


def markdown_summary(decision: dict, rows: list[dict], *, title: str, links: dict | None = None) -> str:
    """The PR-facing summary ($GITHUB_STEP_SUMMARY, a PR comment):
    - first line f"## {icon} {title}: {decision}" with icon ✅ ship / ❌ block / ⚠️ error;
    - one "- reason" bullet per reason, or "- no confirmed regressions" when there is none;
    - a Markdown table "| item | expected | baseline | candidate | status |" of every row whose status is not
      "pass" (pass rates with 2 decimals, "-" for None), omitted when there is no such row;
    - a line "<k>/<n> item(s) pass on the candidate." (k = rows with status "pass");
    - one "- [name](url)" line per link (e.g. the experiment URLs).
    Ends with a newline.
    """
    raise NotImplementedError("TODO 1: implement markdown_summary")
