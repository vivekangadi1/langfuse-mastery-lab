# Langfuse Mastery: Module 10 (CI/CD gate) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 06/07/08/09 plans.

**Goal:** module 10 turns module 09's one-off comparison into a **release gate that runs in CI**: a pytest suite that
runs the candidate prompt label as a Langfuse experiment on the **pinned regression dataset**, compares it item by item
with the **production baseline**, re-runs only the suspicious items to separate real regressions from LLM noise,
records the decision **on the experiment** (a run-level score with the CI context), writes JUnit + Markdown artifacts,
and exits non-zero on a regression. The learner:
- writes the gate's pure logic (`langfuse_mastery/ci_gate.py`, pytest): CI context from GitHub Actions env vars, run
  names + experiment metadata, pass rates over attempts, **majority-vote confirmation** of suspects, the decision
  (`ship`/`block`/`error` + exit code; reuses module 09's `experiments.gate`), report rows, JUnit XML, Markdown;
- writes the Langfuse side (`langfuse_mastery/ci/release_gate.py`): `run_variant` (pinned dataset, CI metadata,
  optional item subset), `verdicts` (read back through `api.experiments.list_items`), `baseline_for` (**cached
  baseline**: reuse a production run with the same cache key, else run it), `evaluate_candidate` (baseline → candidate →
  compare → re-run suspects on both sides → confirm → decide) and `record_gate` (`create_score(dataset_run_id=…)`);
- writes the pytest test (`ci/test_release_gate.py`) and the GitHub Actions workflow (`ci/release-gate.yml`, committed
  but never executed remotely: it lives outside `.github/workflows/`);
- iterates for free with a **dry-run harness** (an oracle task, no Azure calls; `staging` simulated as login → billing);
- drives the real gate from the notebook as a subprocess: `CANDIDATE_LABEL=staging` must exit 1 and name the regressed
  login items; `CANDIDATE_LABEL=candidate` (a clean prompt version) must exit 0.

Scenario (design row 10): "pytest / GitHub Actions gate that fails on a planted regression". **Planted failure:**
`staging` = module 04's `WRONG_HANDOFF_TEMPLATE` (login → billing). The gate must block it, with ≥1 **confirmed**
regressed item that expects `account_security`, exit code 1, and the decision recorded on the experiment. The clean
`candidate` (production's text with a clarified login rule) must ship.

**Earlier modules' objects need not exist.** `harness.bootstrap()` (a GIVEN notebook cell; never called by CI)
get-or-creates the prompt (by content; `production` = good, `staging` = bad, `candidate` = clean; a label is only set
when missing), module 06's `handoff_correct` config, and **seeds the regression dataset only when it doesn't exist**,
with exactly the items module 09's `build_dataset` would write (same ids, curated labels as in 09 *before* its label fix),
so a later module 09 run still passes its own checks. In CI the harness is **read-only** (`Harness.from_env`): it fails
with a clear message if a label or the dataset is missing. Module 09 TODO 1 (`experiments.compare_runs` / `gate`) is a
prerequisite; the notebook's first cell checks it.

**Conventions:** as in modules 07–09. Project-global names are `PREFIX + base` (`GATE_PREFIX` env var for the CI
process); the learner uses `PREFIX = ""`, validation `zz_ref_m10_`.

| object | name (`PREFIX=""`) |
|---|---|
| dataset | `support-regression` (module 09's) |
| managed prompt | `support-agent-system`: `production` (good), `staging` (bad), **`candidate`** (clean, new) |
| score config | `handoff_correct` (module 06) |
| experiment name | `release-gate` (run names `ci <candidate> · <role> <label> v<N> · <sha7> · <ci run>[ #k]`) |
| gate score | `ci_gate` (CATEGORICAL `ship`/`block`/`error`, on the candidate experiment, no config) |

**Verified live (probe, 2026-11-12, server 4.77 / SDK 5.2; probe objects `zz_probe_m10_*`):**
- **Run-level scores on an experiment:** `langfuse.create_score(dataset_run_id=<experiment id>, name, value,
  data_type="CATEGORICAL", comment, metadata)` works without a config (this is what `run_evaluators` do internally).
  Read back with `api.experiments.list(id=…, fields="core,metadata,scores")` (scores carry **no comment/metadata**) or
  `api.scores.get_many(experiment_id=…, fields="details,subject")` (comment + metadata with lists/dicts intact,
  `subject.kind = "experiment"`).
- **Experiment metadata:** `run_experiment(metadata=…)` values are **stringified** (`3` → `"3"`), not truncated (300
  chars came back whole), and copied onto every item's metadata.
- `api.experiments.list` filters by `id`, `name`, `dataset_id` only (a `filter` param supports the same columns): **no
  metadata filter**, so a baseline cache lookup lists the dataset's experiments and filters client-side.
- `DatasetClient.items` can be sliced/filtered before `run_experiment` (re-run only some items); the pinned
  `experimentItemVersion` is kept.
- Agent (Azure, 3 turns): production v1 and the clean candidate both route "I cannot log in after changing my
  password." and the locked-out cust_03 to `account_security` at temperature 0.

---

### Task 1: lab: clean candidate prompt, CI workflow checker
- `prompts.CLEAN_CANDIDATE_TEMPLATE` (production + "with or without it, ALWAYS create_handoff to account_security"),
  `prompts.CANDIDATE_LABEL = "candidate"`.
- `regression.seed_items(dataset_name)`: the items module 09's `build_dataset` would write (pure, ids from
  `dataset_item_id`), for the harness to seed a missing dataset.
- `langfuse_lab/ci_workflow.py`: `workflow_problems(yaml_text) -> list[str]` (what a usable release-gate workflow must
  have; PyYAML parses `on:` as `True`). Tests. Commit: "lab: module 10 clean candidate prompt, seed items, workflow checker".

### Task 2: verify: module 10 checks
`module_10_checks(run_tag, client, *, prefix="", clean_label="candidate")`; one snapshot: dataset, labels, the
dataset's experiments (48 h), this run's non-dry gate experiments (`metadata.run_tag`), referenced baselines, their
items + scores, the `ci_gate` scores (`scores.get_many(experiment_id=…, fields="details")`).
1. gate runs carry CI metadata (git sha, branch, pr, ci_run, candidate label = its prompt label + current version, trigger, baseline id, dataset version; sha in the run name).
2. baseline = production on the same pinned dataset version, every pinned item once (a shared/cached baseline is fine).
3. only the suspects were re-run, on both sides, `policy.reruns` times.
4. the gate result is recorded on the candidate experiment and matches the recomputation (majority vote + gate rules with the recorded policy).
5. planted: staging blocked, exit code 1, confirmed regressed login item(s) named.
6. the clean candidate shipped (exit code 0) and is a different prompt version from production and staging.
Commit: "lab: module 10 checks (...)".

### Task 3: learner `langfuse_mastery/ci_gate.py` + tests
Stubs + `tests/test_ci_gate.py`. Commit: "mastery: module 10 ci_gate stubs + tests (learner implements)".

### Task 4: `ci/` package, notebook `src/10_ci_gate.py`, live validation
Given `ci/harness.py` + `ci/conftest.py`; learner stubs `ci/release_gate.py`, `ci/test_release_gate.py`,
`ci/release-gate.yml`. Notebook: TODO 1 ci_gate; Given bootstrap + dry harness; TODO 2 run_variant/verdicts (dry
smoke); TODO 3 baseline_for (dry: second call reuses); TODO 4 evaluate_candidate/record_gate (dry staging → block); TODO 5
test + workflow (checked); run the real gate twice as a subprocess; artifacts; verify; UI tour; notes.
Budget: 7 items (the injection item makes no LLM call): baseline 6 + staging 6 + clean 6 turns + re-runs of ~1–2 suspects
on both sides ≈ 20–22 turns ≈ 50 Azure calls; the second gate reuses the cached baseline.

---

### As implemented (2026-11-12)

- **Files:** given `ci/harness.py` (read-only `Harness.from_env`, notebook-only `bootstrap`, dry-run oracle, policy from
  `GATE_*` env vars, `baseline_key`), `ci/conftest.py` (skips unless `CANDIDATE_LABEL` is set); learner stubs
  `ci_gate.py` (+ 26 tests), `ci/release_gate.py` (TODOs 2–4), `ci/test_release_gate.py` (5a), `ci/release-gate.yml`
  (5b, linted by `langfuse_lab.ci_workflow.workflow_problems`); `ci/.gitignore` ignores `artifacts/`.
- **Rehearsal:** the whole notebook ran first with the real gate swapped to dry mode (0 Azure calls) to shake out
  plumbing; verify was exercised on the dry data by rewriting `dry_run` in a scratch client (all 6 ✅).
- **Validation:** one real run `run:m10-88215f` (`PREFIX="zz_ref_m10_"`, reference files swapped in) printed
  **MODULE PASSED** on the first try. Real behaviour: production v1 passed the vague login on the first run and
  failed it on the re-run, staging failed it both times → **flaky** (0.5 vs 0.0), not confirmed; the locked-out
  cust_03 item was 1.0 vs 0.0 → **confirmed**. Gate: block (1 regressed, mean drop 0.21 > 0.20), exit 1. The clean
  candidate shipped with 0 suspects, exit 0, reusing the cached baseline (no second production run). The seeded
  `invoice_request` item expects `none` (module 09 fixes that label), so it shows as `fail` on both sides: a known gap,
  correctly not a regression. ≈22 LLM turns (the injection item is guardrail-blocked) ≈ 50 Azure calls + 7 in probes.
- **Leftovers (deletes blocked / no delete API):** prompt `zz_ref_m10_support-agent-system` (v1 production, v2 staging,
  v3 candidate); dataset `zz_ref_m10_support-regression` (all items archived); dataset `zz_probe_m10_ds` (items
  archived); archived config `zz_ref_m10_handoff_correct`; experiments `release-gate` / `zz_probe_m10` and their
  `sdk-experiment` traces (dry + real).
