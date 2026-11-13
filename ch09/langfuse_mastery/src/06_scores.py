# %% [markdown]
# # Module 06: Scores (configs, targets, feedback and corrections)
#
# **Goal:** every request gets **quality numbers** that everyone defines the same way. These are:
# - code scores the agent can't argue with,
# - an end-user feedback hook with corrections,
# - all of it attached to the right **target**, and validated before it's sent.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **Score** | name + value + data type + **target**, with an optional comment and metadata | `handoff_correct=False` on one trace |
# | **Target** | what the score is about: a **trace**, an **observation** or a **session** | trace: the answer · observation: the `kb-search` retriever · session: the whole chat |
# | **Score config** | the schema for one score name: type, range, categories | `kb_hit` is NUMERIC 0..1; `user_feedback` is `thumbs_up`/`thumbs_down` |
# | **Data types** | NUMERIC, BOOLEAN, CATEGORICAL (+ TEXT; and CORRECTION, which takes no config) | CORRECTION holds the *right* answer |
# | **Source** | who produced it: `API` (code, users), `EVAL` (LLM judges, module 07), `ANNOTATION` (humans, module 08) | all `API` here |
#
# ```
# Session  …-s_refund_followup  ─── csat = 4                      (session score, end-of-chat survey)
#
# Trace  support-request "I cannot log in…"  ─── handoff_correct = False   (code score, trace)
#   │                                        ─── user_feedback  = thumbs_down "wrong team"
#   │                                        ─── output (CORRECTION) = "…routed to account security…"
#   └── … tools → search_kb → kb-search (retriever) ─── kb_hit = 1.0   (code score, observation)
# ```
#
# **Why configs?** Without them, team A sends `helpfulness` as 1-5, team B as 0-1, and the dashboard averages both.
# A config fixes the type and range once. The server then **rejects** scores that break it, and (as the demo shows)
# it rejects them *silently*.
#
# Docs: https://langfuse.com/docs/evaluation/evaluation-methods/data-model ·
# https://langfuse.com/docs/evaluation/evaluation-methods/custom-scores ·
# https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-ui ·
# https://langfuse.com/docs/observability/features/user-feedback

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import importlib
import subprocess
import time
import uuid
from langfuse_lab import verify, traffic, kb
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.failures import FailureConfig
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_mastery import scoring

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
RUN_TAG = f"run:m06-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
ENV = "dev"
fixtures = load_fixtures()
CASES = traffic.score_demo_cases(run_id=RUN_ID)
SESSION = traffic.score_demo_session(run_id=RUN_ID)
print("run tag:", RUN_TAG)
for c in CASES:
    print(f"  {c.case_id:<36} expect handoff={c.expected_handoff:<19} kb={c.expected_kb or '-':<11} {c.failures or ''}")

# %% [markdown]
# ## Given: the instrumentation you built in modules 01–02
# This is a recap, not a TODO. One addition: the retriever records `(its observation id, the article ids it returned)`
# in `RETRIEVALS`. Scoring an **observation** needs its id, and the only place that knows it is the code running
# inside that span (`get_current_observation_id()`).

# %%
from langfuse import get_client, observe, propagate_attributes
from langfuse.langchain import CallbackHandler

langfuse = get_client()
RETRIEVALS = []  # [(observation_id, [article ids])] for the turn in progress


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    results = kb.search(query, fixtures.kb, k=k)
    RETRIEVALS.append((langfuse.get_current_observation_id(), [a["id"] for a in results]))
    return results


@observe(name="support-request", as_type="agent", capture_input=False, capture_output=False)
def traced_turn(message, agent, *, session_id, user_id, tags, history=None):
    """One turn = one trace. Returns (AgentRun, trace_id) so the caller can score the trace afterwards."""
    with propagate_attributes(session_id=session_id, user_id=user_id, tags=[RUN_TAG, *tags], environment=ENV):
        run = agent.invoke(message, history=history or [], config={"callbacks": [CallbackHandler()]})
        langfuse.update_current_span(input=message, output=run.summary["final_answer"])
        return run, langfuse.get_current_trace_id()


clean_agent = SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)
planted_agent = SupportAgent(failures=FailureConfig.from_names(["wrong_handoff"]),  # bad prompt: login -> billing
                             guardrail_fn=observed_guardrail, search_fn=observed_search)

# %% [markdown]
# ## TODO 1: score configs, defined once
#
# Write `ensure_score_configs() -> dict[str, dict]`. It should get-or-create these configs **by name**:
#
# | name | data type | range / categories | produced by |
# |---|---|---|---|
# | `handoff_correct` | BOOLEAN | n/a | your code (TODO 3) |
# | `kb_hit` | NUMERIC | 0..1 | your code, on the retriever observation (TODO 3) |
# | `user_feedback` | CATEGORICAL | `thumbs_up`=1, `thumbs_down`=0 | the end user (TODO 4) |
# | `csat` | NUMERIC | 1..5 | the end user, on the session (TODO 4) |
#
# Return `{name: config_dict}`, where `config_dict = config.dict()`. That gives camelCase keys (`id`, `dataType`,
# `minValue`, `maxValue`, `categories`), the shape `scoring.validate_score` expects.
#
# Two things the server does **not** do for you (both verified live):
# - **Names are not unique.** Calling `create` twice makes two `kb_hit` configs, and your scores split between them.
#   So list first, and create only what's missing.
# - **Archived configs are still listed** (`is_archived=True`). Skip them.
#
# Hints:
# - `langfuse.api.score_configs.get(page=1, limit=100)` (`.data`, `.meta.total_pages`)
# - `langfuse.api.score_configs.create(name=, data_type=, min_value=, max_value=, categories=[{"label": …, "value": …}],
#   description=)`
#
# The cell after yours runs it **twice** and checks that nothing new was created.

# %%
# TODO 1: your code here

# %%
first, second = ensure_score_configs(), ensure_score_configs()
assert {k: v["id"] for k, v in first.items()} == {k: v["id"] for k, v in second.items()}, "second call created new configs"
configs = second
for name, cfg in configs.items():
    print(f"{name:<16} {cfg['dataType']:<12} {cfg.get('minValue')}..{cfg.get('maxValue')}  {cfg['id']}")

# %% [markdown]
# ## Demo: the server drops invalid scores *silently*
# This cell sends two scores against your `kb_hit` config (0..1): a valid `0.5` and an invalid `7`. Then it reads
# back what was stored. The SDK reports nothing, and only one score arrives.
# The only evidence is a line in the **worker's** log:
# `docker logs ch09_langfuse_worker --since 5m | findstr "not valid against provided config"`.
#
# That's why TODO 2 validates scores **before** sending them.

# %%
with langfuse.start_as_current_observation(name="m06-demo", as_type="span") as demo:
    demo.update(input="silent rejection demo", output="ok")
    demo_trace = langfuse.get_current_trace_id()   # not tagged RUN_TAG on purpose: verify ignores it
for value in (0.5, 7):
    langfuse.create_score(trace_id=demo_trace, name="kb_hit", value=value, data_type="NUMERIC",
                          config_id=configs["kb_hit"]["id"], comment=f"demo value {value}")
langfuse.flush()

stored = []
for _ in range(10):
    stored = langfuse.api.scores.get_many(trace_id=demo_trace, fields="details").data
    if stored:
        break
    time.sleep(3)
time.sleep(3)  # give the invalid one the same chance to arrive
stored = langfuse.api.scores.get_many(trace_id=demo_trace, fields="details").data
print("sent 2 scores, stored:", [(s.value, s.comment) for s in stored])

# %% [markdown]
# ## TODO 2: implement `langfuse_mastery/scoring.py` (pure functions + pytest)
#
# Open `ch09/langfuse_mastery/scoring.py` and implement:
# - `validate_score(config, value)`: would Langfuse accept this value? Returns `(ok, reason)`.
# - `handoff_correct(actual, expected)`
# - `kb_hit(returned_ids, expected_id)`
#
# There's no Langfuse code in that file. These are plain functions, so they're testable in milliseconds and reusable
# in the CI gate (module 10). The cell below runs the tests and reloads your module. Re-run it until it shows
# `32 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_scoring.py", "-q", "-p", "no:cacheprovider"],
                        cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
scoring = importlib.reload(scoring)

# %% [markdown]
# ## TODO 3: code scores on the trace and on the retriever observation
#
# Write two functions.
#
# **`send_score(configs, name, value, *, comment=None, evaluator="code:v1", score_id=None, **target)`** is the single
# path every config-backed score goes through:
# - It runs `scoring.validate_score(configs[name], value)` first and **raises `ValueError(reason)`** if the value is
#   invalid. Fail loudly instead of the server dropping it silently.
# - It calls `langfuse.create_score(name=…, value=…, data_type=…, config_id=…, comment=…, score_id=…,
#   metadata={"evaluator": evaluator}, **target)`. Here `target` is `trace_id=`, `observation_id=` and/or `session_id=`.
# - Send BOOLEAN values as `1.0` / `0.0`.
#
# **`score_case(case, run, trace_id, retrievals, configs) -> bool`:**
# - Trace-level `handoff_correct`: compare `run.summary["handoff_target"]` with `case.expected_handoff`. When wrong,
#   comment `"expected X, got Y"` (the comment is what a reviewer reads first).
# - Observation-level `kb_hit`: for each `(observation_id, ids)` in `retrievals`, if
#   `scoring.kb_hit(ids, case.expected_kb)` is not None, score that observation (`trace_id=` **and**
#   `observation_id=`).
# - Return whether the handoff was correct.
#
# `metadata={"evaluator": "code:v1"}` is **provenance**. When you change the scoring logic, bump the version, and you
# can still tell old numbers from new ones.

# %%
# TODO 3: your code here

# %% [markdown]
# ## TODO 4: the end-user feedback hook (votes, corrections, CSAT)
#
# **`record_feedback(configs, trace_id, *, thumbs_up, comment="", correction=None)`:**
# - Sends `user_feedback` = `"thumbs_up"` / `"thumbs_down"` through `send_score` with `evaluator="user"`.
# - Uses a **deterministic** `score_id=f"{trace_id}-user_feedback"`. If the user changes their vote, the same id
#   **updates** the score instead of adding a second one (verified live). One person, one vote.
# - If `correction` is given, also sends `create_score(trace_id=…, name="output", value=correction,
#   data_type="CORRECTION")`. CORRECTION takes no config. This is the *right* answer, and module 09 turns it into
#   dataset ground truth.
#
# **`record_csat(configs, session_id, rating)`:** a session-level `csat` (1..5) via `send_score(…, session_id=…)`.
#
# **`feedback_report(trace_id)`:** reads the scores back with
# `langfuse.api.scores.get_many(trace_id=…, fields="details,subject")` and prints name, data type, target kind,
# value and comment, one per line. This is the export path pipelines use. Without `fields`, comment, config and
# target are missing.

# %%
# TODO 4: your code here

# %% [markdown]
# ## Run it, including the planted failure
# There are four single-turn cases. The login case runs on the **`wrong_handoff`** prompt, which routes login problems
# to billing. Your `handoff_correct` must catch it. The simulated user then reacts:
# - right handoff: thumbs up;
# - wrong handoff: thumbs **up** first (a hasty click), then changes to thumbs **down** with a comment and the
#   correct answer.
#
# After the cases, the 2-turn `s_refund_followup` chat runs and gets a CSAT of 4. That's 6 traces and about 18 Azure
# calls. The cell calls your functions by these exact names.

# %%
rows, planted_trace = [], None
for case in CASES:
    RETRIEVALS.clear()
    agent = planted_agent if case.failures else clean_agent
    run, trace_id = traced_turn(case.message, agent, session_id=case.session_id, user_id=case.user_id,
                                tags=[case.case_id, *case.failures])
    ok = score_case(case, run, trace_id, list(RETRIEVALS), configs)
    if ok:
        record_feedback(configs, trace_id, thumbs_up=True)
    else:
        record_feedback(configs, trace_id, thumbs_up=True)   # hasty click...
        langfuse.flush()
        time.sleep(2)                                         # ...a moment later, the user changes their mind
        record_feedback(configs, trace_id, thumbs_up=False, comment="Sent me to the wrong team.",
                        correction=case.reference_answer)
        planted_trace = trace_id
    rows.append((case.case_id, case.expected_handoff, run.summary["handoff_target"], ok, langfuse.get_trace_url(trace_id=trace_id)))

history = []
for message in SESSION.turns:
    run, _ = traced_turn(message, clean_agent, session_id=SESSION.session_id, user_id=SESSION.user_id,
                         tags=[SESSION.intent], history=history)
    history = run.messages
record_csat(configs, SESSION.session_id, 4)
langfuse.flush()

print(f"{'case':<36} {'expected':<19} {'actual':<19} handoff_correct")
for case_id, expected, actual, ok, url in rows:
    print(f"{case_id:<36} {expected:<19} {actual:<19} {ok}   {url}")

# %%
time.sleep(5)  # ingestion is async
if planted_trace:
    feedback_report(planted_trace)
else:
    print("the planted case was scored correct: check score_case (verify will flag it too)")

# %% [markdown]
# `expected_traces=6` = 4 cases + 2 session turns. Verify waits for all of them, then checks:
# - the configs exist exactly once,
# - scores sit on the right targets and reference their configs,
# - one vote per trace,
# - the planted `wrong_handoff` was scored `handoff_correct=False`.

# %%
verify.check_module_06(RUN_TAG, session_id=SESSION.session_id, expected_traces=6)

# %% [markdown]
# ## UI tour
# - [ ] **Traces** table: add the **Scores** columns. Filter on `handoff_correct = false`: one click to every bad
#   handoff.
# - [ ] Open the planted trace: the **Scores** tab shows code score, feedback (one vote, with comment) and the
#   correction.
# - [ ] Open the `kb-search` observation of the refund-policy trace: `kb_hit` is attached **to that span**, not to the trace.
# - [ ] **Sessions** → `…-s_refund_followup`: the `csat` score sits on the session itself.
# - [ ] **Settings → Scores / Score Configs**: your 4 configs (and archived probe configs, if any).
# - [ ] **Scores** analytics (Dashboards / score analytics): the distribution of `handoff_correct` and `user_feedback`.
#
# ## Production notes
# - **Define configs once, as code** (like `ensure_score_configs`, run at deploy time) and reference them by id. Never
#   create configs from request-handling code.
# - **Validate before sending.** The server drops invalid scores silently, and a mis-scaled score (0–100 into a 0–1
#   config) leaves your quality dashboard empty without any error.
# - **One vote per user per trace:** use a deterministic `score_id`, so a changed vote updates the score.
# - **Provenance in metadata** (`evaluator: code:v1`) lets you tell numbers from old and new scoring logic apart.
# - **Corrections are ground truth.** Every thumbs-down with a correction is a future regression test (module 09).
# - **Browser feedback:** the frontend should call *your* backend, which writes the score. Never ship the secret key to
#   a browser. Langfuse's public-key-only web SDK exists for exactly this.
# - **No PII in comments**: they're shown everywhere.
# - **Why it matters (leadership):** one definition of "quality", measured on every request, that every team reports
#   against. Every unhappy customer leaves behind the correct answer.
