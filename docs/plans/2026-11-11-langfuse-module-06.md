# Langfuse Mastery: Module 06 (Scores) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 02 plan.

**Goal:** Module 06 opens the Evaluate phase. It teaches what a **score** is and where it can attach, and how to make quality
data trustworthy:
- **Score configs:** the schema for each score. Types are NUMERIC, BOOLEAN and CATEGORICAL, plus TEXT and CORRECTION, which have no config.
- **Score targets:** trace, observation and session.
- **Score sources:** deterministic code scores and an end-user feedback hook with corrections.
- **Silent rejection:** an invalid score is dropped without warning. The learner adds a client-side validator, tested with pytest.

Modules 03–05 are deferred. The user chose to jump to evaluation on 2026-11-11. Module 06 must not depend on them.

**Conventions:** these are the same as in the scaffold and module 02 plans:
- Work from `ch09/` and use `$PY = .venv-langfuse/Scripts/python.exe`.
- Lab tests run with `$PY -m pytest langfuse_lab/tests -q`.
- `git add` files explicitly.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Branch: `langfuse-mastery`.
- `langfuse_lab/` contains no Langfuse code except `verify.py`, which only reads data. The learner writes all the Langfuse code.

**Verified live (planner probe, 2026-11-11, server 4.77 / SDK 5.2):**
- **Creating configs:** `api.score_configs.create(name=, data_type="NUMERIC"|"BOOLEAN"|"CATEGORICAL"|"TEXT", min_value=, max_value=, categories=[{"label","value"}], description=)`.
  - **Duplicate names are allowed**, so the same name can exist twice.
  - Configs cannot be deleted, only archived with `update(id, is_archived=True)`.
  - `get(limit=100)` lists archived configs too.
  - This means the learner needs **get-or-create by name, skipping archived configs**.
- **Writing scores:** `create_score(name, value, trace_id=|observation_id=|session_id=, data_type=, config_id=, comment=, metadata=)`, plus `span.score(...)`, `span.score_trace(...)`, `langfuse.score_current_span(...)` and `score_current_trace(...)`.
  - Here `data_type` is one of NUMERIC, CATEGORICAL, BOOLEAN, TEXT or CORRECTION.
  - **CORRECTION:** the value is the corrected output string. It takes no config. The name `output` worked.
- **A score that violates its config is dropped silently.**
  - The SDK raises and logs nothing, and the score is missing from reads.
  - The only evidence is a langfuse-worker log line at `info` level: "Ingested score body not valid against provided config: Value exceeds maximum value of 1 / Value meh does not map to a valid category".
- **Reading scores:** `api.scores.get_many(trace_id=|session_id=|observation_id=|config_id=|name=, fields="details,subject")`.
  - `details` adds `comment`, `configId` and `metadata`.
  - `subject` adds `{"kind": "trace"|"observation"|"session", "id", "traceId"?}`.
  - Without `fields`, only the core fields come back: id, name, value, dataType, source, timestamp and environment.
  - BOOLEAN values read back as `True`/`False`, CATEGORICAL as the label string, and CORRECTION as the string.
- The probe left 4 archived `zz_probe_*` configs in the project. They are harmless.

---

### Task 1: traffic: module 06 demo cases (expected outcomes attached)

**Files:** modify `ch09/langfuse_lab/traffic.py` and `tests/test_traffic.py`.

- Add a frozen dataclass `ScoredCase(case_id, message, expected_handoff, expected_kb: str | None, failures: tuple, session_id, user_id)`.
- Add `score_demo_cases(*, run_id: str) -> list[ScoredCase]`. It returns exactly these 4 single-turn cases, with run-scoped ids following the existing scheme:
  1. `refund_double_charge` (from `cases.json`): expected `billing_specialist`, no KB.
  2. `shipping_status_no_handoff_needed`: expected `none`.
  3. `kb_refund_policy`: "What is your refund policy?", expected `none`, `expected_kb="kb_refunds"`. This case exercises the retriever.
  4. `reset_password_wrong_handoff` with `failures=("wrong_handoff",)`: expected `account_security`. This is the planted failure: the bad prompt routes it to billing.
- Add `score_demo_session(*, run_id)`. It returns the `s_refund_followup` ConversationSpec (2 turns, cust_00) and is used for the session-level CSAT score.
- Tests:
  - 4 cases, with exactly one carrying `wrong_handoff`.
  - Expected handoffs match `cases.json`.
  - ids are scoped by `run_id`.
  - The session spec has 2 turns.
- Commit: "lab: module 06 demo cases with expected outcomes".

### Task 2: verify: module 06 checks

**Files:** modify `verify.py`. Tests go in `tests/test_verify_modules.py`, using a fake `api.scores.get_many` and `api.score_configs.get` in `fakes.py`.

- Add helpers:
  - `scores_for_trace(client, trace_id)` and `scores_for_session(client, session_id)`, both using `fields="details,subject"` and the truncation guard.
  - `active_configs(client) -> dict[name, list[config]]`, which skips archived configs.
- Add `module_06_checks(run_tag, client, *, session_id, expected_traces=6)`. The checks use the RunSnapshot of roots plus their scores:
  1. **score configs defined once:** `handoff_correct` (BOOLEAN), `user_feedback` (CATEGORICAL with labels `thumbs_up` and `thumbs_down`), `kb_hit` (NUMERIC 0..1) and `csat` (NUMERIC 1..5). Each must exist exactly once among active configs, with that type and range. If a name appears more than once, the check fails with "get-or-create created duplicates".
  2. **every turn has a code score:** every turn root has a `handoff_correct` score with `subject.kind=="trace"` and `configId` equal to the config's id.
  3. **observation-level score:** some trace has a `kb_hit` score with `subject.kind=="observation"` whose id is a `kb-search` observation in that trace.
  4. **session-level score:** `csat` exists with `subject.kind=="session"`, `id==session_id` and a value in 1..5.
  5. **user feedback + correction:** the `wrong_handoff` trace has exactly **one** `user_feedback` score (so the score_id upsert worked), equal to `"thumbs_down"` with a non-empty comment, plus a `CORRECTION` score with a non-empty value.
  5b. **score provenance:** every `handoff_correct` and `kb_hit` score has metadata `evaluator` set.
  6. **planted failure caught:** every root tagged `wrong_handoff` has `handoff_correct == False`. If a clean case also scored False, report it in the detail as "agent error found by your score", but don't fail.
- Missing scores raise `NotYet`, so ingestion lag is retried.
- Add `check_module_06(run_tag, *, session_id, client=None, expected_traces=6, retries=12)`.
- Unit tests: one passing fixture, plus one counterexample per check (duplicate config, wrong subject kind, missing correction, planted trace scored True).
- Commit: "lab: module 06 checks (configs, trace/observation/session scores, feedback, planted wrong_handoff)".

### Task 3: learner module: pure scoring functions + their tests

**Files:** create `ch09/langfuse_mastery/scoring.py` (stubs that raise `NotImplementedError`) and `ch09/langfuse_mastery/tests/test_scoring.py`.

This follows the learn-by-building pattern: pure functions plus pytest. The learner implements them. The tests are given and fail until then.

- `validate_score(config: dict, value) -> tuple[bool, str]`. The config dict has `dataType`, `minValue`, `maxValue` and `categories` (`[{"label","value"}]`). Rules:
  - NUMERIC: the value must be a number within [min, max], where either bound may be None.
  - BOOLEAN: the value must be 0, 1, True or False.
  - CATEGORICAL: the value must be one of the labels.
  - It returns `(False, reason)` on failure.
- `handoff_correct(actual: str, expected: str) -> bool`.
- `kb_hit(returned_ids: list[str], expected_id: str | None) -> float | None`. It returns None when the case has no KB expectation.
- Tests (about 12 parametrized cases): range edges, None bounds, bool vs int, unknown category, and the None expectation.
- Add the README line: run `$PY -m pytest langfuse_mastery/tests -q` before the notebook.
- Commit: "mastery: module 06 scoring stubs + tests (learner implements)".

### Task 4: Module 06 notebook + live validation

**Files:** create `ch09/langfuse_mastery/src/06_scores.py` and build `06_scores.ipynb` with `$PY -m langfuse_mastery._nbbuild 06`.

The notebook follows the module template. Its setup is the same as module 02, with `RUN_TAG = f"run:m06-…"` and `ENV="dev"`.

1. **Concept:**
   - A score is name + value + type + target (trace / observation / session) + source (API / ANNOTATION / EVAL), with an optional config.
   - A config is the schema for one score name, so every team reports the same "quality".
   - Score sources: code (this module), end users (this module), LLM judges (07) and humans (08).
   - CORRECTION captures the right answer, which later becomes dataset ground truth (09).
   - Diagram: one trace with scores at all three targets.
2. **Demo: silent rejection.**
   - Create a throwaway config `m06_demo_0_to_1` once, get-or-create inline (given code).
   - Send one valid score and one score with value 7, then flush.
   - Read both back after a wait: only one exists.
   - Show the docker command that finds the worker log line.
   - Lesson: "validate before you send".
3. **Given cell: instrumentation recap** (not a TODO, since the learner wrote this in modules 01/02):
   - `observed_search`: the retriever records `(langfuse.get_current_observation_id(), [ids])` into a per-turn list `RETRIEVALS`.
   - `traced_case(case, agent) -> (run, trace_id)`: a `support-request` root with `propagate_attributes(session_id, user_id, tags=[RUN_TAG, case.case_id, *case.failures])`.
   - `run_conversation`, the same as module 02 but without the worker.
4. **TODO 1, `ensure_score_configs() -> dict[str, dict]`:** get-or-create by name, skipping archived configs. It creates the 4 configs from Task 2 check 1 and returns `{name: {"id", "dataType", "minValue", "maxValue", "categories"}}`. Running it twice must create nothing; the cell runs it twice and asserts the ids are equal.
5. **TODO 2, implement `scoring.py`** (`validate_score`, `handoff_correct`, `kb_hit`). The cell runs `!$PY -m pytest langfuse_mastery/tests -q` and then `importlib.reload(scoring)`.
6. **TODO 3, `score_case(case, run, trace_id, configs)`: code scores.**
   - A trace-level `handoff_correct`, with a comment like "expected X, got Y" when wrong.
   - An observation-level `kb_hit` on each `RETRIEVALS` entry, when `case.expected_kb` is set.
   - Each score goes through a helper `send_score(configs, name, value, **target)` that calls `validate_score` first and **raises** on invalid values instead of sending. It also stamps `metadata={"evaluator": "code:v1"}`, so every score records which code produced it.
7. **TODO 4, the feedback hook.**
   - `record_feedback(trace_id, *, thumbs_up: bool, comment="", correction: str | None = None)`. It writes `user_feedback` (CATEGORICAL `thumbs_up`/`thumbs_down`) and, when given, a `CORRECTION` score named `output`.
   - `record_csat(session_id, rating: int)`, a session-level score.
   - The run cell simulates the user: thumbs_down plus the case's `reference_answer` as the correction whenever `handoff_correct` is False, thumbs_up otherwise, and CSAT 4 on the session.
   - **Update, not duplicate:** `record_feedback` uses a deterministic `score_id` (`f"{trace_id}-user_feedback"`), so a user who changes their mind overwrites the score. The run cell clicks thumbs_up first on the wrong-handoff trace, then thumbs_down. Exactly one `user_feedback` score must remain.
   - **Read back:** `feedback_report(trace_id)` uses `api.scores.get_many(trace_id=, fields="details,subject")` to print name, type, target, value and comment. This is the export path for pipelines.
8. **Run:** 4 cases plus the 2-turn session, about 6 traces and about 18 Azure calls. Print a table: case → expected → actual → handoff_correct.
9. **Verify:** `verify.check_module_06(RUN_TAG, session_id=SESSION.session_id, expected_traces=6)`.
10. **UI tour:**
    - The Scores column in the traces table.
    - Filter traces on `handoff_correct = false`.
    - Open the wrong-handoff trace and see feedback plus correction.
    - The `kb_hit` score on the `kb-search` observation.
    - The Sessions view CSAT.
    - Settings → Score Configs.
    - The Scores analytics page.
11. **Production notes:**
    - Define configs once (infra-as-code) and reference them by id.
    - Validate client-side, because the server drops invalid scores silently.
    - Feedback from the browser should go through your backend (never ship secret keys). Langfuse also has a public-key-only browser SDK path.
    - Corrections are ground truth for datasets (09).
    - Don't score PII into comments.
    - Leadership line: "One consistent definition of quality, measured on every request, that every team reports against."

**Live validation**, the same as modules 00–02:
- Create `_validate_06.ipynb` with reference solutions for the TODOs, plus a reference `scoring.py` swapped in temporarily.
- Run it with `nbconvert` from `langfuse_mastery/`. It must print `MODULE PASSED` and must show the planted `wrong_handoff` caught.
- Restore the stub `scoring.py` and delete the validation notebook.
- Fix hints and checks against **real** data. Ask before changing verify semantics.
- The committed notebook has TODOs only and no outputs.

Update `README.md` (module 06 available, 03–05 deferred) and the syllabus artifact (06 → Ready).

Commit: "mastery: module 06 (score configs, trace/observation/session scores, feedback + corrections)".
