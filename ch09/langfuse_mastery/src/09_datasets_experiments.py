# %% [markdown]
# # Module 09: Datasets & experiments (datasets from traces, versioning, evaluators, prompt v1 vs v2, remote trigger)
#
# **Goal:** stop shipping prompt changes on gut feeling. Production traces that someone **labelled** (a code score, a
# correction, a human verdict) become a **regression dataset**; every candidate prompt runs on that dataset as an
# **experiment**; evaluators score each answer; and you **compare** the candidate with production item by item before
# you move a label.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **Dataset** | a named, project-global collection of test cases | `support-regression` |
# | **Dataset item** | `input` + `expected_output` + `metadata`, optionally linked to the trace it came from (`source_trace_id` / `source_observation_id`) | `{"message": …}` → `{"handoff": "account_security", "reference_answer": …}` |
# | **Item version** | every write to an item id makes a **new version**; archived items drop out | fix a wrong label without losing what it was |
# | **Dataset version** | "the items as of time T": pin it so two runs see the **same** test set | `get_dataset(name, version=DATASET_VERSION)` |
# | **Experiment (run)** | the task run once over the items: one trace per item, linked to the item, named by `run_name` | `production v1 · …` vs `staging v2 · …` |
# | **Item evaluator** | scores one output against its expected output → a score on that item's trace | `handoff_correct` (module 06's config), `concise` |
# | **Composite evaluator** | combines one item's evaluations into one number | `quality` = 0.8 · handoff + 0.2 · concise |
# | **Run evaluator** | scores the whole run (aggregates) → a score on the experiment | `handoff_accuracy`, `quality_avg` |
# | **Compare** | the same items side by side across runs: who regressed, who improved | the login items under v2 |
# | **Remote trigger** | a dataset setting in the UI: "Run" POSTs a signed webhook to **your** service, which runs the experiment | a handler that runs a variant on request |
# | **UI prompt experiment** | Langfuse runs a prompt version over the dataset with an LLM connection, no code | optional, needs module 07's connection |
#
# ```
#  production traffic ──► labelled traces (handoff_correct + CORRECTION, human verdicts from module 08)
#                               │  item_from_trace()          curated cases ─┐
#                               ▼                                            ▼
#                      dataset support-regression  ── fix a label (new item version), archive a retired item
#                               │  pin DATASET_VERSION
#              ┌────────────────┴────────────────┐
#    run_variant("production") v1      run_variant("staging") v2        (remote trigger: same code, on request)
#              │ evaluators: handoff_correct, concise → composite quality → run: handoff_accuracy, quality_avg
#              └──────────────► compare item by item ──► regressed items ──► gate: ship / block
# ```
#
# **The planted failure:** `staging` holds module 04's bad candidate (login → billing). The comparison must name the
# regressed login item(s) and the gate must say `block`, **even if the averages tie**: in validation both runs scored
# 0.67 (v1 missed the invoice, v2 got it right but sent the locked-out login to billing). That is why the gate looks
# at items, not at the mean. Labelled production failures are exactly the tests that stop the next bad prompt.
#
# Things verified live on this server (4.77 / SDK 5.2) that shape the TODOs:
# - `create_dataset` is an **upsert by name**; `create_dataset_item(id=…)` with an existing id is an **upsert that
#   creates a new item version**. Langfuse never dedupes by content: deterministic ids are your job.
# - `dataset_items.list()` **never lists archived items** (`dataset_items.get(id)` still returns them).
# - Experiments are read with **`api.experiments`** (`list`, `list_items`); the old dataset-run endpoints answer 404 on
#   v4. **The same `run_name` on the same dataset is the same experiment**: re-running appends duplicate items.
# - The server stamps versions with **its** clock (≈3 s behind this machine here): pin with a server timestamp.
#
# Docs: https://langfuse.com/docs/evaluation/experiments/datasets ·
# https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk ·
# https://langfuse.com/docs/evaluation/experiments/experiments-via-ui ·
# https://langfuse.com/docs/evaluation/experiments/remote-experiment

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import importlib
import json
import os
import secrets
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from langfuse_lab import verify, kb, regression
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.failures import FailureConfig
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_lab.prompts import (PROMPT_CONFIG, PROMPT_VARIABLES, SYSTEM_PROMPT, SYSTEM_PROMPT_TEMPLATE,
                                  WRONG_HANDOFF_TEMPLATE)
from langfuse_mastery import experiments

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
RUN_TAG = f"run:m09-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
RUN_STARTED = datetime.now(timezone.utc)
ENV = "dev"
fixtures = load_fixtures()

# Datasets, prompts and score configs are PROJECT-GLOBAL and long-lived (a dataset cannot be deleted through the
# API). Keep PREFIX empty.
PREFIX = ""
NAMES = {role: PREFIX + base for role, base in verify.REGRESSION_NAMES.items()}
DATASET, PROMPT, CONFIG_NAME = NAMES["dataset"], NAMES["prompt"], NAMES["config"]
EXPERIMENT = verify.EXPERIMENT_NAME              # "prompt-routing": the experiment name every run shares
ROOT_NAME = verify.LIVE_ROOT_NAME                # "support-request"
WEIGHTS = {CONFIG_NAME: 0.8, "concise": 0.2}  # the composite `quality` (CONFIG_NAME = "handoff_correct" for you)
MAX_SENTENCES = PROMPT_VARIABLES["max_sentences"]

SOURCE = regression.source_cases(run_id=RUN_ID)
print("run tag:", RUN_TAG)
for role, name in NAMES.items():
    print(f"  {role:<12} {name}")
for c in SOURCE:
    print(f"  source  {c.case_id:<36} expect {c.expected_handoff:<19} {c.failures or ''}")
for c in regression.CURATED:
    print(f"  curated {c.case_id:<36} expect {c.expected_handoff}")

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/experiments.py` (pure functions + pytest)
#
# Open `ch09/langfuse_mastery/experiments.py` and implement:
# - `item_from_trace(trace_input, scores, case_id=…)`: a labelled trace → a dataset item (or None if unlabelled).
# - `curated_item(…)` and `changed(existing, wanted)`: idempotent writes (every upsert makes a new item version).
# - `concise(answer, max_sentences)` and `quality(evaluations, weights)`: an item evaluator and the composite.
# - `compare_runs(baseline, candidate)` and `gate(comparison, …)`: the comparison and the release decision.
# - `verify_signature(body, header, secret, now=…)` and `trigger_request(body)`: the remote-trigger receiver.
#
# The cell runs the tests and reloads your module. Re-run it until it shows `33 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_experiments.py", "-q",
                         "-p", "no:cacheprovider"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
experiments = importlib.reload(experiments)

# %% [markdown]
# ## Given: instrumentation, the managed prompt, the score config, and labelled production traffic
# This is a recap plus setup, not a TODO:
# - `traced_turn`: the module 01/02 instrumentation (one `support-request` trace per request, the root carries
#   `metadata.handoff_target` and `case_id`).
# - `ensure_prompt()`: module 04's contract. `support-agent-system` with the good template and the bad candidate,
#   get-or-create **by content**; a label is only set when it doesn't exist yet. If you finished module 04 this
#   changes nothing: `production` = good, `staging` = bad.
# - `ensure_config()`: module 06's `handoff_correct` BOOLEAN config (get-or-create, skipping archived configs).
# - `label_trace()`: module 06's code score on each production trace: `handoff_correct` with the **expected** team
#   in its metadata (that is the label a dataset item needs), plus a `CORRECTION` score (the right answer) when the
#   agent got it wrong. In module 08 a human confirmed failures like these in the review queue.

# %%
from langfuse import Evaluation, get_client, observe, propagate_attributes
from langfuse.api import NotFoundError
from langfuse.langchain import CallbackHandler

langfuse = get_client()
PROJECT_ID = langfuse.api.projects.get().data[0].id
UI = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


@observe(name=ROOT_NAME, as_type="agent", capture_input=False, capture_output=False)
def traced_turn(case, agent):
    """One production request = one trace. Returns (AgentRun, trace_id, root observation id)."""
    with propagate_attributes(session_id=case.session_id, user_id=case.user_id,
                              tags=[RUN_TAG, case.case_id, *case.failures], environment=ENV):
        run = agent.invoke(case.message, config={"callbacks": [CallbackHandler()]})
        langfuse.update_current_span(input=case.message, output=run.summary["final_answer"],
                                     metadata={"handoff_target": run.summary["handoff_target"], "case_id": case.case_id})
        return run, langfuse.get_current_trace_id(), langfuse.get_current_observation_id()


def _prompt_version(template):
    """Module 04 TODO 2: the version of PROMPT whose text + config equal `template` + PROMPT_CONFIG (created if missing)."""
    metas = [m for m in langfuse.api.prompts.list(name=PROMPT, limit=100).data if m.name == PROMPT]
    for v in sorted({v for m in metas for v in (m.versions or [])}):
        row = langfuse.api.prompts.get(PROMPT, version=v)
        if row.prompt == template and all((row.config or {}).get(k) == val for k, val in PROMPT_CONFIG.items()):
            return v
    return langfuse.create_prompt(name=PROMPT, prompt=template, type="text", config=PROMPT_CONFIG,
                                  commit_message="module 09: get-or-create by content").version


def ensure_prompt():
    good, bad = _prompt_version(SYSTEM_PROMPT_TEMPLATE), _prompt_version(WRONG_HANDOFF_TEMPLATE)
    for label, version in (("production", good), ("staging", bad)):
        try:
            langfuse.api.prompts.get(PROMPT, label=label)
        except NotFoundError:
            langfuse.update_prompt(name=PROMPT, version=version, new_labels=[label])
    labels = {label: langfuse.api.prompts.get(PROMPT, label=label).version for label in ("production", "staging")}
    if labels != {"production": good, "staging": bad}:
        print(f"WARNING: labels {labels}, but the good version is v{good} and the bad one v{bad}. Module 04 should "
              "end with production = good, staging = bad; the planted check expects that.")
    return labels


def ensure_config():
    """Module 06's `handoff_correct` config: get-or-create by name, skipping archived configs."""
    for c in langfuse.api.score_configs.get(page=1, limit=100).data:
        if c.name == CONFIG_NAME and not c.is_archived:
            return c.dict()
    return langfuse.api.score_configs.create(name=CONFIG_NAME, data_type="BOOLEAN",
                                             description="agent handed off to the expected team (code score)").dict()


def label_trace(case, run, trace_id):
    """Module 06 TODO 3/4: the code score (with the expected team) and, when wrong, the CORRECTION."""
    actual = run.summary["handoff_target"]
    ok = actual == case.expected_handoff
    langfuse.create_score(name=CONFIG_NAME, value=1.0 if ok else 0.0, data_type="BOOLEAN", trace_id=trace_id,
                          config_id=CONFIG["id"], comment=None if ok else f"expected {case.expected_handoff}, got {actual}",
                          metadata={"evaluator": "code:v1", "expected": case.expected_handoff, "actual": actual})
    if not ok:
        langfuse.create_score(name="output", value=case.reference_answer, data_type="CORRECTION", trace_id=trace_id)
    return ok


def wait_for_labels(rows, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        missing = [r for r in rows if not any(s.name == CONFIG_NAME for s in langfuse.api.scores.get_many(
            trace_id=r["trace_id"], limit=100).data)]
        if not missing:
            return print(f"all {len(rows)} source trace(s) labelled")
        time.sleep(4)
    raise TimeoutError(f"{len(missing)} trace(s) without a {CONFIG_NAME} score")


LABELS = ensure_prompt()
CONFIG = ensure_config()
clean_agent = SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)
planted_agent = SupportAgent(failures=FailureConfig.from_names(["wrong_handoff"]),  # bad prompt: login -> billing
                             guardrail_fn=observed_guardrail, search_fn=observed_search)
print(f"prompt {PROMPT}: {LABELS} | config {CONFIG_NAME}: {CONFIG['id']}")

# %% [markdown]
# ### The production traffic the dataset is built from
# Module 06's four requests (about 12 Azure calls). The login one is served by the bad prompt, like the incident in
# module 04, so its trace is labelled a failure, with a correction.

# %%
ROWS = []
for case in SOURCE:
    run, trace_id, root_id = traced_turn(case, planted_agent if case.failures else clean_agent)
    ok = label_trace(case, run, trace_id)
    ROWS.append({"case": case, "trace_id": trace_id, "root_id": root_id})
    print(f"  {case.case_id:<36} expected={case.expected_handoff:<19} actual={run.summary['handoff_target']:<19} "
          f"{'ok' if ok else 'FAILURE'}")
langfuse.flush()
wait_for_labels(ROWS)

# %% [markdown]
# ## TODO 2: build the regression dataset from the labelled traces
#
# Write `build_dataset(rows) -> dict[str, dict]` (item id → the item as a dict):
# 1. `langfuse.create_dataset(name=DATASET, description=…, metadata={…})`. It is an upsert by name, so calling it
#    on every run is safe.
# 2. For each row (one labelled trace):
#    - read the root observation's input: `langfuse.api.observations.get_many(trace_id=…, fields="core,io",
#      limit=50).data`, the one whose `.id == row["root_id"]`;
#    - read the trace's labels: `[s.dict() for s in langfuse.api.scores.get_many(trace_id=…, fields="details",
#      limit=100).data]`;
#    - `item = experiments.item_from_trace(root.input, scores, case_id=row["case"].case_id,
#      label_score=CONFIG_NAME)`; skip `None`;
#    - the id is `regression.dataset_item_id(DATASET, item["input"]["message"])`: the same request always maps to the
#      same item, so a re-run (or the same failure seen twice) updates it instead of adding a duplicate test;
#    - write it only when `experiments.changed(existing, item)`, where `existing` is
#      `langfuse.api.dataset_items.get(item_id).dict()` or `None` on `NotFoundError`:
#      `langfuse.create_dataset_item(dataset_name=DATASET, id=…, source_trace_id=…, source_observation_id=…, **item)`.
# 3. For each `regression.CURATED` case: `experiments.curated_item(c.case_id, c.message, c.expected_handoff,
#    c.reference_answer)`, created **only when the id doesn't exist yet**. Never overwrite a curated item: a reviewer
#    may have fixed it since (TODO 3 does exactly that). Check with `dataset_items.get(id)`, not with `list()`:
#    the list hides archived items, so you'd resurrect the one TODO 3 retires.
# 4. Return every ACTIVE item: `langfuse.api.dataset_items.list(dataset_name=DATASET, page=…, limit=100)`, paging
#    until `meta.total_pages`, as `{i.id: i.dict()}`.
#
# The cell runs it twice: the second call must write nothing (same item versions).

# %%
# TODO 2: your code here

# %%
ITEMS = build_dataset(ROWS)
again = build_dataset(ROWS)
assert {k: v["version"] for k, v in again.items()} == {k: v["version"] for k, v in ITEMS.items()}, \
    "the second build wrote new item versions: compare with experiments.changed() before writing"
DATASET_ID = langfuse.api.datasets.get(DATASET).id
for i in ITEMS.values():
    print(f"  {i['metadata'].get('case_id', '?'):<36} {i['metadata'].get('label', ''):<18} "
          f"expect {i['expectedOutput']['handoff']:<19} from trace {i.get('sourceTraceId') or '-'}")
print(f"{len(ITEMS)} active item(s): {UI}/project/{PROJECT_ID}/datasets/{DATASET_ID}")

# %% [markdown]
# ## TODO 3: version the dataset (fix a label, retire an item, pin the version)
#
# A reviewer reads the curated `invoice_request` item: it expects **no handoff**, but the routing policy says
# "invoice → billing_specialist". The label is wrong (it came from an old `cases.json`). And `prompt_injection` is a
# guardrail test that belongs in its own dataset.
#
# Write `revise_item(item_id, *, expected_handoff=None, status=None, note="") -> dict`:
# - read the current item (`langfuse.api.dataset_items.get(item_id).dict()`, camelCase keys);
# - build the wanted item: same `input`; `expected_output` = the current one with `handoff` replaced when given;
#   `metadata` = the current one plus `"revision_note": note`; `status` = the given one or the current one;
# - if `experiments.changed(current, wanted)`, upsert it with `langfuse.create_dataset_item(dataset_name=DATASET,
#   id=item_id, source_trace_id=…, source_observation_id=…, **wanted)` (keep the source links), and return the
#   **returned** item as a dict (its `version` is the server's timestamp of this write).
#
# Then pin: `DATASET_VERSION` = the **newest** `version` the server gave you, including the archive write. An
# experiment on `langfuse.get_dataset(DATASET, version=DATASET_VERSION)` then sees exactly today's test set, even if
# someone edits the dataset while it runs. Pin with a server timestamp: `datetime.now()` on this machine is a few
# seconds ahead of the server here, and a pin taken locally can land *before* your own last write.

# %%
FIX_CASE, FIX_TEAM = regression.LABEL_FIX
FIX_ID = regression.dataset_item_id(DATASET, regression.curated(FIX_CASE).message)
RETIRED_ID = regression.dataset_item_id(DATASET, regression.curated(regression.RETIRED).message)

# TODO 3: your code here (revise_item, then the two revisions, then DATASET_VERSION)

# %%
print("pinned dataset version:", DATASET_VERSION)
fixed = langfuse.api.dataset_items.get(FIX_ID)
before = {i.id: i for i in langfuse.api.dataset_items.list(dataset_name=DATASET, limit=100,
                                                           version=fixed.version - timedelta(milliseconds=1)).data}
print(f"{FIX_CASE}: now expects {fixed.expected_output['handoff']!r} (version {fixed.version}); "
      f"the version before expected {before[FIX_ID].expected_output['handoff']!r}")
print(f"{regression.RETIRED}: {langfuse.api.dataset_items.get(RETIRED_ID).status}")
PINNED = langfuse.get_dataset(DATASET, version=DATASET_VERSION)
assert FIX_ID in {i.id for i in PINNED.items} and RETIRED_ID not in {i.id for i in PINNED.items}, \
    "the pinned version must contain the fix and not the retired item: pin to the newest server timestamp"
print(f"{len(PINNED.items)} item(s) in the pinned dataset")

# %% [markdown]
# ## TODO 4: the evaluators
#
# An **item evaluator** is a function `(*, input, output, expected_output, metadata, **kwargs) -> Evaluation`. The SDK
# calls it once per item and stores each returned `Evaluation` as a score on that item's trace. Write:
# - `handoff_correct(...)`: `output["handoff"] == expected_output["handoff"]` →
#   `Evaluation(name=CONFIG_NAME, value=<bool>, data_type="BOOLEAN", config_id=CONFIG["id"], comment=…)`.
#   Same name and config as module 06's code score, so production and experiment scores mean the same thing (if
#   you did module 06, `scoring.handoff_correct` is this comparison). Verified live: a score sent with a
#   `config_id` is stored under the **config's** name, whatever `name` says, so use the config's name.
# - `concise(...)`: `experiments.concise(output["answer"], MAX_SENTENCES)` → a BOOLEAN `"concise"` evaluation.
#
# A **composite evaluator** gets the same arguments plus `evaluations` (this item's Evaluation list):
# - `quality(*, input, output, expected_output, metadata, evaluations, **kwargs)` →
#   `Evaluation(name="quality", value=experiments.quality(evaluations, WEIGHTS), data_type="NUMERIC")`.
#
# A **run evaluator** gets `item_results` (each with `.evaluations`) and scores the whole experiment:
# - `handoff_accuracy(*, item_results, **kwargs)`: the mean of the items' `CONFIG_NAME` values (as 1/0), with a
#   comment like `"5/6 items routed correctly"`.
# - `quality_avg(*, item_results, **kwargs)`: the mean `quality`.
#
# Return `Evaluation` objects, not dicts. The cell tests them on a fake item before any Azure call.

# %%
# TODO 4: your code here

# %%
fake = dict(input={"message": "x"}, output={"handoff": "billing_specialist", "answer": "Done. Billing will call."},
            expected_output={"handoff": "billing_specialist"}, metadata={})
evals = [handoff_correct(**fake), concise(**fake)]
print([(e.name, e.value) for e in evals], "->", quality(**fake, evaluations=evals).value)
assert evals[0].value is True and evals[0].config_id == CONFIG["id"] and quality(**fake, evaluations=evals).value == 1.0
from types import SimpleNamespace
print(handoff_accuracy(item_results=[SimpleNamespace(evaluations=evals),
                                     SimpleNamespace(evaluations=[Evaluation(name=CONFIG_NAME, value=False)])]))

# %% [markdown]
# ## Given: the task (module 04's prompt serving, inside an experiment)
# `make_task(label)` serves the prompt **once** per experiment (`get_prompt(label=…)`), so every item of a run sees
# the same version, and returns a task function `task(*, item) -> {"handoff", "answer"}`. Inside the task:
# `propagate_attributes(prompt=prompt)` links every generation to that version (module 04), and the agent gets the
# compiled text and the version's model parameters for that turn. The SDK wraps each call in an `experiment-item-run`
# trace (environment `sdk-experiment`) linked to the dataset item. A sync task runs items one at a time, so one agent
# object is safe.

# %%
EXPERIMENT_AGENT = SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)


def serve_prompt(label):
    prompt = langfuse.get_prompt(PROMPT, label=label, cache_ttl_seconds=0)
    text = prompt.compile(**PROMPT_VARIABLES)
    assert "{{" not in text, f"{PROMPT}@{label} has variables we don't supply"
    params = {k: prompt.config[k] for k in ("temperature", "max_tokens") if k in (prompt.config or {})}
    return prompt, text, params


def make_task(label):
    prompt, text, params = serve_prompt(label)

    def task(*, item, **kwargs):
        with propagate_attributes(prompt=prompt):
            run = EXPERIMENT_AGENT.invoke(item.input["message"], config={"callbacks": [CallbackHandler()]},
                                          system_prompt=text, llm_params=params)
        return {"handoff": run.summary["handoff_target"], "answer": run.summary["final_answer"]}

    return prompt, task


def wait_for_items(result, n, timeout=180):
    """Poll the experiments API until `n` items of this run are ingested, each with a handoff_correct score."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = langfuse.api.experiments.list_items(from_start_time=RUN_STARTED - timedelta(minutes=10),
                                                   experiment_id=result.experiment_id, fields="core,scores",
                                                   limit=100).data
        if len(rows) >= n and all(any(s.name == CONFIG_NAME for s in (r.scores or [])) for r in rows):
            return print(f"{result.run_name}: {len(rows)} item(s) ingested and scored")
        time.sleep(4)
    raise TimeoutError(f"{result.run_name}: items not ingested/scored within {timeout}s")

# %% [markdown]
# ## TODO 5: run one prompt variant as an experiment
#
# Write `run_variant(label, *, trigger="notebook", limit=None) -> ExperimentResult`:
# - `prompt, task = make_task(label)`;
# - `dataset = langfuse.get_dataset(DATASET, version=DATASET_VERSION)` (the pinned items; `dataset.run_experiment`
#   records that version on every item). For `limit`, slice it: `dataset.items = dataset.items[:limit]`;
# - `dataset.run_experiment(name=EXPERIMENT, run_name=…, description=…, task=task, evaluators=[handoff_correct,
#   concise], composite_evaluator=quality, run_evaluators=[handoff_accuracy, quality_avg], max_concurrency=1,
#   metadata={…})`.
#
# Rules:
# - **`run_name` must be unique per run**: the experiment id is derived from (dataset, run_name), so the same name
#   appends to the old experiment. Use e.g. `f"{label} v{prompt.version} · {RUN_ID}"`, and prefix remote runs with
#   `"remote "`.
# - `metadata` (string values only) must carry `run_tag=RUN_TAG`, `prompt_label=label`,
#   `prompt_version=str(prompt.version)` and `trigger=trigger`. It is how you (and verify) find a run later.
#
# The cell runs production (v1), then staging (v2): about 36 Azure calls.

# %%
# TODO 5: your code here

# %%
BASE = run_variant("production")
CAND = run_variant("staging")
for res in (BASE, CAND):
    wait_for_items(res, len(PINNED.items))
    print(res.format())

# %% [markdown]
# ## TODO 6: compare the runs (through the API) and gate the candidate
#
# The `ExperimentResult` objects in memory vanish with the kernel; a CI job or a teammate reads the stored runs.
# Write:
# - `verdicts(result) -> dict[str, bool | None]`: dataset item id → that item's `CONFIG_NAME` score value, read with
#   `langfuse.api.experiments.list_items(from_start_time=RUN_STARTED - timedelta(minutes=10),
#   experiment_id=result.experiment_id, fields="core,io,scores", limit=100)`. Page with `cursor=resp.meta.cursor`
#   until it is None. The item id is `row.experiment_item_id`; the scores are `row.scores` (`.name`, `.value`; these
#   item scores come without `configId`/`comment`: `api.scores.get_many(trace_id=row.trace_id)` has those).
# - `compare(base, cand) -> dict`: `experiments.compare_runs(verdicts(base), verdicts(cand))`, then print one line
#   per **regressed** item: its case id (`row.experiment_item_metadata`), the message, the expected team, and what
#   each version did (`row.output` is a JSON string). Return the comparison.
#
# Then `COMPARISON = compare(BASE, CAND)` and `DECISION = experiments.gate(COMPARISON)`.

# %%
# TODO 6: your code here

# %%
COMPARISON = compare(BASE, CAND)
DECISION = experiments.gate(COMPARISON)
print(f"production v{LABELS['production']} {COMPARISON['baseline_rate']:.2f} -> staging v{LABELS['staging']} "
      f"{COMPARISON['candidate_rate']:.2f} | regressed {len(COMPARISON['regressed'])}, improved "
      f"{len(COMPARISON['improved'])} | gate: {DECISION}")
print("side by side in the UI:", BASE.experiment_url)

# %% [markdown]
# ## TODO 7: the remote trigger receiver
#
# In the UI, a dataset can hold a **remote experiment URL** (Datasets → the dataset → *Run experiment* → *Remote
# experiment*: URL, default payload, signing). Clicking *Run* makes Langfuse POST to that URL:
# `{"projectId", "datasetId", "datasetName", "payload"}` (`payload` = the configured default payload, **a string**,
# e.g. `'{"label": "staging", "limit": 2}'`), with `x-langfuse-signature: t=<unix>,v1=<HMAC-SHA256>` when signing
# is on. Your service runs the experiment; Langfuse only shows the result.
#
# Write `handle_remote_trigger(body: bytes, headers: dict) -> tuple[int, dict]` (status code, JSON reply). The
# headers dict has lower-case names:
# - `experiments.verify_signature(body, headers.get(regression.SIGNATURE_HEADER), REMOTE_SECRET, now=time.time())`,
#   else `(401, {...})`. Never run anything for an unsigned request: the URL is reachable by whoever finds it.
# - `experiments.trigger_request(body)` (a `ValueError` → `400`); a `dataset_name` other than `DATASET` → `404`.
# - from the payload: `label` (default `"staging"`, must be `"production"` or `"staging"`, else 400) and `limit`
#   (default 2).
# - Langfuse waits **20 s** at most, and an experiment takes longer: start it in the background with
#   `start_background(run_variant, label, trigger="remote", limit=limit)` and answer `(202, {...})` right away.
#
# The given cell runs a local server with your handler and POSTs to it **exactly** what Langfuse would send (body and
# signature built the same way, `langfuse_lab/regression.py`): first a tampered request (must be refused), then a
# signed one (2 items on staging, about 6 Azure calls).

# %%
REMOTE_SECRET = "lf-whsec_" + secrets.token_hex(16)  # in the UI: shown once when you enable signing
REMOTE_RUNS = []


def start_background(fn, *args, **kwargs):
    def work():
        REMOTE_RUNS.append(fn(*args, **kwargs))
    t = threading.Thread(target=work, daemon=True)
    t.start()
    REMOTE_THREADS.append(t)
    return t


REMOTE_THREADS = []

# TODO 7: your code here

# %%
import httpx
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class TriggerEndpoint(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        status, reply = handle_remote_trigger(body, {k.lower(): v for k, v in self.headers.items()})
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), TriggerEndpoint)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_address[1]}/langfuse/remote-experiment"


def simulate_langfuse_trigger(payload, *, secret=REMOTE_SECRET):
    """What Langfuse's web container does when you click Run (verified in the server code, v4.77)."""
    body = regression.trigger_body(PROJECT_ID, DATASET_ID, DATASET, payload)
    headers = {"content-type": "application/json", "user-agent": "Langfuse/1.0",
               regression.SIGNATURE_HEADER: regression.sign(body, secret, int(time.time()))}
    r = httpx.post(URL, content=body.encode(), headers=headers, timeout=20)
    return r.status_code, r.json()


print("tampered:", simulate_langfuse_trigger('{"label": "staging"}', secret="lf-whsec_wrong"))
print("signed:  ", simulate_langfuse_trigger('{"label": "staging", "limit": 2}'))
for t in REMOTE_THREADS:
    t.join(timeout=300)
server.shutdown()
assert REMOTE_RUNS, "the signed request started no experiment"
wait_for_items(REMOTE_RUNS[-1], 2)
print("remote run:", REMOTE_RUNS[-1].run_name, REMOTE_RUNS[-1].experiment_url)

# %% [markdown]
# ### In the real UI (optional)
# Datasets → `support-regression` → *Run experiment* → *Remote experiment*: URL, default payload
# `{"label": "staging", "limit": 2}`, signing on (copy the secret into `REMOTE_SECRET`). On **this** deployment the
# button cannot reach the notebook: Langfuse only calls http(s) URLs on **port 80 or 443** that pass its SSRF check
# (private/loopback hosts need `LANGFUSE_WEBHOOK_WHITELISTED_HOST` / `_IPS` on the web container). In production the
# URL is a deployed service (or a CI workflow dispatch). Verify reports this part ⚠️: the remote-run config is not
# readable through the public API.
#
# ## Optional: a UI prompt experiment (needs module 07's LLM connection)
# Langfuse can run a **prompt** over the dataset by itself (worker + LLM connection, no code, no tools). It compiles
# the prompt with the item's **input keys as variables**. Our `support-agent-system` is a *text* system prompt; Langfuse
# would send it alone as a system message, without the customer's request. A **chat** prompt with a `{{message}}`
# user turn works. Set `CREATE_CHAT_PROMPT = True` to create it (get-or-create), then: Datasets → `support-regression`
# → *Run experiment* → *Prompt experiment* → prompt `support-agent-chat` (production), your module 07 LLM connection
# and model. The answers are text only (no tool calls), so read them; add module 07's judge as an evaluator if you
# like. Its traces land in environment `langfuse-prompt-experiment`; verify notices them.

# %%
CREATE_CHAT_PROMPT = False
if CREATE_CHAT_PROMPT:
    try:
        chat = langfuse.api.prompts.get(NAMES["chat_prompt"], label="production")
        print("exists:", chat.name, "v", chat.version)
    except NotFoundError:
        chat = langfuse.create_prompt(name=NAMES["chat_prompt"], type="chat", labels=["production"],
                                      prompt=[{"role": "system", "content": SYSTEM_PROMPT},
                                              {"role": "user", "content": "{{message}}"}],
                                      commit_message="module 09: UI prompt experiment over support-regression")
        print("created:", chat.name, "v", chat.version)

# %% [markdown]
# Verify waits for the 4 source traces, then checks:
# - the dataset: at least 6 active labelled items, one per request, the 4 trace items linked to their traces with
#   the label (`confirmed_failure` / `golden`) and the correction;
# - versioning: the invoice item fixed (with its old label still in the previous version), the injection item archived;
# - the two variants: pinned to one dataset version, every active item exactly once, `prompt_version` matching the
#   labels, every generation linked to its version;
# - every item scored by `handoff_correct` (config id; value recomputed from output vs expected), `concise` and the
#   composite `quality`; the run scores `handoff_accuracy` (recomputed) and `quality_avg`;
# - your `COMPARISON` against the stored item scores, the **planted** login regression (a login item that passes
#   on production and fails on staging) with `DECISION = block`;
# - the remote-triggered run; the UI-side remote config (⚠️) and the optional UI prompt experiment.

# %%
verify.check_module_09(RUN_TAG, prefix=PREFIX, comparison=COMPARISON, decision=DECISION, expected_traces=len(SOURCE))

# %% [markdown]
# ## UI tour
# - [ ] **Datasets** → `support-regression`: the items, each with *input / expected output / metadata*; the
#   trace-sourced ones link back to their production trace (*Source trace*). Open the planted login item: the trace it
#   came from shows the failure and the correction.
# - [ ] An item's **version history**: the invoice item shows the old `none` label and your fix. The archived
#   `prompt_injection` item is hidden by default (status filter).
# - [ ] The dataset's **Experiments** tab: `production v… · m09-…` and `staging v… · m09-…` with item counts and the
#   aggregated scores (`handoff_correct`, `concise`, `quality`) plus the run scores (`handoff_accuracy`, `quality_avg`).
# - [ ] Select both runs → **Compare**: the same items side by side. Find the login rows: v1 → `account_security`,
#   v2 → billing (or nobody). Click into an item's trace: `experiment-item-run` → task → the agent's spans, then
#   `experiment-item-evaluation` with one `EVALUATOR` observation per evaluator.
# - [ ] A generation in a v2 trace: *Prompt* shows `support-agent-system` v2: the link that proves which version
#   produced the bad routing.
# - [ ] Traces, environment filter: experiment traces are in `sdk-experiment`, separate from `dev` production traffic.
# - [ ] *Run experiment* on the dataset: the **Prompt experiment** form (prompt, LLM connection, evaluators) and the
#   **Remote experiment** settings (URL, default payload, signing).
#
# ## Production notes
# - **Every confirmed failure becomes a test.** Build dataset items from labelled traces (code scores, corrections,
#   human verdicts) with deterministic ids, so the same request is one test no matter how often it fails.
# - **Datasets are versioned, so fix labels in place.** A wrong expected output is a bug in the test; fix it with an
#   upsert (the old version stays readable), archive what no longer belongs, and **pin** the version a comparison
#   runs on.
# - **Compare candidates on the same items, by label.** Run `production` and the candidate on the pinned dataset,
#   compare item by item, and gate on regressions, not only on the average (an average can rise while a critical
#   intent breaks).
# - **Evaluators are shared definitions.** The experiment's `handoff_correct` uses the same name and score config
#   as production's code score, so a 0.67 in an experiment and a 0.67 in production mean the same thing.
# - **Unique run names.** The run name is the experiment's identity on a dataset; re-using one appends. Put the
#   prompt version and a run id (or a git SHA in CI, module 10) in it, and the rest in `metadata`.
# - **Remote triggers are webhooks.** Verify the signature (and the timestamp) before doing anything, answer within
#   20 s and run the experiment in the background; on self-hosted Langfuse, allow the target host explicitly.
# - **Experiment traffic is real traffic.** It costs LLM calls and lands in its own environment (`sdk-experiment`):
#   keep it out of production dashboards and live judges (module 07 rules filter by environment and tags).
# - **Why it matters (leadership):** a prompt change ships only after it beats production on the failures we have
#   already seen, so the same incident cannot happen twice, and the decision is a link anyone can open.
