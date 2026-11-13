# Langfuse Mastery: Module 08 (Human review) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 06/07 plans.

**Goal:** module 08 closes the loop on module 07's judges. Low or False judge scores fill an **annotation queue**; a
human (the learner) reviews each item, in the **UI** and/or through the API, with **ANNOTATION scores**, an error
taxonomy and **comments**; then the learner measures **human-vs-judge agreement** (agreement rate, Cohen's kappa,
flag precision, judge misses found by control items).

Scenario (design row 08): "Queue filled by low judge scores".

**Module 07's objects need not exist.** The learner has not run 07 yet. The notebook detects the judge source:
- `JUDGE_MODE = "live"` when the enabled rule `PREFIX + "support-live-eval"` exists: the traffic carries the
  `live-eval` tag and the worker's real EVAL scores are used (≈10 judge calls, made by the worker).
- otherwise `JUDGE_MODE = "stand-in"`: a **given, clearly labelled, deterministic keyword judge**
  (`langfuse_lab/standin_judge.py`) writes `routing_judge` scores with `source=API`, the reasoning as `comment`,
  `metadata.evaluator = "stand-in:keyword-v1"`, on the root observation (where EVAL scores land). It is deliberately
  naive ("refund" → billing), so it raises a false alarm on "What is your refund policy?", the same false alarm the
  first 07 prompt draft produced. Verify accepts EVAL scores or stand-in scores, and says which mode it saw. No Azure
  judge calls, and verify stays honest: the stand-in score is a real stored score with its provenance.

**Conventions:** as in module 07 (`ch09/`, `$PY`, explicit `git add`, trailer, branch `langfuse-mastery`, no Langfuse
code in `langfuse_lab/` except the read-only `verify.py`). Project-global names are `PREFIX + base`; the learner uses
`PREFIX = ""`, validation `zz_ref_m08_`.

| object | name (`PREFIX=""`) |
|---|---|
| annotation queue | `routing-review` |
| score config (BOOLEAN) | `human_routing_ok` |
| score config (CATEGORICAL) | `review_error_type` (`wrong_team`, `missed_handoff`, `unnecessary_handoff`, `other`) |
| judge score read | `routing_judge` (+ `helpfulness_judge` in live mode) |
| module 07 rule looked up | `support-live-eval` |

**Verified live (probe, 2026-11-12, server 4.77 / SDK 5.2; probe objects `zz_probe_m08_*`):**
- **Queues** (`api.annotation_queues`): `create_queue(name, score_config_ids=[...], description=)`.
  - **At least one score config is required** (400 `too_small`). Names are **unique** (400 "A queue with this name
    already exists."), but get-or-create by name is still the pattern (list first; the 400 is not a lookup).
  - `list_queues(page, limit)` (page-based, `meta.total_pages`), `get_queue(id)`. **No update, archive or delete
    endpoint for queues**: score configs cannot be changed after creation through the API.
- **Items:** `create_queue_item(queue_id, object_id=, object_type="TRACE"|"OBSERVATION"|"SESSION", status=)`.
  - **No dedupe**: adding the same trace twice creates two items. **No existence check**: a bogus object id is
    accepted. So the learner lists existing items first (`list_queue_items(queue_id, status=, page, limit)`).
  - `update_queue_item(queue_id, item_id, status="COMPLETED")` sets `completedAt`; `status="COMPLETED"` is accepted at
    create time too. Item shape: `id, queueId, objectId, objectType, status, completedAt, createdAt, updatedAt`.
- **Assignments:** `create_queue_assignment(queue_id, user_id=)` works and is idempotent; unknown user → 404.
  **There is no endpoint to list assignments**, and the project key cannot list members
  (`organizations.get_project_memberships` → 403 "projectMembers:read" needs an org key). A user id is visible as
  `authorUserId` on a score made in the UI. → verify reports assignments ⚠️ (not readable through the public API).
- **ANNOTATION scores through the API:** `api.scores.create(request=[CreateScoreRequest(..., source="ANNOTATION",
  config_id=, queue_id=, id=)])` answers 202 `Accepted`. `source` accepts only `API`/`ANNOTATION` (EVAL is rejected
  client-side). The SDK's `create_score` has no `source` (always API).
  - **`configId` is required for ANNOTATION** (except CORRECTION). Without it the score is **dropped silently**; the
    worker logs `configId is required when source is ANNOTATION`. A config violation (value 7 on 0..1) is dropped the
    same way. The 202 says nothing in either case.
  - API annotation scores have `authorUserId = null` (no field to set it).
- **Reading:** `scores.get_many(..., fields="details,subject,annotation")`. The **`annotation`** group adds
  `authorUserId` and `queueId` (valid groups: core, details, subject, annotation). `queue_id=` and `source=` filters work.
- **UI review (checked in the browser):** Annotation Queues → queue → *Process queue* shows the trace with the queue's
  configs; a score set there is `source=ANNOTATION`, `authorUserId` = the user, `queueId` = the queue, subject = trace.
  *Mark Completed* (Ctrl+Enter) sets the item `COMPLETED`. API-written annotation scores show up in the same panel.
- **Comments** (`api.comments`): `create(project_id=, object_type="TRACE"|"OBSERVATION"|"SESSION"|"PROMPT",
  object_id=, content=, author_user_id=)`. **Upper-case** object types only (`"trace"` → 400). Unknown object → 404.
  `project_id` is required by the SDK but the server uses the key's project (a wrong id still attaches to ours).
  `get(object_type=, object_id=)` lists them; `authorUserId` is null unless passed (a real member id is accepted).
  No update/delete in the public API.

---

### Task 1: lab: stand-in judge + review cases
- `langfuse_lab/standin_judge.py`: `POLICY` keyword table, `policy_team(message)`, `judge_routing(message, handoff)
  -> (ok, reasoning)`, `EVALUATOR = "stand-in:keyword-v1"`. Pure, deterministic, deliberately naive.
- `traffic.review_cases(run_id)`: module 07's 5 cases, all judged (one planted `wrong_handoff`).
- Tests: the planted login→billing is flagged, the refund-policy question is a false alarm, ids are run-scoped.
- Commit: "lab: module 08 stand-in keyword judge + review cases".

### Task 2: verify: module 08 checks
`module_08_checks(run_tag, client, *, prefix="", threshold=0.5, report=None, expected_traces=5)`; one snapshot per
attempt: run roots, per-trace scores (`details,subject,annotation`), active configs, queues, the queue's items,
per-trace comments.
1. review configs defined once (`human_routing_ok` BOOLEAN, `review_error_type` CATEGORICAL with the 4 labels).
2. queue exists once and carries both config ids.
3. every run trace has a judge `routing_judge` score (EVAL, or stand-in API with `metadata.evaluator`), mode reported.
4. queue = judge-flagged traces + controls: every flagged run trace queued as a TRACE item, none twice, ≥1 control
   (an unflagged trace) and no more than half of the unflagged ones.
5. every queued run trace reviewed: item COMPLETED, `human_routing_ok` ANNOTATION score with the config id and
   `queueId`; PENDING → NotYet with the queue URL. Detail counts UI (authorUserId) vs API reviews.
6. every reviewed trace has a trace comment.
7. every human-flagged trace has a `review_error_type` ANNOTATION score.
8. the learner's report equals the recomputation (n, agree, judge_flagged, human_flagged, both_flagged, kappa).
9. planted wrong_handoff caught: judge-flagged, queued, human False, and the error type that matches the root's
   `metadata.handoff_target` (login → billing = `wrong_team`; seen in validation run 1: the bad prompt sometimes
   hands off to nobody = `missed_handoff`, so the label is derived, not hard-coded).
10. queue assignment: ⚠️ FeatureUnavailable (no list endpoint).
Commit: "lab: module 08 checks (...)".

### Task 3: learner module `langfuse_mastery/review.py` + tests
`is_judge_score`, `judge_flags`, `pick_controls`, `items_to_add`, `error_type`, `agreement` (with kappa and
flag precision). Stubs + `tests/test_review.py`. Commit: "mastery: module 08 review stubs + tests (learner implements)".

### Task 4: notebook `src/08_human_review.py` + live validation
TODO 1 review.py; Given (instrumentation, configs, judge mode, stand-in writer, wait for judges); TODO 2
`ensure_queue`; run; TODO 3 `fill_queue` (twice, no duplicates); UI step; TODO 4 `review_pending` (scripted reviewer
for what is still PENDING); TODO 5 `human_vs_judge` → `REPORT`; TODO 6 (optional) assignment; verify; UI tour; notes.
Validation with `PREFIX="zz_ref_m08_"`, stand-in mode, reference `review` monkey-patched. Then archive the reference
configs. The reference queue cannot be deleted or archived (no API): it stays as `zz_ref_m08_routing-review`.

**Validation:** `run:m08-002fb9` (stand-in mode, `PREFIX="zz_ref_m08_"`): MODULE PASSED, 2 judge-flagged (planted
login→billing + the stand-in's refund-policy false alarm) + 1 control, kappa 0.40, flag precision 0.5, assignment ⚠️.
Leftovers (no delete/archive API for queues): queues `zz_ref_m08_routing-review`, `zz_probe_m08_queue`; archived
configs `zz_ref_m08_*`, `zz_probe_m08_*`.
