"""Module 10: what a usable release-gate GitHub Actions workflow must contain. No Langfuse code.

`workflow_problems(text)` returns a list of human-readable problems ([] = fine). It is a structural lint, not a
GitHub Actions validator: it checks the pieces the gate depends on (triggers, secrets, the head SHA, the pytest step,
artifacts on failure, a time budget, no overlapping runs, least privilege, no hard-coded keys).
"""
from __future__ import annotations

import re

import yaml

REQUIRED_SECRETS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
                    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY")
MAX_TIMEOUT_MINUTES = 30
KEY_LIKE = re.compile(r"\b(?:sk|pk)-lf-[0-9a-f-]{8,}")


def _env(*blocks) -> dict:
    out = {}
    for b in blocks:
        if isinstance(b, dict) and isinstance(b.get("env"), dict):
            out.update({str(k): str(v) for k, v in b["env"].items()})
    return out


def workflow_problems(text: str) -> list[str]:
    try:
        wf = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"not valid YAML: {exc}"]
    if not isinstance(wf, dict):
        return ["the workflow is empty or not a mapping"]
    problems = []
    if KEY_LIKE.search(text):
        problems.append("a Langfuse key is written in the file: use ${{ secrets.NAME }}")

    on = wf.get("on", wf.get(True))  # PyYAML (YAML 1.1) reads the bare key `on` as True
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = {k: None for k in on}
    on = on if isinstance(on, dict) else {}
    if "pull_request" not in on:
        problems.append("on: needs pull_request (gate every PR that touches the agent or its prompt)")
    dispatch = on.get("workflow_dispatch")
    if "workflow_dispatch" not in on:
        problems.append("on: needs workflow_dispatch (run the gate by hand for any candidate label)")
    elif not (isinstance(dispatch, dict) and "candidate_label" in (dispatch.get("inputs") or {})):
        problems.append("workflow_dispatch needs an input `candidate_label`")

    if not isinstance(wf.get("concurrency"), (dict, str)):
        problems.append("concurrency: missing (two gate runs for the same PR would both spend LLM calls; cancel the older one)")

    jobs = wf.get("jobs") if isinstance(wf.get("jobs"), dict) else {}
    if not jobs:
        return problems + ["jobs: missing"]
    perms = [wf.get("permissions")] + [j.get("permissions") for j in jobs.values() if isinstance(j, dict)]
    if not any(isinstance(p, dict) and p.get("contents") == "read" for p in perms) and "read-all" not in perms:
        problems.append("permissions: give the job least privilege (contents: read)")

    found_pytest = False
    for name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
        gate_steps = [s for s in steps if "pytest" in str(s.get("run", "")) and "langfuse_mastery/ci" in str(s.get("run", ""))]
        if not gate_steps:
            continue
        found_pytest = True
        timeout = job.get("timeout-minutes")
        if not isinstance(timeout, (int, float)) or timeout > MAX_TIMEOUT_MINUTES:
            problems.append(f"job {name}: timeout-minutes must be set and <= {MAX_TIMEOUT_MINUTES} (a hung LLM call "
                            "must not hold the runner for 6 h)")
        if not any(str(s.get("uses", "")).startswith("actions/checkout@") for s in steps):
            problems.append(f"job {name}: no actions/checkout step")
        env = _env(wf, job, *gate_steps)
        for key in REQUIRED_SECRETS:
            if "secrets." not in env.get(key, ""):
                problems.append(f"job {name}: env {key} must come from ${{{{ secrets.{key} }}}}")
        if "CANDIDATE_LABEL" not in env:
            problems.append(f"job {name}: env CANDIDATE_LABEL missing (which prompt label is the candidate?)")
        if "pull_request.head.sha" not in env.get("GATE_HEAD_SHA", ""):
            problems.append(f"job {name}: env GATE_HEAD_SHA must use github.event.pull_request.head.sha (on a PR, "
                            "GITHUB_SHA is the temporary merge commit, not the commit under review)")
        if "GATE_ARTIFACTS" not in env:
            problems.append(f"job {name}: env GATE_ARTIFACTS missing (where the gate writes its JUnit + Markdown)")
        uploads = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
        if not uploads:
            problems.append(f"job {name}: no actions/upload-artifact step for the gate report")
        elif not any("always()" in str(s.get("if", "")) for s in uploads):
            problems.append(f"job {name}: upload the artifacts with `if: always()`: a blocked gate is exactly when "
                            "you need the report")
    if not found_pytest:
        problems.append("no job runs `pytest ... langfuse_mastery/ci`")
    return problems
