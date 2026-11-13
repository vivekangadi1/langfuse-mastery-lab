# %% [markdown]
# # Module 04: Prompt management (versions, labels, caching, linking, config, rollback)
#
# **Goal:** take the agent's system prompt out of the code. After this module:
# - the prompt lives in Langfuse as **versions**, and the agent serves whichever version holds the **`production`** label,
# - every LLM call in every trace says **which prompt version produced it**,
# - a bad prompt change is **rolled back by moving a label**, in seconds and with no code deploy.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **Prompt / version** | a named template; every `create_prompt` adds an **immutable** version (v1, v2, …) | `support-agent-system`: the routing rules from `langfuse_lab/prompts.py` |
# | **Label** | a movable pointer to one version. Unique per prompt. `latest` is automatic. | `production` = what the agent serves; `staging` = the candidate under test |
# | **Variables** | `{{name}}` placeholders filled in by the app at compile time | `{{company}}`, `{{max_sentences}}` |
# | **Config** | free-form JSON stored **with** the version: model parameters, app settings | `{"temperature": 0, "max_tokens": 400}` |
# | **Client cache** | `get_prompt` caches per process for `cache_ttl_seconds` (default 60), stale-while-revalidate | no network call per request; label moves land within one TTL |
# | **Fallback** | the prompt returned when Langfuse can't be reached (or the label doesn't exist) | the agent keeps answering during a Langfuse outage |
# | **Linking** | the generation records `promptName` + `promptVersion` | filter traces, cost and latency **by prompt version** |
# | **Playground** | UI to try a prompt version against a model, side by side | UI tour |
#
# ```
#  Langfuse  support-agent-system   v1 ── labels: [production]          v2 ── labels: [staging, latest]
#                                       ▲  move a label = deploy / rollback (no code change)
#  agent process ── get_prompt(label="production", cache_ttl_seconds=…, fallback=…) ── cache ─┐
#        │                                                                                    │
#        ├── compile {{company}}, {{max_sentences}}  → system prompt for this turn  ◄──────────┘
#        ├── config → llm_params (temperature, max_tokens) for this turn
#        └── trace ─► GENERATION  promptName=support-agent-system  promptVersion=1  modelParameters={…}
# ```
#
# **The planted failure, `wrong_handoff` as a bad prompt version:** a teammate "simplifies" the routing rules. Their v2
# sends login problems to **billing** instead of **account security**. It gets promoted to `production`, live login
# traffic is misrouted, and you roll it back by moving the label. Nothing in the agent's code changes at any point,
# so the only way to know which version caused which answer is the **prompt link on each generation**.
#
# Docs: https://langfuse.com/docs/prompt-management/get-started ·
# https://langfuse.com/docs/prompt-management/features/prompt-version-control ·
# https://langfuse.com/docs/prompt-management/features/caching ·
# https://langfuse.com/docs/prompt-management/features/link-to-traces ·
# https://langfuse.com/docs/prompt-management/features/config ·
# https://langfuse.com/docs/prompt-management/features/playground

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import importlib
import json
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from langfuse_lab import verify, traffic, kb
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_lab.prompts import (PROMPT_CONFIG, PROMPT_NAME, PROMPT_VARIABLES, SYSTEM_PROMPT, SYSTEM_PROMPT_TEMPLATE,
                                  WRONG_HANDOFF_TEMPLATE)
from langfuse_mastery import prompting

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
RUN_TAG = f"run:m04-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
RUN_STARTED = datetime.now(timezone.utc)
ENV = "dev"
CACHE_TTL = 10  # seconds. Production default is 60; short here so label moves made elsewhere show up quickly.
fixtures = load_fixtures()
CASES = traffic.prompt_demo_cases(run_id=RUN_ID)
print("run tag:", RUN_TAG, "| prompt:", PROMPT_NAME, "| variables:", PROMPT_VARIABLES, "| config:", PROMPT_CONFIG)
for c in CASES:
    print(f"  {c.phase:<9} {c.case_id:<22} {c.user_id}  expect {c.expected_handoff:<19} {c.failures or ''}")

# %% [markdown]
# ## Given: the instrumentation you built in modules 01–02
# This is a recap, not a TODO. One agent serves every phase of this module: its code never changes. What changes is
# the prompt it is **handed per turn**. `SupportAgent.invoke(..., system_prompt=..., llm_params=...)` overrides the
# built-in prompt and binds model parameters for that one turn.

# %%
from langfuse import get_client, observe, propagate_attributes
from langfuse.api import NotFoundError
from langfuse.langchain import CallbackHandler

langfuse = get_client()


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


AGENT = SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)

print("the template you will move into Langfuse (from langfuse_lab/prompts.py):\n")
print(SYSTEM_PROMPT_TEMPLATE)

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/prompting.py` (pure functions + pytest)
#
# Open `ch09/langfuse_mastery/prompting.py` and implement:
# - `template_variables(template)`: the `{{variables}}` a template needs.
# - `compile_strict(template, variables)`: fill them in, and **fail loudly** on a missing one. Langfuse's own
#   `prompt.compile()` silently leaves `{{max_sentences}}` in the text (verified live), and the model then reads it literally.
# - `llm_params(config)`: turn a prompt's free-form config into the model kwargs you'll send, rejecting values the
#   model API would refuse. Anyone with UI access can edit a config, so treat it as input.
#
# There's no Langfuse code in that file. The cell below runs its tests and reloads your module. Re-run it until it
# shows `27 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_prompting.py", "-q",
                         "-p", "no:cacheprovider"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
prompting = importlib.reload(prompting)

# %% [markdown]
# ## TODO 2: get-or-create a prompt version, by content
#
# Write `ensure_version(template, *, config=PROMPT_CONFIG, labels=(), commit_message="") -> int`. It returns the
# version number of the `PROMPT_NAME` **text** prompt whose text **and** config equal the arguments, creating that
# version only if none exists yet.
#
# Why by content? **Langfuse never dedupes** (verified live): calling `create_prompt` twice with the same text makes v1
# **and** v2. A deploy script that runs on every release would add a new version each time, and the version history
# would stop meaning anything.
#
# Requirements:
# - List the prompt's versions, fetch each, and compare `prompt` (the template text) and `config`.
# - If the prompt doesn't exist yet, the listing is simply empty.
# - Create with `type="text"`, the given `config`, `labels=list(labels)` and `commit_message`. When the version already
#   exists, **don't touch its labels**: moving labels is TODO 4's job.
#
# Hints:
# - `langfuse.api.prompts.list(name=PROMPT_NAME)` returns `.data`, a list of prompt metas with `.versions` (a list of ints).
# - `langfuse.api.prompts.get(PROMPT_NAME, version=v)` returns `.prompt`, `.config`, `.labels`, `.version`.
# - `langfuse.create_prompt(name=, prompt=, type="text", config=, labels=, commit_message=)` returns `.version`.
#
# The cell after yours creates the good version (labelled `production`), runs it **again**, and checks that nothing
# new was created.

# %%
# TODO 2: your code here

# %%
GOOD = ensure_version(SYSTEM_PROMPT_TEMPLATE, labels=["production"],
                      commit_message="v1: routing rules moved out of langfuse_lab/prompts.py")
assert ensure_version(SYSTEM_PROMPT_TEMPLATE) == GOOD, "second call created a new version: compare text + config first"
row = langfuse.api.prompts.get(PROMPT_NAME, version=GOOD)
print(f"{PROMPT_NAME} v{GOOD}  labels={row.labels}  config={row.config}  variables={prompting.template_variables(row.prompt)}")

# %% [markdown]
# ## TODO 3: serve the prompt at runtime, and link it to the generations
#
# Write two functions.
#
# **`serve_prompt(label="production") -> (prompt, system_text, params)`**
# - `langfuse.get_prompt(PROMPT_NAME, label=label, cache_ttl_seconds=CACHE_TTL, fallback=SYSTEM_PROMPT_TEMPLATE)`.
#   The **fallback** is what the agent runs on when the fetch fails (Langfuse down, label missing). It's returned with
#   `prompt.is_fallback == True`, `version == 0` and an empty config.
# - `system_text = prompting.compile_strict(prompt.prompt, PROMPT_VARIABLES)`: your strict compile, not `prompt.compile()`.
# - `params = prompting.llm_params(prompt.config)`: the version's model parameters.
#
# **`traced_turn(case) -> (run, trace_id, served_version)`**, one `support-request` trace per case, as in module 02:
# - `@observe(name="support-request", as_type="agent", capture_input=False, capture_output=False)`. `capture_output=False`
#   matters because the function returns a tuple: otherwise `@observe` overwrites your `update_current_span(output=...)`
#   with it when the function returns.
# - Serve the prompt **once per turn**, at the start, so every LLM call of the turn uses the same version.
# - `propagate_attributes(session_id=case.session_id, user_id=case.user_id, tags=[RUN_TAG, *case.tags], environment=ENV, ...)`.
# - Run `AGENT.invoke(case.message, config={"callbacks": [CallbackHandler()]}, system_prompt=system_text, llm_params=params)`.
# - Set the root's input/output (`langfuse.update_current_span(input=case.message, output=run.summary["final_answer"])`).
# - **Link the prompt to every generation.** Two ways work with LangGraph on SDK 5.2 (both verified live). Pick one:
#   1. add `prompt=prompt` to `propagate_attributes(...)` (framework-agnostic), or
#   2. add `"metadata": {"langfuse_prompt": prompt}` to the LangChain `config` (the CallbackHandler links it).
#
#   A fallback prompt is never linked (it has no version), which is exactly what you want.
#
# The cell after yours serves `production` (expect your good version), then a label that doesn't exist (expect the fallback).

# %%
# TODO 3: your code here

# %%
prompt, system_text, params = serve_prompt()
print(f"served v{prompt.version}  fallback={prompt.is_fallback}  params={params}")
assert system_text == SYSTEM_PROMPT and "{{" not in system_text
fb, fb_text, fb_params = serve_prompt(label="no-such-label")  # logs a 404 warning: that's the point
print(f"missing label -> fallback={fb.is_fallback} version={fb.version} params={fb_params}")
assert fb.is_fallback and fb_text == SYSTEM_PROMPT, "serve_prompt must fall back, not raise"

# %% [markdown]
# ## TODO 4: promote by label (and remember what you replaced)
#
# Write **`promote(version, label="production") -> int | None`**. It moves `label` to `version` and returns the version
# that held the label **before** (None if no version had it).
#
# Why return the previous version? **Langfuse keeps no label history** (verified live). Once you move `production`,
# nothing in Langfuse records where it was. Your deploy tooling has to remember it, or a rollback becomes guesswork.
#
# Also write **`label_table() -> dict[str, int]`**: print one row per version (version, labels, commit message, first
# routing line that mentions `login`) and return `{label: version}`.
#
# Hints:
# - `langfuse.api.prompts.get(PROMPT_NAME, label=label).version` is the current holder. It raises `NotFoundError`
#   (imported above) when no version has the label.
# - `langfuse.update_prompt(name=PROMPT_NAME, version=version, new_labels=[label])` adds the label to that version and
#   removes it from the version that had it. The version's other labels are kept. It also clears **this process's**
#   prompt cache for that name. Another process only sees the move after its TTL.
# - A version row has `.labels` and `.commit_message`.

# %%
# TODO 4: your code here

# %% [markdown]
# ## Run it: healthy → bad version promoted → rollback
# Three phases, 6 single-turn requests (about 18 Azure calls). The agent object is the same in all three. Only the
# label moves.
#
# `wait_until_served(version)` polls *your* `serve_prompt` until the agent really gets that version. In this process,
# `update_prompt` clears the cache so it's immediate; a server fleet would converge within one `CACHE_TTL`.

# %%
TRACES = []  # (phase, case, trace_id, served version, handoff)


def wait_until_served(version, label="production", timeout=3 * CACHE_TTL + 5):
    deadline = time.time() + timeout
    while (served := serve_prompt(label)[0].version) != version:
        if time.time() > deadline:
            raise TimeoutError(f"{label} still serves v{served}, expected v{version}")
        time.sleep(1)
    print(f"  {label} now serves v{version}")


def run_phase(phase):
    for case in [c for c in CASES if c.phase == phase]:
        run, trace_id, served = traced_turn(case)
        TRACES.append((phase, case, trace_id, served, run.summary["handoff_target"]))
        ok = "ok" if run.summary["handoff_target"] == case.expected_handoff else "WRONG"
        print(f"  {phase:<9} v{served}  {case.case_id:<22} -> {run.summary['handoff_target']:<19} "
              f"(expected {case.expected_handoff}) {ok}")


print("phase 1: healthy")
print("  production was on", promote(GOOD))
wait_until_served(GOOD)
run_phase("healthy")

# %% [markdown]
# ### Phase 2: a bad version is promoted
# A teammate's v2 "simplifies" the login rule. It is created on **`staging`** and promoted without a test that
# covers login. `promote` hands back what `production` pointed to before: keep it, it's your rollback.

# %%
BAD = ensure_version(WRONG_HANDOFF_TEMPLATE, labels=["staging"],
                     commit_message="v2: simplify routing rules (BAD: login -> billing)")
print("phase 2: promote the candidate")
PREVIOUS = promote(BAD)
print(f"  production moved v{PREVIOUS} -> v{BAD}  (remembered for the rollback)")
wait_until_served(BAD)
run_phase("bad")

# %% [markdown]
# ### Phase 3: roll back by moving the label
# Login tickets are landing in billing's queue. The fix is one API call, with no build and no deploy.

# %%
print("phase 3: rollback")
promote(PREVIOUS)
wait_until_served(PREVIOUS)
run_phase("rollback")
langfuse.flush()
print()
LABELS = label_table()

# %% [markdown]
# ## Which version was live? Ask Langfuse
# The prompt link is queryable. This given cell asks the **Metrics API** for this run's generations, grouped by prompt
# version: the same query powers the per-version charts on the prompt's **Metrics** tab. (Ingestion is asynchronous,
# so the cell waits until all 6 traces are in.)

# %%
def metrics(dimensions, filters=()):
    query = {"view": "observations", "dimensions": [{"field": d} for d in dimensions],
             "metrics": [{"measure": "count", "aggregation": "count"}, {"measure": "latency", "aggregation": "avg"}],
             "filters": [{"column": "tags", "operator": "any of", "value": [RUN_TAG], "type": "arrayOptions"},
                         {"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}, *filters],
             "fromTimestamp": (RUN_STARTED - timedelta(minutes=5)).isoformat(),
             "toTimestamp": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    return langfuse.api.metrics.metrics(query=json.dumps(query)).data


trace_ids = [t for _, _, t, _, _ in TRACES]
for _ in range(30):
    roots = {t for t in trace_ids if any(o.parent_observation_id is None for o in
                                         langfuse.api.observations.get_many(trace_id=t, fields="core", limit=100).data)}
    if len(roots) == len(trace_ids):
        break
    time.sleep(2)
time.sleep(4)  # let the last generations land
for row in metrics(["promptName", "promptVersion"]):
    print(f"  {row['promptName']} v{row['promptVersion']}: {row['count_count']} generation(s), "
          f"avg latency {row['avg_latency']:.0f} ms")

# %% [markdown]
# Verify waits for all 6 traces, then checks:
# - the good and the bad template each exist **once** as a version of `support-agent-system`, with the config,
# - `production` ended on the good version and `staging` on the bad candidate (module 09 will compare the two),
# - **every** generation is linked to a prompt version, and ran with that version's `temperature` / `max_tokens`
#   (on a GPT-5-family deployment the agent never sends `temperature`, which it rejects, and sends `max_tokens` as
#   `max_completion_tokens`, so that is what verify expects in the generation's model parameters),
# - the planted bad version is caught: the phase-2 traces are linked to **it** and at least one login was misrouted,
# - after the rollback, new traffic is linked to the good version and routed to `account_security`.

# %%
verify.check_module_04(RUN_TAG, expected_traces=6, agent_deployment=settings.agent_deployment)

# %% [markdown]
# ## Demo: the cache when a label moves *somewhere else*
# In this process, `update_prompt` cleared the cache, so every move was instant. Your other servers don't get that
# signal. This given cell moves `staging` with the **raw API** (as the UI or another service would) and watches a
# 5-second cache:
# - the old version is served until the TTL expires,
# - then **once more**, while a background refresh runs (stale-while-revalidate: a request never waits on Langfuse),
# - then the new version.
#
# It puts `staging` back on the bad candidate at the end. Module 09 needs it there.

# %%
def served_staging():
    return langfuse.get_prompt(PROMPT_NAME, label="staging", cache_ttl_seconds=5).version


langfuse.clear_prompt_cache()
print(f"t=0s   staging serves v{served_staging()}")
try:
    langfuse.api.prompt_version.update(PROMPT_NAME, GOOD, new_labels=["staging"])  # someone moves it in the UI
    for second in range(1, 9):
        time.sleep(1)
        print(f"t={second}s   staging serves v{served_staging()}")
finally:
    langfuse.update_prompt(name=PROMPT_NAME, version=BAD, new_labels=["staging"])
print("staging restored to the candidate v%s" % langfuse.api.prompts.get(PROMPT_NAME, label="staging").version)

# %% [markdown]
# ## UI tour
# - [ ] **Prompts → `support-agent-system`**: the versions, their labels, and commit messages. Open v2 and use
#   **Diff** against v1: one line changed, and that line is the outage.
# - [ ] The **config** panel of a version: `temperature` and `max_tokens` live with the text, versioned together.
# - [ ] Move a label in the UI (then move it back). The SDK picks the move up after its cache TTL.
# - [ ] Open a **phase:bad** trace and click a generation: the prompt link (name + version) is in the header. Click it to
#   jump to that exact version.
# - [ ] **Traces**: filter on tag `phase:bad`, or in **Observations** filter on *Prompt Name* / *Prompt Version*.
# - [ ] The prompt's **Metrics** tab: generations, latency, cost and scores per version. Once module 06/07 scores exist,
#   this is where a bad version shows up as a quality drop.
# - [ ] **Playground**: open v1 from the prompt page (*Playground* button), fill `company` / `max_sentences`, send
#   "I can't log in to my account cust_02." to your agent deployment (needs an LLM connection: Settings → LLM
#   Connections, which module 07 sets up; on a GPT-5-family deployment, switch temperature off in the model
#   parameters: it only accepts its default). Then open v2 side by side. The Playground runs the prompt text only,
#   without the agent's tools, so it's for wording; behaviour is tested with real traffic and experiments (module 09).
#
# ## Production notes
# - **Serve by label, never by version number**, and let the label be the deploy. A rollback is the same operation:
#   moving the label back. Remember the previous holder: Langfuse keeps no label history.
# - **Cache + fallback are not optional.** With caching, a Langfuse outage costs nothing for TTL seconds, and after that
#   the stale copy keeps being served while refreshes fail. The fallback covers a cold start with Langfuse down.
#   Ship the fallback **in code** (it's your last known-good prompt) and alert when `is_fallback` is served.
# - **Label moves propagate within one TTL**, not instantly: every process refreshes on its own schedule. Pick the TTL
#   as "how long may two versions be live at once" (60s default). Use `cache_ttl_seconds=0` only in tests.
# - **Get-or-create by content in deploy scripts**: Langfuse adds a version on every `create_prompt`, even if nothing
#   changed.
# - **Compile strictly and validate the config.** A missing variable or a bad `temperature` should fail the request
#   in your code with a clear error, not reach the model.
# - **Protect `production`.** On Langfuse Cloud / Enterprise, *protected labels* restrict who can move it. On OSS,
#   restrict who has project write access, and gate promotions on an experiment (module 09) or a CI check (module 10).
# - **Prompt webhooks** (module 12) can announce every version and label change in Slack, so a promotion is never silent.
# - **Why it matters (leadership):** prompt changes ship in minutes without a code deploy, and roll back just as fast,
#   with every trace showing which version was live.
