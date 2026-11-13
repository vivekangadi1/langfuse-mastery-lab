# %% [markdown]
# # Module 07: Live evaluation (LLM-as-a-judge on live traffic)
#
# **Goal:** quality scores that Langfuse produces **by itself**, on every matching trace, as it is ingested. No scoring
# code runs in your request path: the langfuse **worker** container reads each new observation, decides whether a rule
# matches, calls a judge model, and writes the score.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **LLM connection** | provider credentials stored in Langfuse (upserted by `provider` name); the *worker* calls it | Azure OpenAI, deployments `gpt-5.2-chat` + `gpt-4.1-mini` (or your one deployment) |
# | **Evaluator** | the *how*: judge prompt with `{{variables}}`, structured output (BOOLEAN / NUMERIC / CATEGORICAL), model. Versioned | `routing_judge` (BOOLEAN), `helpfulness_judge` (NUMERIC 0..1) |
# | **Evaluation rule** | the *which*: filters on **observations**, a sampling rate, and the evaluators to run | root `support-request` spans of traces tagged `live-eval` |
# | **Variable mapping** | fills each prompt variable from the observation's `input`, `output`, `metadata` (+ JSONPath) or `tool_calls` | `handoff` ← `metadata` `$.handoff_target` |
# | **Judge score** | `source=EVAL`, named after the evaluator, on the matched observation; `comment` = the judge's reasoning | `routing_judge=False` "policy says account_security, got billing" |
# | **Code evaluator** | the same, but a TypeScript/Python `evaluate(ctx)` instead of an LLM | needs a code-eval dispatcher: ⚠️ disabled on this deployment |
#
# ```
# your app ──trace──► Langfuse ingestion ──► worker: rule filter matches? ──► judge LLM (Azure, via the LLM connection)
#                                                 │ no: nothing happens            │
#                                                 ▼                                ▼
#                                       (untagged traffic, child spans)   score on the root observation
#                                                                         + an "Execute evaluator" trace in the
#                                                                           langfuse-llm-as-a-judge environment
# ```
#
# **Why a judge, when module 06 already has `handoff_correct`?** The code score needs the *expected* handoff, which
# only exists for test cases. Real traffic has no answer key. A judge applies the *policy* to any request, so it
# scales to production traffic. The price: a judge can be wrong. So this module also **calibrates** the judge against
# the code score on cases where both exist.
#
# Things verified live on this server (4.77) that shape the TODOs:
# - Evaluator create/update takes ~15 s (the server test-calls the model). A create that **times out on the client is
#   still created**, so get-or-create by name is not optional.
# - Rules are **not retroactive**: create them *before* the traffic.
# - A `traceName` filter silently never matches our traces. Filter on the root observation's **`name`**.
#
# Docs: https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge ·
# https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators ·
# https://langfuse.com/docs/administration/llm-connection

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
from langfuse_mastery import judging, scoring

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
RUN_TAG = f"run:m07-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
ENV = "dev"
fixtures = load_fixtures()

# Evaluators, rules and LLM connections are PROJECT-GLOBAL and long-lived (unlike traces). Keep PREFIX empty.
PREFIX = ""
NAMES = {role: PREFIX + base for role, base in verify.LIVE_NAMES.items()}
LIVE_TAG = PREFIX + "live-eval"      # traffic opts in to live evaluation with this tag
ROOT_NAME = verify.LIVE_ROOT_NAME    # "support-request": the root observation the rule judges
JUDGE_MODEL = settings.expensive_deployment  # EVAL_JUDGE_MODEL: gpt-5.2-chat, or the agent's own deployment

CASES = traffic.live_eval_cases(run_id=RUN_ID)
print("run tag:", RUN_TAG, "| judge:", JUDGE_MODEL)
for role, name in NAMES.items():
    print(f"  {role:<12} {name}")
for c in CASES:
    judged = "judged" if c.case_id not in traffic.UNJUDGED_CASES else "NOT opted in"
    print(f"  {c.case_id:<36} expect handoff={c.expected_handoff:<19} {judged:<13} {c.failures or ''}")

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/judging.py` (pure functions + pytest)
#
# Open `ch09/langfuse_mastery/judging.py` and implement:
# - `prompt_variables(template)`, `variable_mapping(spec)`, `check_mapping(template, mapping)`: build the judge's
#   variable mapping and catch what the server would reject.
# - `rule_filters(live_tag, root_name)`: the rule's three filters, each with the one filter type its column accepts.
# - `as_verdict(value)`, `agreement(pairs)`: turn judge/code scores into a calibration report.
#
# The cell runs the tests and reloads your module. Re-run it until it shows `30 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_judging.py", "-q", "-p", "no:cacheprovider"],
                        cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
judging = importlib.reload(judging)

# %% [markdown]
# ## Given: the instrumentation (modules 01–02) and the code score (module 06)
# This is a recap, not a TODO. Two things are new:
# - The root span writes **`metadata={"handoff_target": …}`**. The judge can only see what is *on the observation*,
#   so the app must put the facts the judge needs there. Here: which team the agent handed off to.
# - Opted-in traffic carries the **`LIVE_TAG`** tag. That is what the rule filters on.
#
# The code score is module 06's: the `handoff_correct` BOOLEAN config (get-or-create, skipping archived configs) and
# `send_score`, which validates with **your `scoring.validate_score`** (module 06 TODO 2) and stamps
# `metadata.evaluator`. If `scoring` still raises `NotImplementedError`, finish module 06 TODO 2 first.

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


@observe(name=ROOT_NAME, as_type="agent", capture_input=False, capture_output=False)
def traced_turn(message, agent, *, session_id, user_id, tags):
    """One turn = one trace. Returns (AgentRun, trace_id)."""
    with propagate_attributes(session_id=session_id, user_id=user_id, tags=[RUN_TAG, *tags], environment=ENV):
        run = agent.invoke(message, config={"callbacks": [CallbackHandler()]})
        langfuse.update_current_span(input=message, output=run.summary["final_answer"],
                                     metadata={"handoff_target": run.summary["handoff_target"]})
        return run, langfuse.get_current_trace_id()


def ensure_code_score_config():
    """Module 06's `handoff_correct` config: get-or-create by name, skipping archived configs."""
    name = NAMES["code_score"]
    for c in langfuse.api.score_configs.get(page=1, limit=100).data:
        if c.name == name and not c.is_archived:
            return {name: c.dict()}
    created = langfuse.api.score_configs.create(name=name, data_type="BOOLEAN",
                                                description="agent handed off to the expected team (code score)")
    return {name: created.dict()}


def send_score(configs, name, value, *, comment=None, evaluator="code:v1", score_id=None, **target):
    """Module 06 TODO 3: validate first (the server drops invalid scores silently), then send with provenance."""
    cfg = configs[name]
    ok, reason = scoring.validate_score(cfg, value)
    if not ok:
        raise ValueError(f"{name}={value!r}: {reason}")
    if cfg["dataType"] == "BOOLEAN":
        value = 1.0 if value else 0.0
    langfuse.create_score(name=name, value=value, data_type=cfg["dataType"], config_id=cfg["id"], comment=comment,
                          score_id=score_id, metadata={"evaluator": evaluator}, **target)


CONFIGS = ensure_code_score_config()
clean_agent = SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)
planted_agent = SupportAgent(failures=FailureConfig.from_names(["wrong_handoff"]),  # bad prompt: login -> billing
                             guardrail_fn=observed_guardrail, search_fn=observed_search)
print("code score config:", {k: v["id"] for k, v in CONFIGS.items()})

# %% [markdown]
# ## TODO 2: the LLM connection the worker calls
#
# Write `ensure_llm_connection()`. It upserts an Azure OpenAI connection and returns it.
# - `langfuse.api.llm_connections.upsert(provider=NAMES["connection"], adapter="azure", secret_key=…, base_url=…,
#   custom_models=[…], with_default_models=False)`
# - **`provider`** is the connection's unique key inside the project. Upsert on the same provider updates it in place,
#   so this call is naturally idempotent (unlike evaluators, below).
# - **`base_url`** is your Azure resource root plus `/openai`, e.g.
#   `https://<resource>.cognitiveservices.azure.com/openai`. `settings.azure_endpoint` may carry a longer path
#   (`/openai/deployments/…?api-version=…`): keep only scheme + host (`urllib.parse.urlsplit`).
# - **`custom_models`**: Azure *deployment names*, both `settings.expensive_deployment` (the judge) and
#   `settings.agent_deployment`, each listed once (with a single deployment for everything they are the same name).
# - The secret is stored encrypted and never read back. `list()` only shows `display_secret_key` (`...Z02w`).
#
# Who calls this URL? Not your notebook: the **langfuse worker container**. If judge scores never arrive later, the
# first place to look is `docker logs --tail 50 ch09_langfuse_worker`.

# %%
# TODO 2: your code here

# %%
connection = ensure_llm_connection()
assert ensure_llm_connection().id == connection.id, "upsert on the same provider must not create a second connection"
print(connection.provider, connection.adapter, connection.base_url, connection.custom_models, connection.display_secret_key)

# %% [markdown]
# ## TODO 3: evaluators, defined once and versioned on change
#
# Write `ensure_evaluator(name, *, prompt, mapping, output_definition, model)` that returns the evaluator
# (`langfuse.api.evaluators.get(id)`):
# 1. `judging.check_mapping(prompt, mapping)` first (fail fast, locally).
# 2. List **all** evaluators (`langfuse.api.evaluators.list(limit=100, cursor=…)`; follow `.meta.cursor` until it is
#    None) and pick the ones with this `name`. More than one: raise (someone created duplicates).
# 3. None found: **create** it.
#    `langfuse.api.evaluators.create(request=CreateEvaluatorRequest_LlmAsJudge(name=…, prompt=…,
#    model_config_=EvaluatorModelConfig(provider=NAMES["connection"], model=model), variable_mapping=mapping,
#    output_definition=output_definition), request_options={"timeout_in_seconds": 90})`
# 4. Found, but its prompt, mapping, output definition or model differ from what you asked for: **update** it with
#    `langfuse.api.evaluators.update(id, request=UpdateLlmAsJudgeEvaluatorRequest(prompt=…, model_config_=…,
#    variable_mapping=…, output_definition=…), request_options=…)`. That creates **version 2**, and rules pick up the
#    latest version automatically.
# 5. Found and identical: change nothing (no new version).
#
# Traps (all verified live):
# - The keyword is **`model_config_`** with a trailing underscore. `model_config=` is silently ignored by the SDK and
#   the server answers `400 Unrecognized key: "model_config"`.
# - Create/update take ~15 s, longer than the SDK's default timeout: pass `request_options`. A create that times out
#   on the client **still exists on the server**. Listing first (step 2) is what keeps a retry from duplicating it.
# - Comparing with what's stored: an existing evaluator has `.prompt` as a list of chat messages
#   (`.prompt[0].content`), `.variable_mapping` as objects (compare `m.dict(exclude_none=True)` with your dicts),
#   `.output_definition` and `.model_config_` (compare their `.dict(exclude_none=True)`).
#
# The prompts and mappings are given below. Read them: a judge is only as good as what it's told, and what it can see.
# (Running on one deployment, the judge is the agent's own model, so it can share the agent's blind spots. The
# calibration in TODO 5 against the code score is how you find out; in production, prefer a judge from another model.)
# (A first draft said just "refund -> billing_specialist". During validation the judge then flagged the plain
# "What is your refund policy?" question as mis-routed, a false alarm that calibration (TODO 5) exposed. The wording
# below fixes that. Change the prompt and re-run `ensure_evaluator`, and you get version 2.)

# %%
from langfuse.api.evaluators import (CreateEvaluatorRequest_LlmAsJudge, UpdateLlmAsJudgeEvaluatorRequest,
                                     EvaluatorModelConfig)
from langfuse.api.evaluation_commons import EvaluatorOutputDefinition_Boolean, EvaluatorOutputDefinition_Numeric

ROUTING_PROMPT = """You audit the routing decisions of Acme's customer-support agent.
Routing policy (the only source of truth):
- a problem with a specific charge (double charge, refund of a charge, invoice) -> billing_specialist
- login, password, account access -> account_security
- lost or delayed shipment -> shipping_specialist
- anything the agent can fully answer itself (order status, general questions such as the refund or
  returns policy) -> none

Customer message: {{input}}
Team the agent handed off to ("none" = no handoff): {{handoff}}
Agent's reply to the customer: {{answer}}

Did the agent hand off to exactly the team the policy requires for this message?"""
ROUTING_SPEC = {"input": "input", "handoff": "metadata:$.handoff_target", "answer": "output"}
ROUTING_OUTPUT = EvaluatorOutputDefinition_Boolean(
    score_reasoning_instructions="One sentence: the team the policy requires, then the team the agent chose.",
    score_value_instructions="true only if the chosen team equals the policy team; otherwise false.")

HELPFULNESS_PROMPT = """Customer message: {{input}}
Support agent's reply: {{output}}

How well does the reply move the customer's problem forward (clear next step, no invented facts)?"""
HELPFULNESS_SPEC = {"input": "input", "output": "output"}
HELPFULNESS_OUTPUT = EvaluatorOutputDefinition_Numeric(
    min_value=0.0, max_value=1.0, score_reasoning_instructions="One or two sentences.",
    score_value_instructions="0 = useless or wrong, 0.5 = partially helpful, 1 = fully resolves or correctly escalates.")

# %%
# TODO 3: your code here

# %%
t0 = time.time()
routing = ensure_evaluator(NAMES["routing"], prompt=ROUTING_PROMPT, mapping=judging.variable_mapping(ROUTING_SPEC),
                           output_definition=ROUTING_OUTPUT, model=JUDGE_MODEL)
helpfulness = ensure_evaluator(NAMES["helpfulness"], prompt=HELPFULNESS_PROMPT,
                               mapping=judging.variable_mapping(HELPFULNESS_SPEC),
                               output_definition=HELPFULNESS_OUTPUT, model=JUDGE_MODEL)
again = ensure_evaluator(NAMES["routing"], prompt=ROUTING_PROMPT, mapping=judging.variable_mapping(ROUTING_SPEC),
                         output_definition=ROUTING_OUTPUT, model=JUDGE_MODEL)
assert (again.id, again.version) == (routing.id, routing.version), "an unchanged definition must not create a new version"
for e in (routing, helpfulness):
    print(f"{e.name:<20} v{e.version} {e.status:<7} variables={e.variables} model={e.model_config_.model}")
print(f"({time.time() - t0:.0f}s)")

# %% [markdown]
# ## TODO 4: the evaluation rule
#
# Write `ensure_rule(evaluators) -> EvaluationRule`: get-or-create-or-update the rule named `NAMES["rule"]`.
# - It should judge **only** root `support-request` observations of traces tagged `LIVE_TAG`:
#   `filter=judging.rule_filters(LIVE_TAG, ROOT_NAME)`.
# - `sampling=1.0`: every match. In production you might judge 10% (`0.1`) of traffic. Sampling is deterministic per
#   observation id.
# - `evaluator_assignments=[EvaluationRuleEvaluatorAssignmentInput(evaluator_id=e.id) for e in evaluators]`. Leaving
#   `variable_mapping` out inherits each evaluator's own mapping. A rule-level mapping would override it.
# - `enabled=True`.
# - Create: `langfuse.api.evaluation_rules.create(name=…, enabled=…, sampling=…, filter=…, evaluator_assignments=…)`.
#   List with `langfuse.api.evaluation_rules.list(limit=100, cursor=…)` (names are not unique here either).
# - Exists but differs (filter, sampling, enabled, or the set of evaluator ids): `langfuse.api.evaluation_rules.update(
#   rule.id, filter=…, sampling=…, enabled=…, evaluator_assignments=…)`. Compare filters as dicts
#   (`f.dict()` for each stored filter).
#
# Why **observations, not traces**: a rule tests every observation the worker ingests. Without `isRootObservation`,
# a 10-span trace would be judged 10 times. Why **`name`, not `traceName`**: verified live, `traceName` never matched
# our spans (they carry no trace name unless you propagate one), and nothing tells you. The rule just never fires.
#
# A rule only sees traffic **ingested after** it exists. Older traces are never judged by it, which is why this cell
# comes before the run.

# %%
from langfuse.api.evaluation_rules import EvaluationRuleEvaluatorAssignmentInput

# TODO 4: your code here

# %%
rule = ensure_rule([routing, helpfulness])
assert ensure_rule([routing, helpfulness]).id == rule.id
print(rule.name, "enabled" if rule.enabled else "DISABLED", "sampling", rule.sampling)
for f in rule.filter:
    print("  filter:", f.dict())
print("  evaluators:", [a.evaluator_id for a in rule.evaluator_assignments])

# %% [markdown]
# ## Run it, including the planted failure
# Five single-turn cases. The login case runs on the **`wrong_handoff`** prompt (login → billing). Four cases carry
# `LIVE_TAG`; `returns_policy_unjudged` does not, so the rule must leave it alone.
# After each turn, module 06's **code score** `handoff_correct` is sent. This is the answer key the judge gets
# calibrated against.
#
# That's about 15 agent calls here, and 8 judge calls (4 traces × 2 evaluators) made by the worker, not by this kernel.

# %%
rows = []
for case in CASES:
    judged = case.case_id not in traffic.UNJUDGED_CASES
    agent = planted_agent if case.failures else clean_agent
    tags = [case.case_id, *case.failures, *([LIVE_TAG] if judged else [])]
    run, trace_id = traced_turn(case.message, agent, session_id=case.session_id, user_id=case.user_id, tags=tags)
    actual = run.summary["handoff_target"]
    ok = scoring.handoff_correct(actual, case.expected_handoff)
    send_score(CONFIGS, NAMES["code_score"], ok, trace_id=trace_id,
               comment=None if ok else f"expected {case.expected_handoff}, got {actual}")
    rows.append({"case": case, "trace_id": trace_id, "judged": judged, "actual": actual, "code_ok": ok})
langfuse.flush()

for r in rows:
    print(f"{r['case'].case_id:<36} expected={r['case'].expected_handoff:<19} actual={r['actual']:<19} "
          f"code_ok={r['code_ok']!s:<5} judged={r['judged']}  {langfuse.get_trace_url(trace_id=r['trace_id'])}")

# %% [markdown]
# ## TODO 5: collect the judge's verdicts and calibrate
#
# Write `collect_verdicts(rows, *, timeout=300) -> list[tuple]`:
# - For every row with `judged=True`, poll `langfuse.api.scores.get_many(trace_id=…, fields="details,subject")` until
#   it has a `source == "EVAL"` score named `NAMES["routing"]` **and** one named `NAMES["helpfulness"]`. Sleep a few
#   seconds between polls. Each judge call is a worker → Azure round trip, ~20–40 s after ingestion. Raise
#   `TimeoutError` after `timeout` seconds, naming the traces still missing (and pointing at the worker log).
# - Print one line per judged row: case, actual handoff, `code_ok`, the routing verdict
#   (`judging.as_verdict(score.value)`), the helpfulness value, and the first ~90 characters of the routing score's
#   **comment**. That comment is the judge's reasoning, the first thing a reviewer reads (module 08).
# - Return `[(judge_ok, code_ok), …]` for the judged rows.
#
# Look at a judge score's `metadata` and `subject` once: `evaluator_id`, `evaluator_version_id`,
# `evaluation_rule_id`, and `subject.kind == "observation"` on the **root** span. Provenance comes for free here,
# unlike the `metadata.evaluator` you stamped by hand in module 06. Note `configId` is `None`: LLM-judge scores are not
# tied to a score config.

# %%
# TODO 5: your code here

# %%
pairs = collect_verdicts(rows)
REPORT = judging.agreement(pairs)
print(REPORT)

# %% [markdown]
# ## TODO 6 (optional; ⚠️ on this deployment): a code evaluator
#
# Code evaluators run *your* function in the worker instead of an LLM: free, deterministic, instant. The contract
# (from the worker source) is a TypeScript function:
# ```ts
# function evaluate(ctx) {            // ctx.observation.{input, output, metadata, toolCalls}
#   return { scores: [{ name: "...", dataType: "BOOLEAN", value: true, comment: "..." }] };
# }
# ```
# Write `CODE_SOURCE` (a string): a `handoff_code` evaluator that returns BOOLEAN `true` when
# `ctx.observation.metadata.handoff_target` is one of the four valid values (`billing_specialist`,
# `account_security`, `shipping_specialist`, `none`). That's a cheap schema guard that needs no judge.
# Then get-or-create it by name with
# `langfuse.api.evaluators.create(request=CreateEvaluatorRequest_Code(name=NAMES["code"], source_code=CODE_SOURCE,
# source_code_language="TYPESCRIPT"), request_options=…)`. If it is created, add it to the rule with
# `ensure_rule([routing, helpfulness, code_evaluator])`.
#
# On this self-hosted deployment, expect **`412 Code evaluations are not enabled for this deployment.`** Catch
# `PreconditionFailedError` and print the message. Enabling them means setting `LANGFUSE_CODE_EVAL_DISPATCHER` on the
# worker: `insecure-local` (dev only, TypeScript, runs inside the worker process) or `aws-lambda` (production
# sandboxing). Verify reports ⚠️ for this, not ❌.

# %%
from langfuse.api.evaluators import CreateEvaluatorRequest_Code
from langfuse.api.evaluation_errors import PreconditionFailedError

# TODO 6 (optional): your code here

# %% [markdown]
# Verify waits for all 5 traces, then checks:
# - the connection, evaluators (once each, active, on the connection's model) and rule (filters, sampling, both
#   evaluators);
# - judge scores on the root observation of every opted-in trace, and none on the other one;
# - your agreement report against the stored scores;
# - the planted `wrong_handoff` judged `routing_judge=False`.

# %%
verify.check_module_07(RUN_TAG, prefix=PREFIX, live_tag=LIVE_TAG, agreement=REPORT, expected_traces=len(CASES))

# %% [markdown]
# ## UI tour
# - [ ] **Evaluators** (left nav, *Evaluation → LLM-as-a-Judge*): your two evaluators, their versions, and the rule
#   that runs them. Open `routing_judge`: prompt, variables, mapping, model.
# - [ ] **Settings → LLM Connections**: `azure-openai`, with the key masked.
# - [ ] Open the planted trace: on the `support-request` span, the **Scores** tab shows `routing_judge = false`
#   (source *EVAL*), its reasoning, and next to it your code score `handoff_correct`. Two independent signals agreeing.
# - [ ] Click through from the judge score to its **execution trace** (`Execute evaluator: routing_judge`). It lives
#   in the `langfuse-llm-as-a-judge` **environment**: switch the environment filter to see what each judgment cost and
#   how long it took. The prompt it shows is the compiled prompt, with your variables filled in.
# - [ ] Traces table, filter **`routing_judge = false`**: every bad routing, found without a test case.
# - [ ] The `returns_policy_unjudged` trace: no EVAL scores. That's the rule's filter working.
#
# ## Production notes
# - **Judge the policy, not the answer key.** Code scores need expected outputs, which only exist for test cases. A
#   judge prompt that states the policy works on all traffic. Calibrate it against code or human labels (module 08)
#   on the cases where you have both, and track `judge_missed`: a failure the judge lets through is the expensive error.
# - **Put the judge's evidence on the observation.** The judge sees only `input`, `output`, `metadata` and
#   `tool_calls`. Writing `handoff_target` to the root's metadata made the routing judgment possible, and it costs
#   nothing.
# - **Scope and sample.** Filter to root observations and the environments/tags you mean (`environment = production`,
#   an opt-in tag), then set `sampling` (e.g. 0.05–0.2) to cap cost. Every match is an LLM call billed to *your*
#   connection. The `langfuse-llm-as-a-judge` environment shows exactly what judging costs.
# - **Rules are not retroactive.** Deploy evaluators and rules before the traffic you want judged. For history, use
#   batch evaluation (module 09) instead.
# - **Evaluators as code.** Get-or-create by name at deploy time, update on change (each update is a new version,
#   and the score's `evaluator_version_id` says which version judged it). Never create on every request: a timeout
#   still creates.
# - **Watch for paused evaluators.** A broken connection or model pauses the evaluator (`status=paused`,
#   `paused_message`). Judging stops silently, so alert on it (module 12).
# - **Code evaluators** are the cheap first line (schema checks, regexes, length limits) once a dispatcher is
#   configured. Keep LLM judges for what needs judgment.
# - **Why it matters (leadership):** every production conversation gets a quality verdict with a written reason,
#   automatically and within a minute. It costs a controlled sample of judge calls, and we know how far we can trust
#   the judge.
