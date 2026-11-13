# Langfuse Mastery: Module 09 (Datasets & experiments) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 06/07/08 plans.

**Goal:** module 09 turns labelled production traces into a **regression dataset** and uses it to decide, with data,
whether a prompt change may ship. The learner:
- builds a dataset **from traces** (labelled by module-06-style code scores + CORRECTION scores; "every confirmed
  failure becomes a labelled example", module 08), plus a few curated items, idempotently (dedupe by input);
- **versions** it: fixes a wrong label (upsert = new item version), archives a retired item, and **pins** the dataset
  version every experiment runs on;
- writes **item evaluators** (`handoff_correct` with module 06's score config, `concise`), a **composite evaluator**
  (`quality`) and **run evaluators** (`handoff_accuracy`, `quality_avg`);
- runs an **experiment per prompt label**: `production` (v1, good) vs `staging` (v2, the module 04 bad candidate);
- **compares** the two runs through the public experiments API, lists the regressed items and gates the candidate;
- writes the receiver for a **remote experiment trigger** (Langfuse's "Run remote experiment" webhook: body + HMAC
  signature), exercised by a local simulation of Langfuse's POST;
- optionally runs a **UI prompt experiment** (needs module 07's LLM connection).

Scenario (design row 09): "Regression dataset; prompt v1 vs v2 experiment". **Planted failure:** prompt v2
(`staging` = `WRONG_HANDOFF_TEMPLATE`) regresses login routing; the comparison must catch it: v2's `handoff_accuracy`
is below v1's, the regressed items are identified (all expect `account_security`) and the gate says `block`.

**Earlier modules' objects need not exist.** A GIVEN cell get-or-creates the managed prompt (module 04 contract,
by content, labels only set when missing), module 06's `handoff_correct` config, and the labelled source traffic
(4 module-06 cases, the login one on the bad prompt). No dependency on 06/07/08 having been run, nor on the learner's
stubs (`scoring.py`, `prompting.py`, ...).

**Conventions:** as in modules 07/08 (`ch09/`, `$PY`, explicit `git add`, trailer, branch `langfuse-mastery`, no
Langfuse code in `langfuse_lab/` except the read-only `verify.py`). Project-global names are `PREFIX + base`; the
learner uses `PREFIX = ""`, validation `zz_ref_m09_`.

| object | name (`PREFIX=""`) |
|---|---|
| dataset | `support-regression` |
| managed prompt (module 04) | `support-agent-system` (`production` = good, `staging` = bad) |
| score config (module 06) | `handoff_correct` (BOOLEAN) |
| experiment name | `prompt-routing` (run names `production v<N> · <run id>`, `staging v<N> · <run id>`, `remote … · <run id>`) |
| optional chat prompt (UI prompt experiment) | `support-agent-chat` |

**Verified live (probe, 2026-11-12, server 4.77 / SDK 5.2; probe objects `zz_probe_m09_*`):**
- **Datasets:** `langfuse.create_dataset(name, description, metadata, input_schema, expected_output_schema)` is an
  **upsert by name** (same id; description overwritten, omitted fields kept). `api.datasets.get(name)` (404
  `NotFoundError` when missing), `api.datasets.list(page, limit)`. **No delete endpoint for datasets.**
- **Items:** `langfuse.create_dataset_item(dataset_name, input, expected_output, metadata, source_trace_id,
  source_observation_id, status, id)`. **Same `id` = upsert = a new item VERSION** (`item.version` = when it became
  current; `created_at` moves too). A status-only upsert keeps input/expected. `status="ARCHIVED"` archives.
  - `api.dataset_items.list(dataset_name, version=dt, page, limit)` returns the items **as of `version`**
    (verified: `version = item.version - 1 ms` returns the previous content). **Archived items are not listed**
    (now or at a version); `api.dataset_items.get(id)` still returns them with `status=ARCHIVED`.
  - `langfuse.get_dataset(name, version=dt)` → `DatasetClient` with the items as of `dt` (`.items`, `.version`).
  - **Clock skew:** the server stamps versions with its own clock (≈3 s behind this host). Pin versions with a
    server timestamp (`max(item.version)`), never with the local `datetime.now()`.
- **Legacy dataset-run endpoints are gone:** `/api/public/datasets/{name}/runs` and `/api/public/dataset-run-items`
  answer 404 "not available on deployments running in Langfuse v4 events_only mode". SDK 5.2 has no dataset-run
  client either. Experiments are written as **OTel attributes** on the traces and read with **`api.experiments`**.
- **`run_experiment`** (`langfuse.run_experiment(name, run_name, description, data, task, evaluators,
  composite_evaluator, run_evaluators, max_concurrency, metadata: dict[str,str], experiment_id)` and
  `DatasetClient.run_experiment(...)`, which also records the pinned version):
  - one trace per item: `experiment-item-run` (root) → `experiment-item-task` (the task's spans nest here) →
    `experiment-item-evaluation` → one `EVALUATOR` observation per evaluator; environment **`sdk-experiment`**.
  - a **sync task runs sequentially** (it blocks the event loop), so one shared `SupportAgent` is safe.
  - item evaluators return `Evaluation(name, value, comment, metadata, data_type, config_id)`; the scores land on the
    `experiment-item-task` observation. The composite evaluator gets `evaluations=[Evaluation...]`. Run evaluators
    get `item_results`; their scores attach to the **experiment** (`subject.kind = "experiment"`).
  - **experiment id** = sha256(["langfuse-experiment-v1", project, dataset, run_name])[:16]: **re-running with the
    same `run_name` appends to the same experiment** (duplicate items, seen: itemCount 4 for 2 items). Use unique
    run names.
  - `propagate_attributes(tags=...)` inside the task tags only the task subtree, not the experiment root.
- **Reading experiments** (`api.experiments`, cursor-paginated, `from_start_time` required):
  - `list(from_start_time, dataset_id=, name=, id=, fields="core,metadata,scores")` → `id, name (= run_name),
    description, startTime, endTime, itemCount, datasetId, metadata, scores` (run-level scores).
  - `list_items(from_start_time, experiment_id=, dataset_id=, fields="core,dataset,io,metadata,scores")` →
    `id (task observation), traceId, environment, experimentId, experimentName, experimentItemId (= dataset item id),
    experimentDatasetId, experimentItemVersion (the pinned version; only with `dataset`), input/output/expectedOutput
    (JSON **strings**), metadata, experimentItemMetadata, experimentMetadata, scores` (item scores, values typed).
  - `api.scores.get_many(experiment_id=...)` exists too.
- **Remote experiment trigger (UI "Run remote experiment"):** configured per dataset in the UI only (tRPC
  `datasets.upsertRemoteExperiment`: url, default payload, enabled, signing, headers). **Not in the public API** and
  not in the dataset read model. On trigger the web container POSTs `JSON.stringify({projectId, datasetId,
  datasetName, payload})` (payload = the default payload **string**, or the one edited in the dialog) with
  `x-langfuse-signature: t=<unix>,v1=<hex HMAC-SHA256(secret, f"{t}.{body}")>` when signing is on (secret
  `lf-whsec_…`), 20 s timeout. The URL must be http(s) on **port 80 or 443** and pass the outbound SSRF check
  (private/loopback hosts need `LANGFUSE_WEBHOOK_WHITELISTED_HOST`/`_IPS`, unset here). → the notebook simulates the
  exact POST locally; verify reports the UI-side config ⚠️.
- **UI prompt experiments:** run by the worker with an LLM connection (module 07), traces in environment
  **`langfuse-prompt-experiment`**. A **text** prompt is compiled with the item's input keys as variables and sent as
  a single **system** message (useless for a support agent); a **chat** prompt with a `{{message}}` user turn works.
  Not triggerable through the public API → optional UI step, verify ⚠️ unless such items exist on the dataset.

---

### Task 1: lab: regression cases + item ids + Langfuse's signature scheme
`langfuse_lab/regression.py` (no Langfuse code):
- `SOURCE_CASES(run_id)` = module 06's 4 `ScoredCase`s (planted login on the bad prompt).
- `CURATED`: `locked_out_cust_03` (account_security), `invoice_request` ("Can you send me the invoice for March?",
  `expected_handoff="none"` as in `cases.json`, which contradicts the routing policy: the label fix),
  `prompt_injection` (retired → archived).
- `LABEL_FIX = ("invoice_request", "billing_specialist")`, `RETIRED = "prompt_injection"`.
- `dataset_item_id(dataset_name, message)`: deterministic id (dedupe by input).
- `SIGNATURE_HEADER`, `sign(body, secret, timestamp)`, `trigger_body(project_id, dataset_id, dataset_name, payload)`.
- Tests. Commit: "lab: module 09 regression cases, item ids, remote-trigger signature".

### Task 2: verify: module 09 checks
`module_09_checks(run_tag, client, *, prefix="", comparison=None, decision=None, expected_traces=4)`; one snapshot:
source roots + their scores, dataset, active items, the label-fix item's previous version, the retired item,
this run's experiments (`metadata.run_tag`), their items (+ scores), the generations' prompt links, the prompt
labels, active configs, prompt-experiment items on the dataset.
1. regression dataset: exists, ≥6 active items, each `input.message` + `expected_output.handoff`, one item per message.
2. items from the labelled traces: each source trace's message is an item with `source_trace_id`, expected handoff =
   the trace's `handoff_correct` label, label `confirmed_failure`/`golden`; the planted one carries the CORRECTION.
3. versioning: the label-fix item now expects `billing_specialist` and its previous version expected `none`; the
   retired item is ARCHIVED.
4. two variants on the pinned dataset: one `production` and one `staging` run (trigger notebook), each covering
   every active item exactly once, same non-null `experimentItemVersion`, `prompt_version` = the label's version.
5. every experiment generation linked to its run's prompt version.
6. item + composite evaluators: `handoff_correct` (config id, value = recomputed from output vs expected),
   `concise`, `quality` in [0, 1] on every item.
7. run evaluators: `handoff_accuracy` = mean of the items' `handoff_correct`; `quality_avg` present.
8. the learner's comparison = recomputed (n, baseline/candidate rates, regressed, improved).
9. planted: staging regresses login routing, regressed items all expect account_security, decision `block`.
10. remote trigger: an experiment with `metadata.trigger = "remote"` and scored items.
11. remote-run URL config: ⚠️ (UI only, not readable; port/SSRF constraints).
12. UI prompt experiment: pass if items in `langfuse-prompt-experiment` exist on the dataset, else ⚠️ (optional).
Commit: "lab: module 09 checks (...)".

### Task 3: learner module `langfuse_mastery/experiments.py` + tests
`item_from_trace`, `changed`, `concise`, `quality`, `compare_runs`, `gate`, `verify_signature`. Stubs +
`tests/test_experiments.py`. Commit: "mastery: module 09 experiments stubs + tests (learner implements)".

### Task 4: notebook `src/09_datasets_experiments.py` + live validation
TODO 1 experiments.py; Given (instrumentation, prompt + config get-or-create, labelled source traffic); TODO 2
`build_dataset`; TODO 3 `revise_item` + pin `DATASET_VERSION`; TODO 4 evaluators; Given `serve_prompt`/`make_task`;
TODO 5 `run_variant(label)`; TODO 6 `compare(...)` → `COMPARISON`, `DECISION`; TODO 7 `handle_remote_trigger`
(given local server + simulated Langfuse POST); optional UI prompt experiment; verify; UI tour; notes.
Validation with `PREFIX="zz_ref_m09_"`, reference `experiments` monkey-patched. ≈54 Azure calls (4 source turns +
2 × 6 items + 2 remote items, ~3 calls per turn).

---

### As implemented (2026-11-12)

- **New live facts from validation:**
  - An `Evaluation` (or `create_score`) with a `config_id` is stored under the **config's name**:
    `Evaluation(name="handoff_correct", config_id=<zz_ref_m09_handoff_correct>)` reads back as
    `zz_ref_m09_handoff_correct`. So every score name that has a config is `PREFIX + base`, and
    `experiments.item_from_trace` takes `label_score=`.
  - `experiments.list_items(fields=...scores)` item scores carry **no `configId` / comment / metadata**; verify reads
    `configId` with `scores.get_many(trace_id=<experiment trace>)`. `scores.get_many(experiment_id=)` returns only the
    run-level scores.
  - gpt-4.1-mini on v1 also misses the vague login ("I cannot log in after changing my password." → no handoff) and,
    once, the invoice item. Averages tied at 0.67 (v1 and v2), with one login regression (locked-out cust_03 → billing)
    and one improvement: the planted check gates on **regressed login items + decision block**, not on the mean (the
    first draft required candidate < baseline; that contradicted the module's own "gate on items" lesson).
- **Validation:** run 1 `run:m09-d23251` stopped (score name prefixed by the config; fixed). Run 2 `run:m09-99f743`
  (`PREFIX="zz_ref_m09_"`, reference `experiments.py` swapped in): every check passed except the over-strict planted
  check; after the fix above, `verify.check_module_09` on the same run's stored data printed **MODULE PASSED**
  (remote-run config ⚠️, UI prompt experiment ⚠️ optional).
- **Leftovers (no delete API / deletes blocked):** datasets `zz_ref_m09_support-regression` (all items archived) and
  `zz_probe_m09_ds` (all items archived); prompt `zz_ref_m09_support-agent-system` (v1 production, v2 staging);
  archived config `zz_ref_m09_handoff_correct`; experiments/traces of both runs and the probes (`zz_probe_m09_run_a/_b`)
  in environment `sdk-experiment`.
