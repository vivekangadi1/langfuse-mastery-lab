# %% [markdown]
# # Module 03: Cost & tokens (model definitions, usage, cost per session/user/release)
#
# **Goal:** answer **"what does each conversation cost us?"** before finance asks. That means:
# - every LLM call carries its **usage** (input, cached input, output tokens),
# - Langfuse turns usage into **USD** with a **model definition** you control,
# - you can roll cost up per **trace, session, user, release and model**, and catch a cost regression the day it ships.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **Usage details** | token counts per type on a generation: `input`, `input_cache_read`, `output`, `total` | the CallbackHandler copies them from the Azure response |
# | **Model definition** | `match_pattern` (regex on the generation's **reported** model name) + USD **per token** for each usage type | one per deployment: `azure-gpt-4.1-mini`, `azure-gpt-5.2-chat` (one deployment: just `azure-gpt-5.4`) |
# | **Built-in vs custom** | Langfuse ships ~190 built-in definitions. A **custom** one wins over a built-in. | your Azure prices, not OpenAI list prices |
# | **Unmatched model** | no definition matches → usage is kept but **cost is empty** (not 0, *missing*) | any deployment the probe shows unpriced |
# | **Cost at ingestion** | cost is computed when the generation is ingested. A new definition does **not** reprice old traces. | define models **before** traffic |
# | **Metrics API** | server-side aggregation (sum/avg/p95 of cost, tokens, latency), grouped and filtered | cost by version, by model; per session / user via filters |
#
# ```
# AzureChatOpenAI response ──► CallbackHandler ──► generation
#   model = "gpt-chat-latest"                       model         = "gpt-chat-latest"
#   usage: prompt 4421 (4224 cached), completion 5  usage_details = {input: 197, input_cache_read: 4224, output: 5}
#                                                         │  match_pattern  (?i)^(gpt-5\.2-chat|gpt-chat-latest)$
#                                                         ▼
#                               model definition  azure-gpt-5.2-chat  {input: 1.75e-6, input_cache_read: 1.75e-7, output: 1.4e-5}
#                                                         ▼
#                               cost_details = {input: 0.000345, input_cache_read: 0.000739, output: 0.00007}  total_cost
#                                                         ▼
#                 trace ─► session ─► user ─► version (release) ─► dashboard / Metrics API / alerts (module 12)
# ```
#
# **The planted failure, `cost_spike`:** release **v2** of the agent "accidentally" ships the **expensive deployment** and
# a **padded system prompt** (about 4,000 extra tokens per call). Nothing breaks and every answer is fine, so only cost tracking can catch it.
#
# **Running on one deployment** (`AZURE_OPENAI_DEPLOYMENT` = `EVAL_JUDGE_MODEL`, e.g. a single `gpt-5.4`): there is no
# pricier model to switch to, and a padded prompt alone barely shows, because Azure **caches** a repeated prompt prefix
# and bills cached tokens at a tenth. So v2 ships a bigger appendix (about 17,600 tokens) behind a `Request time: …`
# line at the **top** of the system prompt. That line changes on every call, so nothing after it is ever cached: the
# same lesson (only cost tracking sees it), with a real-world cause (a timestamp at the top of a prompt silently
# disables prompt caching). The setup cell prints which variant you run.
#
# Docs: https://langfuse.com/docs/observability/features/token-and-cost-tracking ·
# https://langfuse.com/docs/metrics/features/metrics-api ·
# https://api.reference.langfuse.com/#tag/models

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import importlib
import json
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from langfuse_lab import verify, traffic, kb, pricing
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent, make_llm
from langfuse_lab.failures import FailureConfig
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_mastery import cost

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
RUN_TAG = f"run:m03-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
RUN_STARTED = datetime.now(timezone.utc)
ENV = "dev"
fixtures = load_fixtures()
CONVERSATIONS = traffic.cost_demo_conversations(run_id=RUN_ID, env=ENV)
MODEL_TABLE = pricing.definitions(settings)  # TODO 2: one model definition per DISTINCT deployment
print("run tag:", RUN_TAG, "| agent deployment:", settings.agent_deployment,
      "| expensive deployment:", settings.expensive_deployment)
print("cost_spike variant:", "ONE deployment: bigger padded prompt behind a cache-busting timestamp"
      if settings.single_model else "expensive deployment + padded prompt")
for spec in CONVERSATIONS:
    print(f"  {spec.session_id:<40} {spec.user_id}  release={spec.release}  turns={len(spec.turns)}  {spec.failures or ''}")

# %% [markdown]
# ## Given: the instrumentation you built in modules 01–02
# This is a recap, not a TODO. Every turn is one `support-request` trace carrying session, user, tags and
# **`version=spec.release`**. That version is what "cost per release" groups by. Conversations with the `cost_spike`
# failure run on the planted agent: the expensive deployment plus the padded prompt (one deployment: the bigger,
# cache-busting padded prompt on the same model).

# %%
from langfuse import get_client, observe, propagate_attributes
from langfuse.langchain import CallbackHandler

langfuse = get_client()


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


@observe(name="support-request", as_type="agent", capture_input=False, capture_output=False)
def traced_turn(message, agent, spec, history=None):
    """One turn = one trace. Returns (AgentRun, trace_id)."""
    with propagate_attributes(session_id=spec.session_id, user_id=spec.user_id, tags=[RUN_TAG, *spec.tags],
                              environment=ENV, version=spec.release):
        run = agent.invoke(message, history=history or [], config={"callbacks": [CallbackHandler()]})
        langfuse.update_current_span(input=message, output=run.summary["final_answer"])
        return run, langfuse.get_current_trace_id()


AGENTS = {
    (): SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search),
    ("cost_spike",): SupportAgent(failures=FailureConfig.from_names(["cost_spike"]),  # see "The planted failure"
                                  guardrail_fn=observed_guardrail, search_fn=observed_search),
}


def run_conversation(spec) -> list[str]:
    """All turns of one conversation (one trace per turn, shared session). Returns the trace ids."""
    history, trace_ids = [], []
    for message in spec.turns:
        run, trace_id = traced_turn(message, AGENTS[spec.failures], spec, history=history)
        history = run.messages
        trace_ids.append(trace_id)
    return trace_ids


def wait_for_ingestion(trace_ids, *, timeout=120, settle=4):
    """Ingestion is async. Wait until every trace's root has arrived and no trace gains observations for `settle` s.
    Query costs only after this: a number read mid-ingestion is silently too low."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        seen = {}
        for t in trace_ids:
            rows = langfuse.api.observations.get_many(trace_id=t, fields="core", limit=200).data
            seen[t] = (any(o.parent_observation_id is None for o in rows), len(rows))
        if all(has_root for has_root, _ in seen.values()) and seen == last:
            print(f"ingested: {len(trace_ids)} traces, {sum(n for _, n in seen.values())} observations")
            return
        last = seen
        time.sleep(settle)
    print("warning: ingestion still settling; cost numbers may be low (re-run the cost cells)")


def generations(trace_id, *, wait=30):
    """The GENERATION observations of one trace, with the `usage` and `model` field groups (waits for ingestion)."""
    deadline = time.time() + wait
    while True:
        rows = langfuse.api.observations.get_many(trace_id=trace_id, fields="core,basic,usage,model", limit=100).data
        gens = [o for o in rows if o.type == "GENERATION"]
        if gens or time.time() > deadline:
            return gens
        time.sleep(2)

# %% [markdown]
# ## Demo: what model name do the generations *report*?
# A `match_pattern` is matched against the **model name on the generation**. That's not necessarily your Azure
# deployment name. This cell makes one tiny call per deployment (2 Azure calls, 1 with a single deployment; tagged
# `…-probe` so verify ignores them) and prints what Langfuse stored.
#
# Read the output carefully. These names are what TODO 2 has to match. A deployment reports either
# - a **dated model id** (`gpt-4.1-mini-2025-04-14`), which a **built-in** definition may already price, or
# - an **alias** (`gpt-5.2-chat` reports `gpt-chat-latest`) that no built-in knows: its usage is there, and its cost
#   is **missing**.
#
# (If you re-run this module after TODO 2, every probe shows a cost: your definitions exist by then.)

# %%
PROBE_TAG = f"{RUN_TAG}-probe"


def probe(llm, label):
    with langfuse.start_as_current_observation(name=f"m03-probe-{label}", as_type="span"):
        with propagate_attributes(tags=[PROBE_TAG], environment=ENV):
            llm.invoke("Say hi in three words.", config={"callbacks": [CallbackHandler()]})
        return langfuse.get_current_trace_id()


DEPLOYMENTS = {row["label"]: row["deployment"] for row in MODEL_TABLE}  # one entry with a single deployment
probe_traces = {label: probe(make_llm(settings, expensive=label == "expensive"), label) for label in DEPLOYMENTS}
langfuse.flush()



def matches(pattern, name):
    try:
        return bool(re.search(pattern, name or ""))
    except re.error:  # a few built-in patterns use regex syntax Python's `re` doesn't support
        return False


first_page = langfuse.api.models.list(page=1, limit=100)
all_models = first_page.data + [m for page in range(2, first_page.meta.total_pages + 1)
                                for m in langfuse.api.models.list(page=page, limit=100).data]
builtins = [m for m in all_models if m.is_langfuse_managed]
print(f"{len(builtins)} built-in model definitions on this server")
REPORTED = {}
for label, trace_id in probe_traces.items():
    g = generations(trace_id)[0]
    REPORTED[label] = g.model
    builtin = next((m.model_name for m in builtins if matches(m.match_pattern, g.model)), None)
    print(f"{label:<9} deployment={DEPLOYMENTS[label]:<14} reported model={g.model!r}")
    print(f"          usage={g.usage_details}")
    print(f"          total_cost={g.total_cost}  priced by={g.internal_model_id or '(nothing: no definition matches)'}"
          f"  built-in that matches={builtin}")

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/cost.py` (pure functions + pytest)
#
# Open `ch09/langfuse_mastery/cost.py` and implement:
# - `per_token(prices_per_million)`: vendor pages quote USD per **1M** tokens, and Langfuse wants USD per **token**.
#   An off-by-a-million price is the most common cost bug.
# - `cost_from_usage(usage, prices)`: the arithmetic Langfuse runs at ingestion. You'll use it to reconcile one
#   generation by hand.
# - `over_budget(costs, budget)` and `cost_spikes(costs, factor=5.0)`: the budget logic for TODO 4.
#
# There's no Langfuse code in that file. The cell below runs its tests and reloads your module. Re-run it until it
# shows `23 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_cost.py", "-q",
                         "-p", "no:cacheprovider"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
cost = importlib.reload(cost)

# %% [markdown]
# ## TODO 2: model definitions for your Azure deployments
#
# Write `ensure_model_definitions() -> dict[str, Model]`. It should get-or-create one **custom** definition per row
# of `MODEL_TABLE` (printed below: `label`, `deployment`, `model_name`, `prices_per_m`), by `model_name`. With this
# course's two deployments that is:
#
# | model_name | `match_pattern` must match | USD per **1M** tokens: input / cached input / output |
# |---|---|---|
# | `azure-gpt-4.1-mini` | `REPORTED["agent"]` **and** the bare deployment name `gpt-4.1-mini` | 0.40 / 0.10 / 1.60 |
# | `azure-gpt-5.2-chat` | `REPORTED["expensive"]` **and** the deployment name `gpt-5.2-chat` | 1.75 / 0.175 / 14.00 (reasoning 14.00) |
#
# With **one deployment** for everything (e.g. `gpt-5.4` as agent and judge), `MODEL_TABLE` has a single row,
# `azure-gpt-5.4`, whose pattern must match `REPORTED["agent"]` and `gpt-5.4`.
#
# (These are list prices at the time of writing, from `langfuse_lab/pricing.py`. In production, use the prices from
# your Azure agreement. A deployment that isn't in that table raises a `KeyError`: add its prices there.)
#
# Why also the deployment names? A **failed** call has no response, so the generation records the *requested* model
# (the deployment name) instead. Match both, and every call your app makes is priced.
#
# Requirements:
# - `unit="TOKENS"` and a single default pricing tier:
#   `pricing_tiers=[{"name": "Standard", "isDefault": True, "priority": 0, "conditions": [], "prices": {...}}]`.
#   The price keys are the **usage types** in the row's `prices_per_m`: `input`, `input_cache_read`, `output`, and for
#   GPT-5 models `output_reasoning`. Convert with `cost.per_token`. Leave out `input_cache_read`, and cached tokens
#   cost **0**. The same goes for `output_reasoning`: GPT-5 bills reasoning tokens as output, but Langfuse records
#   them as their own usage type, so without a price they are free in your numbers.
# - **Anchor and escape** the regex: `(?i)^(gpt-4\.1-mini|...)$`. An unanchored `gpt-4.1-mini` would also match
#   `gpt-4.1-mini-realtime`.
# - **Get-or-create:** names are not unique (like score configs in module 06), so list first and create only what's
#   missing. Skip built-ins (`is_langfuse_managed=True`).
# - Return `{model_name: model}`.
#
# Hints:
# - `langfuse.api.models.list(page=, limit=100)` (`.data`, `.meta.total_pages`; there are ~190 built-ins, so paginate)
# - `langfuse.api.models.create(model_name=, match_pattern=, unit=, pricing_tiers=)`
# - the model object has `.id`, `.model_name`, `.match_pattern`, `.is_langfuse_managed`, `.prices`
#
# The cell after yours runs it **twice**, checks that nothing new was created, and tests your patterns against the
# reported names.

# %%
for row in MODEL_TABLE:
    print(f"{row['label']:<9} deployment={row['deployment']:<14} reported={REPORTED[row['label']]!r:<28} "
          f"-> {row['model_name']}  USD per 1M tokens: {row['prices_per_m']}")

# %%
# TODO 2: your code here

# %%
first, second = ensure_model_definitions(), ensure_model_definitions()
assert {k: m.id for k, m in first.items()} == {k: m.id for k, m in second.items()}, "second call created new definitions"
MODELS = second
for row in MODEL_TABLE:
    name, m = row["model_name"], MODELS[row["model_name"]]
    for reported in (REPORTED[row["label"]], row["deployment"]):
        assert re.search(m.match_pattern, reported), f"{name}: {m.match_pattern!r} does not match {reported!r}"
    print(f"{name:<20} {m.match_pattern:<45} {({k: p.price for k, p in m.prices.items()})}  {m.id}")

# %% [markdown]
# ## Run it, including the planted cost spike
# The definitions exist now, so every generation from here on is priced **at ingestion**. (That's why this cell comes
# after TODO 2: cost is not computed retroactively.)
#
# There are 4 conversations and 7 turns, about 21 Azure calls. The `s_login_then_2fa` conversation runs as **release
# v2**, on the expensive deployment with the padded prompt (one deployment: the same model, with the bigger padded
# prompt behind the per-call timestamp).

# %%
TRACES = {}  # session_id -> [trace ids]
for spec in CONVERSATIONS:
    TRACES[spec.session_id] = run_conversation(spec)
    print(f"{spec.session_id:<40} release={spec.release}  {len(TRACES[spec.session_id])} trace(s)")
langfuse.flush()
wait_for_ingestion([t for ids in TRACES.values() for t in ids])

# %% [markdown]
# ## Reconcile one generation by hand
# This cell picks a generation of the v2 session, preferring one that hit Azure's **prompt cache** (prompts of 1024+
# tokens are cached, and the padded prompt is about 4,400). Your `cost.cost_from_usage` with **your** prices should
# reproduce the number Langfuse stored. On a cached call, `input` holds only the **uncached** part, and
# `input_cache_read` is billed at a tenth of the input price.
#
# With one deployment, no v2 call hits the cache: the timestamp on the first line makes every prompt new, so all
# ~17,600 padded tokens are billed as `input`. That is the whole spike.

# %%
spike_session = next(s.session_id for s in CONVERSATIONS if s.failures)
spike_gens = [g for t in TRACES[spike_session] for g in generations(t)]
g = max(spike_gens, key=lambda o: ((o.usage_details or {}).get("input_cache_read") or 0, o.total_cost or 0))
prices = {k: p.price for k, p in MODELS[pricing.definition_name(settings.expensive_deployment)].prices.items()}
mine = cost.cost_from_usage(g.usage_details, prices)
print("model       ", g.model, "->", g.internal_model_id)
print("usage       ", g.usage_details)
print("cost_details", g.cost_details)
print(f"Langfuse total_cost={g.total_cost:.8f}   your cost_from_usage={mine:.8f}")
assert abs(mine - g.total_cost) < 1e-9 + 1e-6 * g.total_cost, "your arithmetic differs from Langfuse's"

# %% [markdown]
# ## TODO 3: cost per session and per user (Metrics API)
#
# Write three functions.
#
# **`metrics_cost(filters=(), dimensions=()) -> list[dict]`** wraps the Metrics API:
# - `langfuse.api.metrics.metrics(query=json.dumps({...}))` with `"view": "observations"`,
#   `"metrics": [{"measure": "totalCost", "aggregation": "sum"}, {"measure": "totalTokens", "aggregation": "sum"}]`,
#   `"dimensions": [{"field": d} for d in dimensions]`, `"fromTimestamp"` / `"toTimestamp"` (ISO strings: from
#   `RUN_STARTED - 5 min` to now + 5 min).
# - **Always** scope it to this run. Prepend
#   `{"column": "tags", "operator": "any of", "value": [RUN_TAG], "type": "arrayOptions"}` to `filters`.
#   Without that filter, `cust_02`'s cost would include every module that ever used `cust_02`.
# - Return `.data`, which is a list of dicts like `{"sum_totalCost": 0.0123, "sum_totalTokens": 9000}` (plus one key
#   per dimension).
#
# **`cost_per_session(session_ids) -> dict[str, float]`** and **`cost_per_user(user_ids) -> dict[str, float]`:**
# - `sessionId` and `userId` are **high-cardinality**: the Metrics API refuses to *group* by them (HTTP 400). They
#   work as **filters**: `{"column": "sessionId", "operator": "=", "value": sid, "type": "string"}`.
#   So run one query per id.
# - An empty match returns `[{"sum_totalCost": None}]`. Treat None as 0.0.
#
# Why the Metrics API instead of summing observations yourself? It aggregates on the server, it's real-time, and the
# same query powers dashboards (module 11) and cost alerts (module 12).

# %%
# TODO 3: your code here

# %%
SESSION_COSTS = cost_per_session([s.session_id for s in CONVERSATIONS])
USER_COSTS = cost_per_user(sorted({s.user_id for s in CONVERSATIONS}))
print("per session:", {k: round(v, 5) for k, v in SESSION_COSTS.items()})
print("per user:   ", {k: round(v, 5) for k, v in USER_COSTS.items()})

# %% [markdown]
# ## TODO 4: the cost report (by release, by model, flagged sessions)
#
# Write `cost_report(session_costs, *, budget=SESSION_BUDGET) -> list[str]`, which prints three tables and returns the
# **flagged** session ids:
# 1. **Cost by release:** `metrics_cost(dimensions=["version"])`. One row per version, with cost and tokens.
# 2. **Cost by model:** `metrics_cost(filters=[GENERATION only], dimensions=["providedModelName"])`. The filter is
#    `{"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}`.
# 3. **Sessions:** each session's cost, most expensive first, marking the ones you flag.
#
# Flag a session if it is **over budget** (`cost.over_budget(session_costs, budget)`) **or** a **spike** relative to its
# peers (`cost.cost_spikes(session_costs)`). Return the flagged ids, deduplicated and most expensive first.
#
# Expected: release `v2` costs many times `v1` despite fewer turns, the expensive model dominates the model table
# (one deployment: the table has one row; the spike shows up per release and per session), and exactly one session
# is flagged: the v2 login conversation.
#
# The budget is 0.01 USD for a `gpt-4.1-mini` agent. `pricing.scaled_budget` scales it to what a typical call costs on
# YOUR agent deployment (0.071 on `gpt-5.4`): a budget written for a cheap model would flag every clean conversation
# of a 7x pricier one.

# %%
SESSION_BUDGET = pricing.scaled_budget(0.01, settings.agent_deployment)  # USD per conversation: what finance signed off on
print("session budget: $", SESSION_BUDGET)

# TODO 4: your code here

# %%
FLAGGED = cost_report(SESSION_COSTS)
print("flagged:", FLAGGED)

# %% [markdown]
# Verify waits for all 7 traces, then checks:
# - your definitions (one per row of `MODEL_TABLE`) exist exactly once, priced per token, cached input included,
# - **every** generation of the run was priced by *your* definitions (not unmatched, not a built-in),
# - usage was captured, and any cached (and GPT-5 reasoning) tokens were priced,
# - your per-session and per-user numbers match what Langfuse stored (within 1%),
# - the planted `cost_spike` traces cost at least 5× the clean median, and your report flagged exactly that session.

# %%
verify.check_module_03(RUN_TAG, session_costs=SESSION_COSTS, user_costs=USER_COSTS, flagged_sessions=FLAGGED,
                       expected_traces=7, model_defs=[row["model_name"] for row in MODEL_TABLE])

# %% [markdown]
# ## Demo: cost is not retroactive
# The probe calls ran **before** your definitions existed. Read them again: a probe that was unpriced is still
# unpriced, and one a *built-in* priced still carries the built-in's price, even though your definitions now match
# them. Langfuse prices a generation once, when it's ingested.
#
# (On a re-run of this module, your definitions already existed when the probes ran, so every probe is priced by yours.)

# %%
for label, trace_id in probe_traces.items():
    g = generations(trace_id)[0]
    mine = {m.id: name for name, m in MODELS.items()}
    builtin = {m.id: m.model_name for m in builtins}
    by = (f"your {mine[g.internal_model_id]}" if g.internal_model_id in mine
          else f"built-in {builtin[g.internal_model_id]}" if g.internal_model_id in builtin else "nothing")
    print(f"{label:<9} model={g.model!r:<28} total_cost={g.total_cost}  priced by: {by}")

# %% [markdown]
# ## UI tour
# - [ ] **Settings → Models**: your definitions sit above the built-ins. Open one to see its match pattern and prices.
#   Use the built-in **test** box to try a model name against the pattern.
# - [ ] Open a v2 trace and click a **generation**: usage (input / cached / output), cost breakdown, and the matched model.
# - [ ] **Traces** table: add the **Cost** and **Tokens** columns, filter on tag `cost_spike`, and sort by cost.
# - [ ] **Sessions** and **Users** tables: total cost per conversation and per customer.
# - [ ] **Dashboards → Langfuse default (cost)**: cost by model over time. The spike is one bar.
# - [ ] Find a probe trace whose generation has tokens and **no cost** (the probe cell said which, if any). That's
#   what an unmatched model looks like in production.
# - [ ] (One deployment) Open a v2 generation: its input starts with `Request time: …`, and `input_cache_read` is 0 on
#   every v2 call.
#
# ## Production notes
# - **Match what the generation reports, not what you deployed.** Azure returns a dated id (`gpt-4.1-mini-2025-04-14`)
#   or an alias (`gpt-chat-latest`), and the alias can point at a new model tomorrow. Re-run the probe after every
#   model change.
# - **Watch for unmatched models.** Run a weekly Metrics query for generations with tokens and zero cost, grouped by
#   `providedModelName`. Each row is spend you can't see.
# - **Define models before traffic, as code** (like `ensure_model_definitions`, run at deploy time). Cost is computed
#   at ingestion, and old traces are **not** repriced. If you need invoice-exact numbers, ingest the cost yourself
#   (`cost_details` on the generation); ingested cost wins over inferred cost.
# - **Price every usage type you're billed for:** cached input, reasoning tokens, audio. A missing price is silently 0.
# - **Keep what changes per call at the END of a prompt.** Prompt caching matches identical leading tokens: a
#   timestamp, request id or user name on the first line makes every call a cache miss, at ten times the price.
#   Pricing **tiers** (`conditions` on usage thresholds) model long-context surcharges.
# - **Provisioned throughput (PTU)** has no per-token price. Model it as an allocation (cost per token = monthly
#   commitment / tokens used) or track tokens only.
# - **Budgets belong in alerts**, not notebooks: the same Metrics query, per session or per release, feeds the
#   alerts in module 12.
# - **Why it matters (leadership):** we can say what each conversation costs, per customer and per release, and catch
#   a cost regression the day it ships, not when the invoice arrives.
