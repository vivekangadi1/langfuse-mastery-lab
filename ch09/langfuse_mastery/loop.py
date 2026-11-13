"""Module 14: the production loop's DECISIONS. You implement these; `pytest langfuse_mastery/tests/test_loop.py` checks
them.

No Langfuse imports here. The notebook does the Langfuse side (read traffic and scores, fill the queue, upsert dataset
items, run the CI gate, move labels); these functions decide what happens BETWEEN the stages, and they reuse your
earlier modules: `review` (module 08) for judge flags, controls and the error taxonomy, `judging` (module 07) for
verdicts, `alerting` (module 12) for the alert rules. Scores are plain dicts as `api.scores.get_many(...,
fields="details,subject,annotation")` returns them after `.dict()`: {"name", "value", "source", "dataType", "comment",
"metadata", "timestamp", "queueId", ...}.
"""
from __future__ import annotations

from datetime import datetime, timezone

from langfuse_lab.loop_spec import CAUSAL, STAGE_NAMES as STAGES
from langfuse_lab.regression import dataset_item_id
from langfuse_mastery import alerting, judging, review

JUDGE = "routing_judge"

# Given: what pages on LIVE traffic. Production requests carry no expected answer, so module 12's code-score rule
# (score.handoff_correct.API) never sees them: the judge's pass rate is the signal. "*routing_judge" also matches a
# PREFIXed judge name. The module 12 rules stay as guard rails (latency, cost, errors).
LOOP_RULES = (
    {"name": "judge-routing-drop", "pattern": "score.*routing_judge.*", "direction": "down", "min_abs": 0.15,
     "min_pct": None, "severity": "critical"},
    *alerting.ALERT_RULES,
)

# ---------------------------------------------------------------- TODO 1a: the release policy (a decision, as data)
# Fill in the loop's release policy. It becomes the CI gate's thresholds (gate_env) and the promotion rule
# (promotion_decision). Keys and the constraints the tests hold you to (pick the values and say why in a comment):
#   "reruns"             int >= 1   attempts per side for each suspect beyond the first (LLMs flake: module 10)
#   "max_rerun_items"    int >= 1   more suspects than this and the fix is plainly worse: no re-runs are paid for
#   "max_regressions"    int == 0   may an INCIDENT fix break something that works today?
#   "max_drop"           float in [0, 0.2]   tolerated drop of the mean pass rate (a ~10-item set: 0.1 = one flaky item)
#   "incident_pass_rate" float in (0.5, 1]   the share of attempts on which the fix must pass each incident test
LOOP_POLICY: dict = {}


# ---------------------------------------------------------------- TODO 1b: what goes into the review queue
def review_selection(scores_by_trace: dict, *, judge: str = JUDGE, threshold: float = 0.5, control_rate: float = 0.25,
                     salt: str = "") -> dict:
    """Which requests of an alert's window a human must look at. `scores_by_trace` = {trace_id: [score dicts]} for
    every request in the window, in time order.

    - flagged: review.judge_flags(scores, boolean_judges=[judge], threshold=threshold) is non-empty; the reason is
      those flags joined by ", " (e.g. "routing_judge=False").
    - unjudged: no score named `judge` that review.is_judge_score accepts: reason "unjudged". On live traffic the judge
      is the ONLY quality signal, so a request it never scored (a judge outage, a sampling gap) is not a pass.
    - controls: review.pick_controls(<the judged, unflagged ids in input order>, rate=control_rate, min_n=1,
      salt=salt): reason "control". Controls are how you find what the judge MISSES (module 08).

    Returns {"queue": [trace ids to queue, in input order], "reasons": {trace_id: reason} (same order),
             "flagged": [...], "unjudged": [...], "controls": [...]}  (each list in input order)."""
    raise NotImplementedError("TODO 1: implement review_selection")


# ---------------------------------------------------------------- TODO 1c: human labels -> dataset items
def incident_items(reviews: list[dict], *, names: dict, dataset_name: str, incident: str) -> list[dict]:
    """The regression tests this incident adds: one dataset item per request a HUMAN confirmed as a failure.

    `reviews` = [{"trace_id", "message", "handoff" (what the agent did; None means "none"), "scores": [score dicts of
    that trace]}]; `names` = {"human": the BOOLEAN review config's name, "expected": the expected-team config's name,
    "error_type": the taxonomy config's name} (config-backed score names carry the PREFIX).

    - Only ANNOTATION scores count (a judge or code score is not a human label); per name, the latest by "timestamp".
    - A request becomes an item when the human said NOT ok (judging.as_verdict(value) is False) AND named an expected
      team. Skip it when the expected team equals the agent's handoff (the labels contradict each other).
    - error_type: the human's taxonomy label when present, else review.error_type(expected, handoff).
    - Two failures with the same message (stripped) are ONE test: keep the first in `reviews` order.

    Each item uses `create_dataset_item`'s keyword names:
        {"id": dataset_item_id(dataset_name, message),          # deterministic: a re-run upserts, never duplicates
         "input": {"message": message},
         "expected_output": {"handoff": expected, "reference_answer": None},
         "metadata": {"case_id": f"{incident}:{trace_id[:8]}", "source": "incident", "label": "confirmed_failure",
                      "incident": incident, "error_type": ..., "trace_id": trace_id},
         "source_trace_id": trace_id}
    in `reviews` order."""
    raise NotImplementedError("TODO 1: implement incident_items")


# ---------------------------------------------------------------- TODO 1d: the gate's thresholds, as CI env vars
def gate_env(policy: dict) -> dict:
    """The env vars that give module 10's gate this policy (`ci/harness.py` POLICY_ENV): reruns -> GATE_RERUNS,
    max_rerun_items -> GATE_MAX_RERUN_ITEMS, max_regressions -> GATE_MAX_REGRESSIONS, max_drop -> GATE_MAX_DROP,
    max_items -> GATE_MAX_ITEMS. Values are str(value); keys the policy doesn't have are left out; any other key
    (incident_pass_rate) is the loop's own and is not passed."""
    raise NotImplementedError("TODO 1: implement gate_env")


# ---------------------------------------------------------------- TODO 1e: when to promote
def promotion_decision(report: dict, exit_code, *, incident_ids, policy: dict) -> dict:
    """May the gate's candidate go to production? `report` = the gate's `gate-report.json` (module 10's
    evaluate_candidate report, read back as JSON: "decision", "policy", "confirmed" with "rates" {item id: [baseline
    pass rate, candidate pass rate]}, "candidate_version"); `exit_code` = the gate process's exit code.

    Promote only when EVERY rule holds. One reason per failing rule, in this order:
    1. the gate shipped: report["decision"]["decision"] == "ship" AND exit_code == 0 (a block, an error or a crashed
       CI job never promotes) -> f"gate decision {decision} (exit {exit_code}): {the gate's reasons joined by '; '}"
    2. the gate ran at least as strictly as the loop's policy, for each of max_regressions (<=), max_drop (<=) and
       reruns (>=) that `policy` has: report["policy"][key] missing or looser ->
       f"gate policy looser than the loop's: {key} {have} {'>' or '<'} {policy[key]}"
    3. every incident item was compared (is a key of confirmed["rates"]) ->
       f"incident item(s) not in the gate run: {ids joined by ', '} (pin the dataset version that contains them)"
    4. each compared incident item passes on the candidate: rates[id][1] >= policy["incident_pass_rate"] (default 1.0;
       None fails) -> f"incident item {id} passes {rate} of attempts on the candidate (< {wanted})"
       (A gate checks "no regressions vs production". It does not check "the incident is fixed": an item production
       itself flakes on can fail on the candidate without counting as a regression.)

    Returns {"promote": bool, "version": report["candidate_version"],
             "reasons": the failures, or ["gate shipped", f"{n} incident item(s) pass on the candidate"]}."""
    raise NotImplementedError("TODO 1: implement promotion_decision")


# ---------------------------------------------------------------- TODO 1f: the timeline
def timeline(events: list[dict], *, skew_s: float = 10.0) -> dict:
    """Order the evidence events and measure the incident. `events` = [{"stage", "at" (ISO string or datetime), ...}].

    - events sorted by "at" (stable for equal times), each returned with "at" as an ISO string in UTC (a naive
      datetime or a string without offset is UTC);
    - problems, in this order: f"unknown stage: {stage}" for an event whose stage is not in STAGES;
      f"missing stage: {stage}" for each stage of STAGES with no event; for each (a, b) in CAUSAL where both exist:
      f"{b} before {a}" when b's FIRST event is more than `skew_s` seconds earlier than a's FIRST event (timestamps
      come from two clocks: this machine and the Langfuse server);
    - durations in seconds (None when a stage is missing):
        time_to_detect_s   first "deploy"  -> first "alert"
        time_to_review_s   first "alert"   -> LAST "review"
        time_to_fix_s      first "alert"   -> first "promote"
        time_to_recover_s  first "deploy"  -> first "resolved"   (the MTTR a leader asks for)

    Returns {"events": [...sorted...], "durations": {...}, "problems": [...]}."""
    raise NotImplementedError("TODO 1: implement timeline")


def _utc(v) -> datetime:
    """Given: ISO string (a trailing Z is fine) or datetime -> tz-aware UTC datetime (naive = UTC)."""
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
