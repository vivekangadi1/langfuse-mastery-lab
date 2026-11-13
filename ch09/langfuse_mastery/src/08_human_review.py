# %% [markdown]
# # Module 08: Human review (annotation queues, UI scoring, comments, human-vs-judge agreement)
#
# **Goal:** a judge (module 07) scores every request, but nobody should trust a judge blindly. The traces it scores
# **low** go into an **annotation queue**; a human (you) reviews each one, in the Langfuse UI or through the API, and
# records a verdict, an error type and a comment. Then you measure **how often the human agrees with the judge**.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **Annotation queue** | a named work list of traces / observations / sessions to review, scored with fixed **score configs** | `routing-review`, scored with `human_routing_ok` + `review_error_type` |
# | **Queue item** | one object in the queue: `objectId` + `objectType` (TRACE / OBSERVATION / SESSION), status `PENDING` → `COMPLETED` | the trace of a request the judge flagged |
# | **ANNOTATION score** | a score with `source="ANNOTATION"`: a human's verdict. **Must** reference a score config. From the UI it carries your `authorUserId`; with `queueId` it is tied to the queue | `human_routing_ok = False`, `review_error_type = wrong_team` |
# | **Comment** | free text (markdown, ≤ 5000 chars) on a trace, observation, session or prompt: the discussion, not a metric | "login → billing; the policy says account_security" |
# | **Assignment** | which project members work a queue | you |
# | **Control items** | a small random sample of traces the judge **passed**, reviewed too | the only way to see what the judge *misses* |
# | **Agreement** | judge vs human on the reviewed traces: rate, Cohen's **kappa**, flag precision | is the judge good enough to act on alone? |
#
# ```
# traffic ─► judge score (EVAL from module 07, or the stand-in below) ─► low/False? ──yes──► annotation queue
#                                                    │ no                                     ▲      │
#                                                    └── small random sample (controls) ──────┘      ▼
#                                    human: ANNOTATION scores + comment + "Mark Completed" (UI or API)
#                                                                                                    │
#                                              human-vs-judge agreement ◄── read both scores back ◄──┘
#                                              (fix the judge prompt; the confirmed failures feed datasets, module 09)
# ```
#
# **Where do the judge scores come from?** If you finished module 07, its rule (`support-live-eval`) judges traffic
# tagged `live-eval` and this notebook uses those **EVAL** scores (`JUDGE_MODE = "live"`, ≈10 judge calls made by the
# worker). If not, a **stand-in judge** (given, deterministic, keyword-based, deliberately naive) writes the same
# `routing_judge` scores with `source=API` and `metadata.evaluator="stand-in:keyword-v1"` (`JUDGE_MODE = "stand-in"`).
# Everything after that is identical. Verify accepts both and tells you which one it saw.
#
# Things verified live on this server (4.77) that shape the TODOs:
# - A queue needs **at least one score config**, and there is **no API to edit, archive or delete a queue**: get its
#   configs right the first time.
# - The queue API does **not dedupe**: adding the same trace twice creates two items. List before you add.
# - An ANNOTATION score **without `config_id` is dropped silently** (the API answers 202 *Accepted*; only the worker
#   log says `configId is required when source is ANNOTATION`).
# - Comment `object_type` must be upper case (`"TRACE"`); `"trace"` is a 400.
#
# Docs: https://langfuse.com/docs/evaluation/evaluation-methods/annotation-queues ·
# https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-ui ·
# https://langfuse.com/docs/observability/features/comments

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import importlib
import os
import subprocess
import time
import uuid
from langfuse_lab import verify, traffic, kb, standin_judge
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.failures import FailureConfig
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_mastery import review

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
RUN_TAG = f"run:m08-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
ENV = "dev"
fixtures = load_fixtures()

# Queues and score configs are PROJECT-GLOBAL and long-lived (a queue cannot even be deleted). Keep PREFIX empty.
PREFIX = ""
NAMES = {role: PREFIX + base for role, base in verify.REVIEW_NAMES.items()}
LIVE_TAG = PREFIX + "live-eval"      # module 07's opt-in tag
ROOT_NAME = verify.LIVE_ROOT_NAME    # "support-request"
THRESHOLD = 0.5                      # a NUMERIC judge score below this sends the trace to review
CONTROL_RATE = 0.25                  # share of judge-PASSED traces reviewed as controls (at least 1)

CASES = traffic.review_cases(run_id=RUN_ID)
print("run tag:", RUN_TAG)
for role, name in NAMES.items():
    print(f"  {role:<12} {name}")
for c in CASES:
    print(f"  {c.case_id:<36} expect handoff={c.expected_handoff:<19} {c.failures or ''}")

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/review.py` (pure functions + pytest)
#
# Open `ch09/langfuse_mastery/review.py` and implement:
# - `is_judge_score(score)`, `judge_flags(scores, ...)`: which traces need a human, and why.
# - `pick_controls(trace_ids, rate=...)`: a deterministic sample of judge-passed traces to review as controls.
# - `items_to_add(existing_items, wanted_ids)`: idempotent queue filling (the API does not dedupe).
# - `error_type(expected, actual)`: the reviewer's error taxonomy (the simulated reviewer uses it).
# - `agreement(pairs)`: human-vs-judge agreement, with Cohen's kappa and flag precision.
#
# The cell runs the tests and reloads your module. Re-run it until it shows `35 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_review.py", "-q", "-p", "no:cacheprovider"],
                        cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
review = importlib.reload(review)

# %% [markdown]
# ## Given: instrumentation, review score configs, and the judge
# This is a recap plus setup, not a TODO:
# - `traced_turn`: the module 01/02 instrumentation. The root `support-request` span carries
#   `metadata.handoff_target` (what a judge, and a reviewer, needs to see). It returns the root observation id too,
#   because judge scores land on the root observation.
# - `ensure_review_configs()`: module 06's get-or-create (skipping archived configs) for the two configs the queue
#   scores with. A human picks from them in the UI; the API refuses (silently) annotation scores without one.
#   `review_error_type` is the **error taxonomy**: a False verdict alone does not tell you what to fix.
# - **The judge.** `JUDGE_MODE` is `"live"` if your module 07 rule exists and is enabled, else `"stand-in"`. Set it by
#   hand to force one. In stand-in mode, `write_standin_judgment` stores the keyword judge's verdict as a
#   `routing_judge` score (source API) on the root observation, with its reasoning as the comment, exactly where a
#   live EVAL score would be.

# %%
from langfuse import get_client, observe, propagate_attributes
from langfuse.langchain import CallbackHandler

langfuse = get_client()
PROJECT_ID = langfuse.api.projects.get().data[0].id  # the project of your API key (comments need it)
UI = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


@observe(name=ROOT_NAME, as_type="agent", capture_input=False, capture_output=False)
def traced_turn(message, agent, *, session_id, user_id, tags):
    """One turn = one trace. Returns (AgentRun, trace_id, root observation id)."""
    with propagate_attributes(session_id=session_id, user_id=user_id, tags=[RUN_TAG, *tags], environment=ENV):
        run = agent.invoke(message, config={"callbacks": [CallbackHandler()]})
        langfuse.update_current_span(input=message, output=run.summary["final_answer"],
                                     metadata={"handoff_target": run.summary["handoff_target"]})
        return run, langfuse.get_current_trace_id(), langfuse.get_current_observation_id()


REVIEW_CONFIGS = {
    NAMES["human"]: dict(data_type="BOOLEAN", description="human reviewer: the agent routed this request correctly"),
    NAMES["error_type"]: dict(data_type="CATEGORICAL", description="human reviewer: what went wrong",
                              categories=[{"label": l, "value": float(i)} for i, l in enumerate(verify.ERROR_TYPES)]),
}


def ensure_review_configs():
    """Module 06 pattern: get-or-create by name, skipping archived configs. Returns {name: config dict}."""
    have = {}
    page = 1
    while True:
        resp = langfuse.api.score_configs.get(page=page, limit=100)
        for c in resp.data:
            if c.name in REVIEW_CONFIGS and not c.is_archived:
                have.setdefault(c.name, c.dict())
        if page >= (resp.meta.total_pages or 1):
            break
        page += 1
    for name, spec in REVIEW_CONFIGS.items():
        if name not in have:
            have[name] = langfuse.api.score_configs.create(name=name, **spec).dict()
    return have


def detect_judge_mode():
    rules = []
    cursor = None
    while True:
        resp = langfuse.api.evaluation_rules.list(limit=100, **({"cursor": cursor} if cursor else {}))
        rules += resp.data
        cursor = resp.meta.cursor
        if not cursor:
            break
    live = [r for r in rules if r.name == NAMES["rule"] and r.enabled]
    return "live" if live else "stand-in"


def write_standin_judgment(case, run, trace_id, root_id):
    """The stand-in judge: a deterministic keyword policy, stored like a judge score (on the root observation)."""
    ok, reasoning = standin_judge.judge_routing(case.message, run.summary["handoff_target"])
    langfuse.create_score(name=NAMES["routing"], value=1.0 if ok else 0.0, data_type="BOOLEAN", trace_id=trace_id,
                          observation_id=root_id, comment=reasoning, metadata={"evaluator": standin_judge.EVALUATOR})


def wait_for_judges(rows, judges, *, timeout=300):
    """Poll until every row has a judge score from each of `judges` (live EVAL scores take ~30 s)."""
    deadline, missing = time.time() + timeout, rows
    while missing and time.time() < deadline:
        missing = []
        for r in rows:
            got = {s.name for s in langfuse.api.scores.get_many(trace_id=r["trace_id"], fields="details", limit=100).data
                   if verify.is_judge_score(verify._row(s))}
            if not set(judges) <= got:
                missing.append(r)
        if missing:
            time.sleep(5)
    if missing:
        raise TimeoutError(f"{len(missing)} trace(s) without judge scores {judges}: docker logs --tail 50 ch09_langfuse_worker")
    print(f"all {len(rows)} trace(s) judged by {judges}")


CONFIGS = ensure_review_configs()
JUDGE_MODE = detect_judge_mode()
# JUDGE_MODE = "stand-in"   # uncomment to force the stand-in judge (no Azure judge calls)
JUDGES = [NAMES["routing"]] + ([NAMES["helpfulness"]] if JUDGE_MODE == "live" else [])
clean_agent = SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)
planted_agent = SupportAgent(failures=FailureConfig.from_names(["wrong_handoff"]),  # bad prompt: login -> billing
                             guardrail_fn=observed_guardrail, search_fn=observed_search)
print("review configs:", {k: v["id"] for k, v in CONFIGS.items()})
print("judge mode:", JUDGE_MODE, "| judges:", JUDGES)

# %% [markdown]
# ## TODO 2: the annotation queue
#
# Write `ensure_queue(configs) -> AnnotationQueue`: get-or-create the queue named `NAMES["queue"]`.
# - List all queues: `langfuse.api.annotation_queues.list_queues(page=…, limit=100)`, paging until
#   `resp.meta.total_pages`. Pick the one with this name.
# - Not found: `langfuse.api.annotation_queues.create_queue(name=…, description=…, score_config_ids=[…])` with the
#   ids of **both** review configs (`configs[name]["id"]`).
# - Found: check its `score_config_ids` contain both ids. If not, **raise** with a clear message. There is no API to
#   update a queue (fix it in the UI: Annotation Queues → the queue → edit, or pick a new name).
#
# Names are unique per project (a second create answers 400 "A queue with this name already exists."), but a 400 is
# not a lookup: list first.

# %%
# TODO 2: your code here

# %%
queue = ensure_queue(CONFIGS)
assert ensure_queue(CONFIGS).id == queue.id
QUEUE_URL = f"{UI}/project/{PROJECT_ID}/annotation-queues/{queue.id}"
print(queue.name, queue.id, "configs:", queue.score_config_ids)
print("open it:", QUEUE_URL)

# %% [markdown]
# ## Run it, including the planted failure
# Five single-turn cases (about 15 agent calls). The login case runs on the **`wrong_handoff`** prompt
# (login → billing). In live mode every trace carries `LIVE_TAG`, so your module 07 rule judges it; in stand-in mode
# the stand-in judge scores it right away. Then wait until every trace has its judge score(s).

# %%
rows = []
for case in CASES:
    agent = planted_agent if case.failures else clean_agent
    tags = [case.case_id, *case.failures, *([LIVE_TAG] if JUDGE_MODE == "live" else [])]
    run, trace_id, root_id = traced_turn(case.message, agent, session_id=case.session_id, user_id=case.user_id, tags=tags)
    if JUDGE_MODE == "stand-in":
        write_standin_judgment(case, run, trace_id, root_id)
    rows.append({"case": case, "trace_id": trace_id, "root_id": root_id, "actual": run.summary["handoff_target"]})
langfuse.flush()
wait_for_judges(rows, JUDGES)

for r in rows:
    print(f"{r['case'].case_id:<36} expected={r['case'].expected_handoff:<19} actual={r['actual']:<19} "
          f"{langfuse.get_trace_url(trace_id=r['trace_id'])}")

# %% [markdown]
# ## TODO 3: fill the queue from low judge scores (+ controls)
#
# Write `fill_queue(queue, rows) -> list[dict]`:
# 1. For each row, read its scores: `langfuse.api.scores.get_many(trace_id=…, fields="details,subject,annotation",
#    limit=100)` and turn them into dicts (`s.dict()`). Compute
#    `review.judge_flags(scores, boolean_judges=[NAMES["routing"]], numeric_judges=[NAMES["helpfulness"]],
#    threshold=THRESHOLD)`.
# 2. Flagged rows are queued with their reasons. From the **unflagged** ones, `review.pick_controls(ids,
#    rate=CONTROL_RATE, salt=RUN_ID)` picks the controls (reason `"control"`).
# 3. List the queue's existing items (`langfuse.api.annotation_queues.list_queue_items(queue.id, page=…, limit=100)`,
#    paging) and add only `review.items_to_add(existing, wanted_ids)`:
#    `langfuse.api.annotation_queues.create_queue_item(queue.id, object_id=trace_id, object_type="TRACE")`.
# 4. Print one line per queued row (case, reason, the judge's comment) and return the queued rows, each with a
#    `"reason"` key added.
#
# Running it twice must add nothing the second time: the cell checks the item count.
#
# Why controls: a queue of judge flags shows you the judge's **false alarms**, never its **misses**. A few passed
# traces reviewed blind are how you estimate what the judge lets through.

# %%
# TODO 3: your code here

# %%
def run_items():
    out, page = [], 1
    while True:
        resp = langfuse.api.annotation_queues.list_queue_items(queue.id, page=page, limit=100)
        out += [i for i in resp.data if i.object_id in {r["trace_id"] for r in rows}]
        if page >= (resp.meta.total_pages or 1):
            return out
        page += 1


queued = fill_queue(queue, rows)
n_items = len(run_items())
fill_queue(queue, rows)
assert len(run_items()) == n_items == len(queued), "fill_queue must be idempotent (the API does not dedupe items)"
print(f"{n_items} item(s) of this run in the queue")

# %% [markdown]
# ## Your turn as the reviewer: the UI (recommended)
#
# Open the queue (link printed above, or **Annotation Queues** in the left nav → `routing-review` → **Process
# queue**). For at least one item, ideally the planted login → billing one:
# - read the trace: input, the agent's answer, `metadata.handoff_target`, and the judge's score + reasoning;
# - set **`human_routing_ok`** (True/False) in the annotation panel, and for False a **`review_error_type`**. Look at
#   what the agent *actually* did: the bad prompt sends login problems to billing (`wrong_team`), but sometimes it
#   hands off to nobody (`missed_handoff`, seen during validation);
# - click **Comment** on the trace and say why (this is what the next person reads);
# - **Mark Completed** (Ctrl+Enter). Langfuse moves to the next item.
#
# A score set in the UI is `source=ANNOTATION` with **your** `authorUserId` and the queue's `queueId`; verify counts
# how many reviews came from the UI. Then come back and run on: TODO 4 handles whatever is still PENDING.

# %%
print("review here:", QUEUE_URL)
for i in run_items():
    case = next(r["case"].case_id for r in rows if r["trace_id"] == i.object_id)
    print(f"  {i.status:<9} {case:<36} {UI}/project/{PROJECT_ID}/annotation-queues/{queue.id}/items/{i.id}")

# %% [markdown]
# ## TODO 4: the scripted reviewer (for every item still PENDING)
#
# In production the reviewer is a person; here a **simulated reviewer** with the answer key (`case.expected_handoff`)
# finishes what you didn't do in the UI. Write `review_pending(queue, rows) -> int` (number reviewed):
# - For each **PENDING** item of this run (`list_queue_items(queue.id, status="PENDING", …)`), find its row.
#   `label = review.error_type(case.expected_handoff, row["actual"])`; the routing is ok when `label is None`.
# - Send the human's scores with the **public scores API**, because only it can set the source:
#   `langfuse.api.scores.create(request=[CreateScoreRequest(id=…, name=NAMES["human"], value=1.0 or 0.0,
#   data_type="BOOLEAN", config_id=…, trace_id=…, source="ANNOTATION", queue_id=queue.id, comment=…)])`, plus, when
#   not ok, a `NAMES["error_type"]` score (`data_type="CATEGORICAL"`, `value=label`). Use deterministic ids
#   (`f"{trace_id}-{name}"`) so a re-run updates instead of duplicating. `langfuse.create_score` cannot do this: it
#   has no `source` and always writes `API`.
# - Add a trace comment: `langfuse.api.comments.create(project_id=PROJECT_ID, object_type="TRACE",
#   object_id=trace_id, content=…)`. Markdown works; say what the judge said and what you decided.
# - Close the item: `langfuse.api.annotation_queues.update_queue_item(queue.id, item.id, status="COMPLETED")`.
#
# Traps (verified live): without `config_id` an ANNOTATION score is **dropped silently** (202 Accepted, nothing
# stored). `object_type="trace"` (lower case) is a 400.

# %%
from langfuse.api.scores.types import CreateScoreRequest

# TODO 4: your code here

# %%
print("reviewed by script:", review_pending(queue, rows))
langfuse.flush()

# %% [markdown]
# ## TODO 5: human vs judge
#
# Write `human_vs_judge(queue, rows, *, timeout=120) -> list[tuple]`:
# - For each item of this run in the queue, poll `langfuse.api.scores.get_many(trace_id=…,
#   fields="details,subject,annotation")` until the trace has a `NAMES["human"]` score with `source == "ANNOTATION"`
#   (annotation scores are ingested asynchronously, a few seconds). Raise `TimeoutError` naming what is missing.
# - The judge verdict is the `NAMES["routing"]` judge score (`review.is_judge_score`); the human verdict is the
#   ANNOTATION score. Both are BOOLEAN (`True`/`False` on read).
# - Print one line per item: case, the queue reason, judge verdict, human verdict, who reviewed (`authorUserId`, or
#   "script"), and for **disagreements** the judge's reasoning (its score comment). Those lines are the input for
#   fixing the judge prompt.
# - Return `[(judge_ok, human_ok), …]`.
#
# Then `REPORT = review.agreement(pairs)`. Read it with the queue in mind: the items are mostly **judge flags**, so
# `flag_precision` (how many flags were real) is the honest number here; `judge_missed` can only come from controls.

# %%
# TODO 5: your code here

# %%
pairs = human_vs_judge(queue, rows)
REPORT = review.agreement(pairs)
print(REPORT)

# %% [markdown]
# ## TODO 6 (optional): assign the queue to a reviewer
#
# `langfuse.api.annotation_queues.create_queue_assignment(queue.id, user_id=…)` assigns a project member (idempotent;
# an unknown user is a 404 `NotFoundError`). The catch: a **project** API key cannot list members
# (`organizations.get_project_memberships` → 403, it needs an organization key, module 13). A user id you *can* see
# is the `authorUserId` of any score you made in the UI. The cell below finds one (or reads
# `LANGFUSE_REVIEWER_USER_ID`). Write `assign_reviewer(queue, user_id)`, catching the 404.
#
# Verify reports this ⚠️: the public API can create and delete assignments but has **no endpoint to list them**, so
# it cannot be checked. See it in the UI (the queue's assignees).

# %%
from langfuse.api.commons.errors import NotFoundError

# TODO 6 (optional): your code here

# %%
reviewer = os.environ.get("LANGFUSE_REVIEWER_USER_ID") or next(
    (s.author_user_id for r in rows for s in langfuse.api.scores.get_many(
        trace_id=r["trace_id"], fields="annotation", limit=100).data if getattr(s, "author_user_id", None)), None)
if reviewer and "assign_reviewer" in globals():
    assign_reviewer(queue, reviewer)
else:
    print("no reviewer user id known yet (score one item in the UI first), or TODO 6 skipped")

# %% [markdown]
# Verify waits for all 5 traces, then checks:
# - both review configs (once each) and the queue scoring with them;
# - a judge verdict on every trace, and the queue holding exactly the flagged traces plus 1–2 controls, each once;
# - every queued trace COMPLETED with an ANNOTATION verdict tied to the queue, a comment, and an error type when the
#   human said "not ok";
# - your `REPORT` against the stored scores;
# - the planted `wrong_handoff`: flagged by the judge, queued, and confirmed by the human with the error type that
#   matches what the agent actually did (login → billing is `wrong_team`; login → no handoff is `missed_handoff`).

# %%
verify.check_module_08(RUN_TAG, prefix=PREFIX, threshold=THRESHOLD, report=REPORT, expected_traces=len(CASES))

# %% [markdown]
# ## UI tour
# - [ ] **Annotation Queues**: `routing-review` with its two score configs and its progress (completed / pending).
#   Open the queue's settings: description, configs, assignees.
# - [ ] Open a reviewed item: the annotation panel shows `human_routing_ok` and `review_error_type` next to the
#   judge's `routing_judge`. Hover the judge score to read its reasoning.
# - [ ] The trace's **Comments** (speech-bubble icon): your review note, rendered as markdown.
# - [ ] **Scores** (left nav): filter `source = ANNOTATION`. Your UI reviews show your user; the scripted ones none.
# - [ ] Traces table: add the `human_routing_ok` and `routing_judge` score columns and filter
#   `human_routing_ok = false`. These are the **confirmed** failures, the ones worth turning into dataset items
#   (module 09).
# - [ ] Find the `kb_refund_policy` trace: if the judge flagged it and the human disagreed, that's a judge false alarm.
#   In live mode, that's a reason to edit the judge prompt (a new evaluator version, module 07).
#
# ## Production notes
# - **Review what the judge doubts, plus a blind sample.** Queue every low/False judge score, and a small random
#   share of passed traffic as controls. Without controls you measure false alarms but never misses.
# - **Configs make reviews comparable.** Every reviewer picks from the same `human_routing_ok` / `review_error_type`,
#   so reviews can be counted, compared and turned into a metric. Keep free text for comments.
# - **An error taxonomy beats a thumbs-down.** `wrong_team` vs `missed_handoff` vs `unnecessary_handoff` tells
#   engineering what to fix. Grow it from the comments reviewers keep writing.
# - **Measure the judge with kappa, not just accuracy.** When 90% of traffic is fine, a judge that always says "fine"
#   gets 90% agreement and kappa 0. Re-measure on every judge prompt version and after model changes.
# - **Plan for queue hygiene.** Queues can't be deleted or edited through the API, items aren't deduped, and
#   assignments can't be listed: make queue creation and filling idempotent code (like here), and name queues by
#   purpose, not by date.
# - **Privacy:** reviewers see full inputs and outputs. Mask PII before ingestion (module 05), and keep PII out of
#   comments.
# - **Why it matters (leadership):** humans spend their time only where the automatic judge is unsure, every
#   confirmed failure becomes a labelled example, and we can state in one number how far the judge can be trusted.
