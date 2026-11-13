# %% [markdown]
# # Module 10: CI/CD gate (a pytest release gate on Langfuse experiments, GitHub Actions, flaky LLMs)
#
# **Goal:** module 09 compared two prompt labels once, in a notebook. A release process needs that comparison to run
# **on every pull request, by itself**, and to **fail the PR** when the candidate breaks something production gets
# right. You build that gate: a pytest suite that runs the candidate as a Langfuse experiment on the pinned regression
# dataset, compares it with the production baseline item by item, re-runs only the suspicious items (LLMs are
# nondeterministic), writes the decision **onto the experiment**, emits JUnit + Markdown reports and exits non-zero.
#
# | Concept | What it is | In our gate |
# |---|---|---|
# | **Release gate** | an automated check that decides ship / block for a candidate | `pytest langfuse_mastery/ci` with `CANDIDATE_LABEL=…` |
# | **Candidate vs baseline** | the version under review vs what users get today, **by label** | `staging` / `candidate` vs `production` |
# | **Pinned dataset version** | every run of a comparison sees the same items | `GATE_DATASET_VERSION` (a repo variable in CI) |
# | **CI context** | who/what is being gated: commit, branch, PR, CI run | experiment metadata `git_sha`, `branch`, `pr`, `ci_run`, … |
# | **Run-level score** | a score on the **experiment** (not on an item) | `ci_gate` = `ship`/`block`/`error` with the reasons |
# | **Suspect / confirmed / flaky** | regressed once / regressed on the majority of attempts / didn't | re-run only suspects, on both sides |
# | **Cached baseline** | reuse a production run whose inputs are identical | `h.baseline_key`: dataset version, prompt version, model, evaluator |
# | **Exit code** | what the CI runner reads | `ship` 0, `block` 1, `error` 2 |
# | **Artifacts** | what a reviewer reads when the check is red | `gate-summary.md` (job summary), `gate-junit.xml`, `gate-report.json` |
#
# ```
#  PR opened ──► GitHub Actions (release-gate.yml) ──► pytest langfuse_mastery/ci   CANDIDATE_LABEL=staging
#                                                          │
#      baseline_for(): cached production run? ──no──► run_variant("production")  ┐ same pinned
#      run_variant(candidate)  ────────────────────────────────────────────────── ┘ dataset version
#      verdicts() ─► experiments.compare_runs ─► suspects ─► re-run suspects on BOTH sides
#      ci_gate.confirm (majority vote) ─► ci_gate.decide ─► record_gate(): ci_gate score on the experiment
#      artifacts (Markdown, JUnit, JSON) ─► exit 0 ship / 1 block / 2 error ─► the PR check is green / red
# ```
#
# **The planted failure:** `staging` is module 04's bad candidate (login → billing). The gate must **block** it,
# exit 1 and name the regressed login items. A clean `candidate` version (production's text with a clarified login
# rule) must **ship**, exit 0.
#
# **False block vs false pass.** An LLM at temperature 0 is still not deterministic (module 09: production itself
# missed items it usually gets right), and a GPT-5-family deployment can't even be set to 0: it runs at its default
# temperature, so expect more suspects. A gate that blocks on any single difference blocks good PRs (**false block**:
# people learn to click "re-run" or to bypass it). A gate that averages everything lets a broken intent through
# (**false pass**: the incident ships). This gate's answer: decide on **items**, re-run only the **suspects** on
# **both** sides, and call an item regressed only when a **majority** of attempts says so. You'll tune the knobs.
#
# Things verified live on this server (4.77 / SDK 5.2) that shape the TODOs:
# - `create_score(dataset_run_id=<experiment id>, …)` writes a **run-level** score (what `run_evaluators` do);
#   CATEGORICAL works without a config. `experiments.list(fields="scores")` returns it **without** comment and
#   metadata; `scores.get_many(experiment_id=…, fields="details,subject")` has both.
# - `run_experiment(metadata=…)` values are **stringified** and copied onto every item.
# - `experiments.list` filters by id / name / dataset only: finding "the cached baseline" means filtering metadata
#   yourself.
# - The same run name on the same dataset is the same experiment (module 09): a re-run appends. CI run names carry the
#   commit and the CI run id **and attempt**.
#
# Docs: https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk ·
# https://langfuse.com/docs/evaluation/experiments/datasets · https://langfuse.com/docs/evaluation/evaluation-methods/custom-scores ·
# https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import importlib
import json
import os
import subprocess
import uuid
import xml.etree.ElementTree as ET
from langfuse_lab import verify
from langfuse_lab.ci_workflow import workflow_problems
from langfuse_lab.config import Settings
from langfuse_lab.prompts import CANDIDATE_LABEL
from langfuse_mastery import ci_gate, experiments

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
RUN_TAG = f"run:m10-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
CI_DIR = LAB_ROOT / "langfuse_mastery" / "ci"
ARTIFACTS = CI_DIR / "artifacts" / RUN_ID  # git-ignored

# Prompts, datasets and score configs are PROJECT-GLOBAL (module 09's dataset can't be deleted). Keep PREFIX empty.
PREFIX = ""


def _git(*args):
    out = subprocess.run(["git", *args], cwd=LAB_ROOT, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


GIT_SHA, BRANCH = _git("rev-parse", "HEAD"), _git("rev-parse", "--abbrev-ref", "HEAD")
print("run tag:", RUN_TAG, "| commit", (GIT_SHA or "?")[:7], "on", BRANCH, "| candidate label:", CANDIDATE_LABEL)

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/ci_gate.py` (pure functions + pytest)
#
# The gate's decisions are plain functions, so they're tested here and behave the same on a GitHub runner. Open
# `ch09/langfuse_mastery/ci_gate.py` and implement:
# - `ci_context(env, …)`: commit / branch / PR / CI run from GitHub Actions variables (with local fallbacks), and
#   `run_name(…)`, `experiment_metadata(…)`: unique run names and string-only metadata.
# - `pass_rates(attempts)` and `confirm(comparison, baseline_attempts, candidate_attempts)`: majority vote over
#   re-runs, which separates confirmed regressions from flaky ones.
# - `decide(confirmed, …)`: `ship` / `block` / `error` + exit code. **Reuse** module 09's `experiments.gate`.
# - `report_rows`, `junit_xml`, `markdown_summary`: the artifacts a reviewer reads.
#
# **Prerequisite:** module 09's TODO 1 (`experiments.compare_runs` / `gate`). The cell checks it first. Re-run it
# until it shows `26 passed`.

# %%
dep = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_experiments.py", "-q",
                      "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
if dep.returncode != 0:
    print("module 09 TODO 1 (experiments.py) is not done yet: this gate reuses compare_runs() and gate().\n",
          dep.stdout[-800:])
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_ci_gate.py", "-q",
                         "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
experiments = importlib.reload(experiments)
ci_gate = importlib.reload(ci_gate)

# %% [markdown]
# ## Given: bootstrap, the harness, a dry run
# This is setup, not a TODO:
# - `harness.bootstrap(langfuse, prefix=PREFIX)` get-or-creates what the gate reads: the managed prompt with
#   `production` (good), `staging` (module 04's bad candidate) and **`candidate`** (a clean version, new in this
#   module), each by content and a label only set when it's missing; module 06's `handoff_correct` config; and the
#   regression dataset, **seeded only when it doesn't exist** (same items and ids as module 09 writes). CI never calls
#   it: in CI the gate only reads, and fails loudly when a label or the dataset is missing.
# - `harness.Harness.from_env(env)` (read it: `ch09/langfuse_mastery/ci/harness.py`) is what the gate runs against:
#   the client, names, labels, the dataset **pinned** to `GATE_DATASET_VERSION`, the **policy** (re-runs, tolerances,
#   budget), `task(label)` (the module 09 task: serve the prompt once, link generations), the evaluators
#   (`handoff_correct` with the config) and `wait_for_items`.
# - **Dry run** (`GATE_DRY_RUN=1`): an oracle task, no Azure calls. It answers the expected team, except `staging`
#   sends login cases to billing like the bad prompt. Iterate on TODOs 2–4 with it for free; dry experiments carry
#   `dry_run=true` and a different baseline key, so they never count as (or stand in for) real runs.
# - `gate_env(label, …)`: the environment one gate run gets (what the workflow sets in CI).

# %%
from langfuse import get_client
from langfuse_mastery.ci import harness

langfuse = get_client()
BOOT = harness.bootstrap(langfuse, prefix=PREFIX)
PIN = BOOT["dataset_version"].isoformat()  # in CI: the repository variable GATE_DATASET_VERSION
for w in BOOT["warnings"]:
    print("WARNING:", w, "(module 04 should end with production = good, staging = bad)")
print(f"prompt {BOOT['names']['prompt']}: {BOOT['labels']} | dataset {BOOT['names']['dataset']}: {BOOT['items']} "
      f"active item(s){' (seeded now)' if BOOT['seeded'] else ''}, pinned at {PIN}")


def gate_env(label, *, dry=False, run=None):
    """The environment of one gate run. GATE_RUN_ID must be unique per run: it is part of every run name."""
    return {**os.environ, "CANDIDATE_LABEL": label, "GATE_PREFIX": PREFIX, "GATE_RUN_TAG": RUN_TAG,
            "GATE_RUN_ID": f"{RUN_ID}-{run or label}-{uuid.uuid4().hex[:4]}", "GATE_DATASET_VERSION": PIN,
            "GATE_ARTIFACTS": str(ARTIFACTS / (run or label)), "GATE_DRY_RUN": "1" if dry else ""}


def dry(label, run):
    env = gate_env(label, dry=True, run=run)
    return harness.Harness.from_env(env, langfuse=langfuse), ci_gate.ci_context(env, local_sha=GIT_SHA,
                                                                                local_branch=BRANCH)


DRY_H, DRY_CTX = dry("staging", "dry-a")
INFO = DRY_H.info
LOGIN_IDS = [i for i, x in INFO.items() if x["expected"] == verify.LOGIN_TEAM]
print("policy:", DRY_H.policy)
print("baseline key:", DRY_H.baseline_key)
for i, x in INFO.items():
    print(f"  {x['case_id']:<32} expect {x['expected']:<19} {i}")

# %% [markdown]
# ## TODO 2: run a variant with CI metadata, read its verdicts back
#
# In `ch09/langfuse_mastery/ci/release_gate.py`, implement (the docstrings have the details):
# - `run_variant(h, ctx, label, *, role, item_ids=None, attempt=0, extra=None)`: the label's prompt on the **pinned**
#   dataset (`h.pinned()`, optionally only `item_ids`), with `ci_gate.run_name(…)` and
#   `ci_gate.experiment_metadata(ctx, …, **h.metadata(), **extra)`. Module 09's `run_variant`, plus CI context.
# - `verdicts(h, experiment_id)`: `{item id: True/False/None}` from `api.experiments.list_items` (cursor paging).
#   CI never trusts the in-memory result: it reads what Langfuse stored, like a teammate would.
#
# The cell runs a **dry** smoke test: staging on the two login items plus one other (no Azure calls).

# %%
from langfuse_mastery.ci import release_gate
release_gate = importlib.reload(release_gate)

smoke_ids = LOGIN_IDS + [next(i for i in INFO if i not in LOGIN_IDS)]
res = release_gate.run_variant(DRY_H, DRY_CTX, "staging", role="smoke", item_ids=smoke_ids)
DRY_H.wait_for_items(res.experiment_id, len(smoke_ids))
v = release_gate.verdicts(DRY_H, res.experiment_id)
print(res.run_name, "->", {INFO[i]["case_id"]: ok for i, ok in v.items()})
meta = langfuse.api.experiments.list(from_start_time=DRY_H.since, id=res.experiment_id, fields="core,metadata").data[0].metadata
print({k: meta.get(k) for k in ("git_sha", "branch", "ci_run", "role", "prompt_label", "prompt_version", "dataset_version", "dry_run")})
assert set(v) == set(smoke_ids) and all(v[i] is False for i in LOGIN_IDS), "dry staging must fail the login items"

# %% [markdown]
# ## TODO 3: the cached baseline
#
# Every PR needs a production baseline on the same items. Running it each time doubles the LLM bill, but reusing a
# **stale** one is worse: if the dataset, the production prompt, the model or the evaluator changed, the comparison
# means nothing. `h.baseline_key` encodes exactly those inputs.
#
# Implement `baseline_for(h, ctx) -> (experiment_id, reused)`: list the dataset's experiments of the last
# `BASELINE_MAX_AGE`, reuse one whose metadata says `role == "baseline"` and `baseline_key == h.baseline_key` and
# whose `item_count` equals the pinned item count (a crashed baseline is incomplete); otherwise run production
# (`run_variant(…, role="baseline")`) and wait for its items.
#
# The cell calls it twice (dry): the second call must reuse the first one's baseline.

# %%
release_gate = importlib.reload(release_gate)
b1 = release_gate.baseline_for(DRY_H, DRY_CTX)
b2 = release_gate.baseline_for(DRY_H, DRY_CTX)
print("first:", b1, " second:", b2)
assert b2 == (b1[0], True), "the second call must reuse the stored baseline"

# %% [markdown]
# ## TODO 4: the whole gate, and its verdict on the experiment
#
# Implement `evaluate_candidate(h, ctx)` and `record_gate(h, report)` (docstrings):
# baseline → candidate (its metadata points at the baseline) → `experiments.compare_runs` → re-run **only the
# suspects**, on **both** sides, `policy["reruns"]` times (not when there are more than `max_rerun_items`: a candidate
# that breaks half the dataset doesn't deserve more LLM calls) → `ci_gate.confirm` → `ci_gate.decide` → report →
# `record_gate`: a CATEGORICAL run-level score `ci_gate` on the **candidate experiment** with the reasons as comment
# and the CI context + regressed/flaky items in metadata. Anyone who opens the run in Langfuse sees whether it
# shipped, for which commit, and why.
#
# The cell runs the full gate **dry** for staging: it must block on the login items.

# %%
release_gate = importlib.reload(release_gate)
DRY_H, DRY_CTX = dry("staging", "dry-b")
report = release_gate.evaluate_candidate(DRY_H, DRY_CTX)
print(ci_gate.markdown_summary(report["decision"], report["rows"], title="dry gate: staging"))
import time
for _ in range(20):  # ingestion is async
    stored = [s for s in langfuse.api.scores.get_many(experiment_id=report["candidate_experiment_id"],
                                                      fields="details,subject", limit=50).data
              if s.name == harness.GATE_SCORE]
    if stored:
        break
    time.sleep(3)
print("stored on the experiment:", [(s.value, s.comment, s.subject.kind if s.subject else None) for s in stored])
assert report["decision"]["decision"] == "block" and set(report["confirmed"]["regressed"]) == set(LOGIN_IDS)

# %% [markdown]
# ### The policy: false block vs false pass
# `harness.DEFAULT_POLICY` (override per run with `GATE_RERUNS`, `GATE_MAX_REGRESSIONS`, `GATE_MAX_DROP`,
# `GATE_MAX_RERUN_ITEMS`, `GATE_MAX_ITEMS`):
#
# | knob | default | turn it up → | turn it down → |
# |---|---|---|---|
# | `reruns` | 1 | fewer false blocks (noise gets voted out), more LLM calls per suspect | cheaper; `0` = every suspect is a regression |
# | `max_regressions` | 0 | more false passes (a broken intent ships if it's "only one") | stricter |
# | `max_drop` | 0.2 | tolerates broad small decay | catches it, but noise on a 7-item set is ±0.14 per flip |
# | `max_rerun_items` | 3 | spends more on candidates that are plainly bad | blocks obviously bad candidates without re-runs |
# | `max_items` | 20 | bigger, slower, costlier gates | a per-PR budget: big suites run nightly instead |
#
# Before you run the real gate, predict: with `reruns=1`, what happens to an item production passes 2 of 2 times and
# the candidate passes 1 of 2? (Majority vote: candidate 0.5 is not < 0.5, so it is **flaky**, not a regression.)
# Which mistake is cheaper for *this* agent, a blocked good prompt or a shipped login regression?

# %% [markdown]
# ## TODO 5: the pytest test and the GitHub Actions workflow
#
# **5a.** `ch09/langfuse_mastery/ci/test_release_gate.py`: call `evaluate_candidate`, write `gate-summary.md`,
# `gate-junit.xml` and `gate-report.json` into the `artifacts` fixture's folder, `pytest.exit(…, returncode=2)` on
# `error`, and `assert decision == "ship"` with the Markdown as the message. The fixtures (`conftest.py`, given) build
# `harness`, `ci` and `artifacts` from the environment, and skip unless `CANDIDATE_LABEL` is set, so a plain `pytest`
# never spends LLM calls.
#
# **5b.** `ch09/langfuse_mastery/ci/release-gate.yml`: fill in the TODOs (triggers, least privilege, concurrency, a
# timeout, secrets, the PR's head SHA, the pinned dataset version, the pytest step, artifacts `if: always()`). It is
# committed but **not active**: GitHub only runs files under `.github/workflows/`, and a runner can't reach
# `localhost:3000`. The cell lints it.

# %%
problems = workflow_problems((CI_DIR / "release-gate.yml").read_text(encoding="utf-8"))
print("\n".join(f"- {p}" for p in problems) or "release-gate.yml: no problems found")
assert not problems, "fix the workflow (5b)"

# %% [markdown]
# ## Run the gate, like CI would
# Two runs of `pytest langfuse_mastery/ci` as separate processes, with the environment a workflow would set:
# 1. `CANDIDATE_LABEL=staging` must **fail** (exit 1) and name the regressed login items. It runs the production
#    baseline, the candidate and re-runs of the suspects: about 16 agent turns.
# 2. `CANDIDATE_LABEL=candidate` must **pass** (exit 0). It reuses the cached baseline: about 6 turns.
#
# About 50 Azure calls in total. Each run gets a unique `GATE_RUN_ID` (a re-run of this cell makes new experiments).

# %%
def run_gate(label):
    env = gate_env(label)
    folder = Path(env["GATE_ARTIFACTS"])
    folder.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/ci", "-q", "-p", "no:cacheprovider",
                        "--tb=no", "--color=no", f"--junitxml={folder / 'pytest.xml'}"],
                       cwd=LAB_ROOT, env=env, capture_output=True, text=True, timeout=1800)
    print(f"$ CANDIDATE_LABEL={label} pytest langfuse_mastery/ci  ->  exit {r.returncode}")
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-2000:])
    summary = folder / "gate-summary.md"
    print(summary.read_text(encoding="utf-8") if summary.exists() else f"(no {summary.name}: {r.stdout[-2000:]})")
    return r.returncode, folder


EXIT_STAGING, ART_STAGING = run_gate("staging")
EXIT_CLEAN, ART_CLEAN = run_gate(CANDIDATE_LABEL)
print("exit codes:", {"staging": EXIT_STAGING, CANDIDATE_LABEL: EXIT_CLEAN})

# %% [markdown]
# ### The artifacts a reviewer gets
# In CI these are the job summary (Markdown) and the uploaded artifact; test reporters render the JUnit file per item.

# %%
for label, folder in (("staging", ART_STAGING), (CANDIDATE_LABEL, ART_CLEAN)):
    junit = folder / "gate-junit.xml"
    if junit.exists():
        root = ET.fromstring(junit.read_text(encoding="utf-8").split("?>", 1)[-1])
        failed = [c.get("name") for c in root.findall("testcase") if c.find("failure") is not None]
        print(f"{label}: {root.get('tests')} testcases, {root.get('failures')} failure(s) {failed}, "
              f"{root.get('errors')} error(s)")
    rep = folder / "gate-report.json"
    if rep.exists():
        r = json.loads(rep.read_text(encoding="utf-8"))
        print(f"   baseline reused: {r['baseline_reused']}, re-runs: {len(r['rerun_experiment_ids'])}, "
              f"elapsed {r['elapsed_s']} s, candidate run: {r['links']['candidate run']}")
assert EXIT_STAGING == 1, "the planted staging regression must fail the gate (exit 1)"
assert EXIT_CLEAN == 0, "the clean candidate must pass the gate (exit 0): look at its summary above"

# %% [markdown]
# Verify reads only what Langfuse stored (this run tag's gate experiments; dry runs excluded):
# - both gate runs carry the CI metadata (commit, branch, PR, CI run, candidate label + its current version, trigger);
# - each candidate ran against a **production** baseline on the **same pinned** dataset version, every item once
#   (one baseline shared by both gates = the cache worked);
# - only the suspects were re-run, on both sides, as often as the recorded policy says;
# - the `ci_gate` score on each candidate experiment matches what the stored item scores give (majority vote + gate
#   rules with the recorded policy);
# - the **planted** staging regression is blocked (exit 1) with the regressed login items named;
# - the clean candidate shipped (exit 0) and is a different prompt version.

# %%
verify.check_module_10(RUN_TAG, prefix=PREFIX, clean_label=CANDIDATE_LABEL)

# %% [markdown]
# ## UI tour
# - [ ] **Datasets** → `support-regression` → *Experiments*: the `ci …` runs of this module: `baseline production`,
#   `candidate staging`, `candidate candidate`, the `rerun … #1` runs, and the dry ones.
# - [ ] Open the **staging candidate** run: the run-level scores show `ci_gate = block` next to `handoff_accuracy`;
#   hover/expand it for the comment (the reasons). The run's metadata shows the commit, branch, CI run and the
#   baseline it was compared with.
# - [ ] Select the baseline and the staging candidate → **Compare**: the login rows differ; then the staging re-run
#   `#1` holds only those suspect items.
# - [ ] The clean candidate's run: `ci_gate = ship`, and `baseline_reused = true` in its metadata (no second
#   production run was paid for).
# - [ ] **Prompts** → `support-agent-system`: three labels (`production`, `staging`, `candidate`). In a real release,
#   the step after a green gate is moving `production` to the candidate's version (module 04).
# - [ ] Open `ch09/langfuse_mastery/ci/artifacts/<run id>/staging/gate-summary.md`: this is what the PR shows.
#
# ## Production notes
# - **Gate on items, confirm with re-runs.** Averages hide a broken intent; single runs block on noise. Re-run only
#   the suspects, on both sides, and vote. Track the flaky list: an item that is flaky every week is a bad test or an
#   ambiguous spec.
# - **Make false blocks rare and false passes visible.** A gate people bypass is worse than none. Keep tolerances
#   explicit (policy in the score metadata), and treat `error` (exit 2) as infrastructure, not as "your PR is bad".
# - **Pin everything the comparison depends on.** The dataset version (a reviewed repository variable), the
#   production prompt version, the model, the evaluator version. The baseline cache key is exactly that list.
# - **Record the decision where the evidence is.** The `ci_gate` score lives on the candidate experiment, with the
#   commit and the reasons; the PR links to it. A release audit is then a Langfuse query, not a CI log search.
# - **Budget the gate.** A small, sharp regression set per PR (seconds to minutes, tens of LLM calls); the big suites
#   nightly. `timeout-minutes`, `concurrency: cancel-in-progress` and `max_items` keep the bill bounded.
# - **Secrets and SHAs.** Keys come from repository secrets; on `pull_request` the commit under review is
#   `github.event.pull_request.head.sha`, not `GITHUB_SHA` (a temporary merge commit). PRs from forks get no secrets:
#   gate them with `workflow_dispatch` or a trusted label.
# - **CI never mutates prompts.** The gate reads labels; promoting `production` after a green gate is a separate,
#   audited step (module 14).
# - **Why it matters (leadership):** every prompt or agent change is checked against the failures we already know,
#   automatically, on every PR, and the verdict (with the commit and the evidence) is one link: shipping becomes a
#   measured decision, not a judgment call at 6 pm.
