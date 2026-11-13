# Langfuse Mastery: Module 07 (Live evaluation) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 06 plan.

**Goal:** module 07 moves scoring from notebook code into Langfuse itself. The learner configures **server-side
LLM-as-a-judge evaluators** that score live traffic as it is ingested, with no scoring code in the request path:
- an **LLM connection** to Azure OpenAI that the langfuse **worker container** calls,
- **evaluators** (the *how*: prompt, structured output, model), versioned on change,
- an **evaluation rule** (the *which*: filters, sampling, variable mapping, including a `metadata` JSONPath),
- **judge calibration**: the judge's routing verdict vs. module 06's deterministic `handoff_correct` code score,
- **code evaluators** (TypeScript `evaluate(ctx)`), honestly reported as ⚠️ on this deployment.

Module 07 builds on module 06 (`scoring.py`, the `handoff_correct` config, `send_score` provenance) and module 04's
routing policy. It does not need modules 03–05.

**Conventions:** the same as the module 06 plan (`ch09/`, `$PY`, explicit `git add`, trailer, branch
`langfuse-mastery`, no Langfuse code in `langfuse_lab/` except the read-only `verify.py`).

**Project-global names are prefixed.** Evaluators, rules and LLM connections are project-global and the harness may
not delete them. The notebook defines `PREFIX = ""`; every global name is `PREFIX + base`. The reference validation
runs with `PREFIX = "zz_ref_m07_"`, so its leftovers never collide with the learner's objects and its rule filters on
the inert tag `zz_ref_m07_live-eval`.

| object | name (`PREFIX=""`) |
|---|---|
| LLM connection (provider) | `azure-openai` |
| evaluator (BOOLEAN) | `routing_judge` |
| evaluator (NUMERIC 0..1) | `helpfulness_judge` |
| evaluation rule | `support-live-eval` |
| opt-in trace tag | `live-eval` |
| code evaluator (optional) | `handoff_code` |

**Verified live (probe, 2026-11-12, server 4.77 / SDK 5.2; probe objects named `zz_probe_m07_*`):**
- **LLM connection:** `api.llm_connections.upsert(provider=, adapter="azure", secret_key=, base_url="https://<resource>.cognitiveservices.azure.com/openai", custom_models=[<deployment names>], with_default_models=False)`.
  Upserted **on provider**, so it is idempotent. `list()` returns it without the secret (`displaySecretKey`).
  The worker container reached Azure with this base URL; both `gpt-4.1-mini` and `gpt-5.2-chat` worked as judges
  (gpt-5.2-chat's temperature restriction does not bite: Langfuse sends no temperature).
- **Evaluators** (`api.evaluators`, public API v2):
  - `create(request=CreateEvaluatorRequest_LlmAsJudge(name, prompt, model_config_=EvaluatorModelConfig(provider, model), variable_mapping=[...], output_definition=EvaluatorOutputDefinition_Boolean|_Numeric|_Categorical(...)))`.
  - **SDK trap:** the field is `model_config_` (trailing underscore). `model_config=` is silently treated as an extra
    field and the server answers 400 `Unrecognized key: "model_config"`.
  - **Create/update are slow (~15 s)**: the server validates the model with a real LLM call. The SDK's default
    timeout is shorter, so pass `request_options={"timeout_in_seconds": 90}`. **A create that times out on the client
    is still created on the server**: retrying blindly makes duplicates. Names are not unique, so get-or-create by
    name (list first) is mandatory.
  - `update(id, request=UpdateLlmAsJudgeEvaluatorRequest(prompt=, model_config_=, variable_mapping=, output_definition=))`
    replaces the whole definition and bumps `version` (1 → 2). Rules always use the latest version.
  - `variable_mapping` accepts dicts (`{"variable", "source", "jsonPath"}`), but `output_definition` must be the
    typed class (a dict fails the `data_type` discriminator client-side). Read-back mapping objects
    (`PromptVariableMappingRead`) cannot be passed back into an update: convert them to dicts.
  - The server extracts `variables` from `{{...}}` in the prompt and rejects incomplete mappings:
    400 `Missing mappings for evaluator variables: handoff, answer`.
  - Read shape: `.status` (`active`/`paused`), `.paused_reason`, `.paused_message`, `.variables`,
    `.variable_mapping`, `.model_config_`, `.output_definition`, `.version`, `.version_id`, `.type`.
  - `list(limit=, cursor=)` pages with `meta.cursor` (None on the last page).
- **Code evaluators:** `create(request=CreateEvaluatorRequest_Code(source_code=, source_code_language="TYPESCRIPT"|"PYTHON"))`
  answers **412 `Code evaluations are not enabled for this deployment.`** The worker needs
  `LANGFUSE_CODE_EVAL_DISPATCHER` (`insecure-local`: TypeScript only, runs in the worker process via `node:vm`;
  `aws-lambda` for production). Contract (from the worker source): `function evaluate(ctx)` with
  `ctx.observation.{input, output, metadata, toolCalls}`, returning `{scores: [{name, value, dataType?, comment?, configId?, metadata?}]}`.
  Unlike LLM judges, code-evaluator scores **can** carry a `configId`. → verify ⚠️ `FeatureUnavailable`.
- **Evaluation rules** (`api.evaluation_rules`):
  - `create(name, enabled=True, evaluator_assignments=[{evaluator_id, variable_mapping?}], sampling=1.0, filter=[...])`; filters accept dicts.
  - Rules target **observations**, not traces. Columns and their only accepted type: `tags` arrayOptions,
    `isRootObservation` boolean, `name` stringOptions, `type` stringOptions, `environment` stringOptions,
    `traceName` stringOptions, `metadata` stringObject (+`key`), `userId`/`sessionId`/`version` string, ...
    The wrong type for a column is a 400.
  - **Trap:** `traceName` did **not** match our roots (no scores at all), because the observation record only carries a
    trace name when `trace_name` is propagated explicitly. Filter on the root observation's **`name`** instead.
    After switching the probe rule's filter to `name` (via `update(id, filter=...)`), the next traces were scored
    within ~25 s.
  - **Not retroactive:** traces ingested before the rule matched were never scored. Create the rule *before* traffic.
  - `update(id, enabled=False)` disables a rule; `update(..., evaluator_assignments=[])` also disables it.
- **Judge scores:** `name` = the evaluator name, `source="EVAL"`, `dataType` from the output definition, BOOLEAN
  values read back as `True`/`False`, `comment` = the judge's reasoning, **`configId=None`** (LLM judges never link a
  score config), `subject={"kind": "observation", "id": <root observation id>, "traceId": ...}`, and metadata with
  provenance: `evaluator_id`, `evaluator_version_id`, `evaluation_rule_id`, `job_execution_id`,
  `target_observation_id`, `target_trace_id`. Each execution also writes an internal trace
  `Execute evaluator: <name>` in environment `langfuse-llm-as-a-judge` (judge cost/latency is observable there).
- The probe's routing judge (gpt-5.2-chat, BOOLEAN, mapping `handoff` ← `metadata` `$.handoff_target`) scored the
  correct login→account_security trace `True` and the login→billing trace `False`.

---

### Task 1: traffic: module 07 cases (one not opted in)

**Files:** modify `ch09/langfuse_lab/traffic.py` and `tests/test_traffic.py`.

- `live_eval_cases(*, run_id) -> list[ScoredCase]`: module 06's 4 cases (with the planted `wrong_handoff` login case)
  plus `returns_policy_unjudged` ("How long do returns take?", expected `none`). Run-scoped ids as before.
- `UNJUDGED_CASES = {"returns_policy_unjudged"}`: the run cell does **not** tag that trace `live-eval`, so the rule
  must leave it alone.
- Tests: 5 cases, exactly one planted, exactly one unjudged, ids scoped by `run_id`.
- Commit: "lab: module 07 live-eval cases (planted wrong_handoff + one not opted in)".

### Task 2: verify: module 07 checks

**Files:** modify `verify.py`; tests in `tests/test_verify_m07.py` with fakes shaped like the live responses above.

`module_07_checks(run_tag, client, *, prefix="", live_tag="live-eval", agreement=None, expected_traces=5)`, one snapshot
(roots + per-trace scores + connections + evaluators + rules) per attempt:
1. **LLM connection:** provider `prefix+"azure-openai"` exists, adapter `azure`, at least one custom model.
2. **evaluators defined once:** exactly one `routing_judge` (llm_as_judge, BOOLEAN) and one `helpfulness_judge`
   (NUMERIC 0..1), status `active` (else print `paused_message`), model config on the connection's provider with a
   model from its `customModels`, and the routing judge maps some variable from `metadata` with a `jsonPath`.
3. **rule:** exactly one `support-live-eval`, enabled, sampling 1, filters `tags any of [live_tag]`,
   `isRootObservation = true`, `name any of ["support-request"]`, and **no `traceName`** filter (it never matches,
   see above); both evaluators assigned.
4. **every opted-in trace judged:** each `live-eval` root has both judge scores, `source=EVAL`, on the root
   observation (`subject.kind=observation`, `id=root.id`), `metadata.evaluator_id` = that evaluator, non-empty reasoning.
   Missing → `NotYet`.
5. **filter scopes the rule:** the unjudged root has no EVAL score (checked once the others are judged).
6. **calibration vs code score:** every judged trace has a `handoff_correct` code score (configId set,
   `metadata.evaluator`), and the learner's `agreement` dict equals the one recomputed from the scores (`n`,
   `agree`, `judge_flagged`, `code_flagged`, `both_flagged`).
7. **planted wrong_handoff caught by the judge:** the planted root's `routing_judge` is False. A clean trace judged
   False is reported as a disagreement (not a failure).
8. **code evaluator:** if `prefix+"handoff_code"` exists it must be type `code`, assigned to the rule, and its scores
   appear; otherwise ⚠️ `FeatureUnavailable` with the dispatcher explanation.

`check_module_07(...)` wraps it with `_check_module`. Unit tests: a passing fixture plus a counterexample per check.
Commit: "lab: module 07 checks (connection, evaluators, rule, judge scores, filter scope, calibration, planted wrong_handoff)".

### Task 3: learner module `langfuse_mastery/judging.py` + tests

Stubs raising `NotImplementedError`; tests in `langfuse_mastery/tests/test_judging.py`.
- `prompt_variables(template) -> list[str]`: `{{ name }}` variables in first-seen order, deduped.
- `variable_mapping(spec: dict[str, str]) -> list[dict]`: `"input"` → `{"variable", "source"}`;
  `"metadata:$.handoff_target"` → adds `"jsonPath"`. Unknown source or a jsonPath not starting with `$` → `ValueError`.
- `check_mapping(template, mapping)`: `ValueError` naming missing / extra variables (what the server would reject).
- `rule_filters(live_tag, root_name) -> list[dict]`: the three filters with their exact types.
- `as_verdict(value) -> bool | None`: True/False/1/0/"true"/"false" → bool; None/other → None.
- `agreement(pairs) -> dict`: pairs of (judge_ok, code_ok); ignores pairs with a None; returns `n`, `agree`,
  `rate` (None when n=0), `judge_flagged`, `code_flagged`, `both_flagged`, `judge_missed`, `judge_false_alarm`.
Commit: "mastery: module 07 judging stubs + tests (learner implements)".

### Task 4: notebook `src/07_live_evaluation.py` + live validation

Bootstrap like module 06 (`RUN_TAG = f"run:m07-…"`, `PREFIX`, names table). Cells:
1. Concept table (connection / evaluator / rule / filter / mapping / sampling / judge score / code evaluator) + diagram
   (ingestion → worker → judge LLM → score on the root observation).
2. TODO 1 `judging.py` + its test cell.
3. Given: instrumentation recap (root `support-request` with `metadata.handoff_target`, tags incl. `live-eval` only for
   opted-in cases), `handoff_correct` config get-or-create, `send_score` (module 06 convention).
4. TODO 2 `ensure_llm_connection()`.
5. TODO 3 `ensure_evaluator(name, *, prompt, output_definition, mapping, model)`: get-or-create-or-update by name,
   timeout, version bump only on change. Run twice → same id, no new version.
6. TODO 4 `ensure_rule(evaluators)`: get-or-create-or-update by name with `judging.rule_filters`.
7. Run: 5 cases (~15 agent calls), code score `handoff_correct` via `send_score`; judges run in the worker (8 calls).
8. TODO 5 `collect_verdicts(rows, timeout=180)`: poll EVAL scores, print the judge-vs-code table, return pairs;
   `REPORT = judging.agreement(pairs)`.
9. TODO 6 (optional) code evaluator: TypeScript source + create; catch the 412 and print why.
10. Verify, UI tour (Evaluators, Rules, judge traces in `langfuse-llm-as-a-judge`, score comment = reasoning), notes.

**Live validation:** `_validate_07.ipynb` with `PREFIX="zz_ref_m07_"`, the reference TODOs spliced in, and a monkey-patched
reference `scoring` / `judging` (no edits to the learner's files). Must print `MODULE PASSED`. Then disable the
reference rule, archive the reference `zz_ref_m07_handoff_correct` config, delete the validation notebook.
Commit: "mastery: module 07 (...)".
