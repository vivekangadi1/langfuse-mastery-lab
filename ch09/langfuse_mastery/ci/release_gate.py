"""Module 10: the Langfuse side of the release gate. You implement these (TODOs 2-4 in the notebook).

`h` is a `harness.Harness` (given: client, names, pinned dataset, labels, policy, task, evaluators); `ctx` is
`ci_gate.ci_context(...)`. The pure decisions live in `langfuse_mastery/ci_gate.py` (TODO 1) and module 09's
`experiments.compare_runs` / `gate`; this file runs experiments, reads them back and writes the verdict.

Flow of one gate run (evaluate_candidate):

    baseline_for()  ── reuse a stored production run with the same h.baseline_key, else run_variant("production")
    run_variant(candidate)                                   (both on the SAME pinned dataset version)
    verdicts(baseline), verdicts(candidate) ─► experiments.compare_runs ─► suspects = "regressed"
    re-run ONLY the suspects, on BOTH sides, policy["reruns"] times   (skip when there are more than
                                                                      policy["max_rerun_items"] suspects)
    ci_gate.confirm (majority vote) ─► ci_gate.decide ─► record_gate() on the candidate experiment
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from langfuse_mastery import ci_gate, experiments
from langfuse_mastery.ci.harness import BASELINE_LABEL, BASELINE_MAX_AGE, GATE_SCORE


def run_variant(h, ctx, label: str, *, role: str, item_ids=None, attempt: int = 0, extra: dict | None = None):
    """TODO 2: run prompt `label` over the pinned dataset as one experiment; return the ExperimentResult.

    - `prompt, task = h.task(label)` (serves the prompt once; the task links generations to it);
    - `dataset = h.pinned()`; when `item_ids` is given, keep only those items: `dataset.items = [...]`
      (re-runs cover only the suspects);
    - metadata = `ci_gate.experiment_metadata(ctx, role=role, label=label, prompt_version=prompt.version,
      dataset_version=h.dataset_version, **h.metadata(), **extra)`, plus `attempt=attempt` when attempt > 0;
    - `dataset.run_experiment(name=h.experiment_name, run_name=ci_gate.run_name(ctx, role, label, prompt.version,
      attempt), description=..., task=task, evaluators=h.evaluators, run_evaluators=h.run_evaluators,
      max_concurrency=h.policy["max_concurrency"], metadata=metadata)`.
    `role` is "baseline", "candidate" or "rerun" (anything else is fine for smoke tests).
    """
    raise NotImplementedError("TODO 2: implement run_variant")


def verdicts(h, experiment_id: str) -> dict:
    """TODO 2: {dataset item id: True/False/None} for one experiment, read back through the public API.

    `h.langfuse.api.experiments.list_items(from_start_time=h.since, experiment_id=..., fields="core,scores",
    limit=100)`, paging with `cursor=resp.meta.cursor` until it is None. The id is `row.experiment_item_id`; the
    verdict is the latest (`.timestamp`) score named `h.names["config"]` (`bool(value)`), None when there is none.
    """
    raise NotImplementedError("TODO 2: implement verdicts")


def baseline_for(h, ctx) -> tuple[str, bool]:
    """TODO 3: (baseline experiment id, reused?). A cached baseline saves a full run of LLM calls per PR.

    Reuse a stored experiment when ALL hold: listed by `h.langfuse.api.experiments.list(from_start_time=<now -
    BASELINE_MAX_AGE>, dataset_id=h.dataset_id, fields="core,metadata", limit=100)` (cursor paging), its
    metadata role == "baseline", its metadata baseline_key == h.baseline_key (same dataset version, production
    prompt version, model, evaluator; a dry run has another key), and `item_count == len(h.items)` (complete).
    Otherwise run it: `run_variant(h, ctx, BASELINE_LABEL, role="baseline")`, then
    `h.wait_for_items(result.experiment_id, len(h.items))` (ingestion is async), and return (id, False).
    """
    raise NotImplementedError("TODO 3: implement baseline_for")


def evaluate_candidate(h, ctx) -> dict:
    """TODO 4: the whole gate for ctx["candidate_label"]; records the decision and returns the report.

    1. `base_id, reused = baseline_for(h, ctx)`.
    2. `cand = run_variant(h, ctx, label, role="candidate", extra={"baseline_experiment_id": base_id,
       "baseline_reused": reused})`, then `h.wait_for_items(cand.experiment_id, len(h.items))`.
    3. `first = experiments.compare_runs(verdicts(base), verdicts(cand))`; suspects = first["regressed"].
    4. When 0 < len(suspects) <= h.policy["max_rerun_items"]: for k in 1..h.policy["reruns"], re-run the suspects
       on BOTH sides: `run_variant(h, ctx, side_label, role="rerun", item_ids=suspects, attempt=k,
       extra={"rerun_of": cand.experiment_id, "side": "baseline" | "candidate"})`, wait for len(suspects) items,
       and append its verdicts to that side's attempt list (which starts with the first run's verdicts).
    5. `confirmed = ci_gate.confirm(first, base_attempts, cand_attempts)`;
       `decision = ci_gate.decide(confirmed, max_regressions=h.policy["max_regressions"],
       max_drop=h.policy["max_drop"])`.
    6. Build the report, `record_gate(h, report)`, return it. Report keys (record_gate and the test read them):
       ctx, candidate_label, candidate_version (h.labels[label]), baseline_version, candidate_experiment_id,
       baseline_experiment_id, baseline_reused, rerun_experiment_ids, comparison (first), confirmed, decision,
       rows (`ci_gate.report_rows(confirmed, h.info)`), policy (h.policy), links ({"candidate run":
       cand.experiment_url, "baseline run": h.experiment_url(base_id)}), elapsed_s.
    """
    raise NotImplementedError("TODO 4: implement evaluate_candidate")


def record_gate(h, report: dict) -> None:
    """TODO 4: write the decision ON the candidate experiment, so the run itself says whether it shipped.

    `h.langfuse.create_score(dataset_run_id=<candidate experiment id>, name=GATE_SCORE, value=<"ship" | "block" |
    "error">, data_type="CATEGORICAL", comment=<the reasons joined by "; ", or "no confirmed regressions">,
    score_id=<deterministic, e.g. sha256(f"{experiment id}:{GATE_SCORE}")[:32]>, metadata={...})`, then
    `h.langfuse.flush()`. A fixed score_id makes a re-recorded decision UPDATE the score instead of adding one.
    metadata: decision, exit_code, reasons, regressed, flaky, suspects, improved, policy, git_sha, branch, pr,
    ci_run, trigger, candidate_label, candidate_version, baseline_experiment_id, baseline_reused,
    rerun_experiment_ids, elapsed_s.
    """
    raise NotImplementedError("TODO 4: implement record_gate")
