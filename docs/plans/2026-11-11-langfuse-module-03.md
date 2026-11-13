# Langfuse Mastery: Module 03 (Cost & tokens) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 02 and 06 plans.

**Goal:** Module 03 answers "what does each conversation cost us?". It teaches:
- **Model definitions:** how Langfuse turns `usage_details` into USD. Built-in prices exist, a custom definition overrides
  them, and a generation whose model name matches **no** definition gets **no cost at all**.
- **Usage details:** input / output / cached-input tokens as the LangChain `CallbackHandler` records them for `AzureChatOpenAI`.
- **Cost rollups:** cost per trace, session, user, release (version) and model through the **Metrics API**.
- **Planted failure `cost_spike`:** release v2 switches to the expensive deployment and pads the system prompt. The cost
  rollup must make the spike obvious, and the learner's budget check must flag it.

Module 03 builds on modules 01 and 02 (instrumentation, sessions, users, version) and is independent of module 06.

**Conventions:** these are the same as in the scaffold, module 02 and module 06 plans:
- Work from `ch09/` and use `$PY = .venv-langfuse/Scripts/python.exe`.
- Lab tests run with `$PY -m pytest langfuse_lab/tests -q`.
- `git add` files explicitly.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Branch: `langfuse-mastery`.
- `langfuse_lab/` contains no Langfuse code except `verify.py`, which only reads data. The learner writes all the Langfuse code.

**Verified live (planner probe, 2026-11-11, server 4.77 / SDK 5.2):**
- **Reported model names (what generations actually carry in `.model`):**
  - Agent deployment `gpt-4.1-mini` → `gpt-4.1-mini-2025-04-14` (the dated model id from the response, not the deployment name).
  - Expensive deployment `gpt-5.2-chat` → **`gpt-chat-latest`** on success. A *failed* call records the request's
    deployment name `gpt-5.2-chat` instead (seen in metrics: `providedModelName='gpt-5.2-chat'`, 0 tokens).
- **Built-in prices:** the server ships 193 Langfuse-managed definitions (`is_langfuse_managed=True`).
  - `gpt-4.1-mini-2025-04-14` **is** matched by a built-in (`(?i)^(openai/)?(gpt-4.1-mini-2025-04-14)$`: input 4e-7,
    output 1.6e-6, input_cache_read 1e-7 per token).
  - `gpt-chat-latest` and `gpt-5.2-chat` match **nothing**. Their generations have `usage_details` but `cost_details={}`,
    `total_cost=None` and `internal_model_id=""`. This is the "unmatched model → no cost" trap, live.
- **Custom definitions:** `api.models.create(model_name=, match_pattern=, unit="TOKENS", pricing_tiers=[{"name": "Standard",
  "isDefault": True, "priority": 0, "conditions": [], "prices": {"input": …, "output": …, "input_cache_read": …}}])`.
  - Plain dicts with camelCase keys are accepted for `pricing_tiers`. Prices are **USD per token** (not per 1M).
  - **Custom beats built-in:** after a custom `(?i)^gpt-4\.1-mini(-\d{4}-\d{2}-\d{2})?$` definition, new mini generations
    report `internal_model_id` = the custom definition's id and its price.
  - Names are not unique (like score configs), so the learner needs **get-or-create by `model_name` among non-managed definitions**.
  - `api.models.list(page=, limit=100)` returns built-ins and custom ones (2 pages here). `api.models.delete(id)` works for
    custom definitions.
- **Not retroactive:** cost is computed at ingestion. Traces ingested before a definition exists keep their old cost
  (`None`, or the built-in price) after the definition is created. Checked 2+ minutes later. So the notebook defines
  models **before** sending traffic.
- **Latency:** cost is readable about 1s after `flush()` (observations API and Metrics API alike).
- **Cached tokens:** Azure caches prompts ≥1024 tokens. On a repeat of the padded prompt (4421 input tokens), the handler
  recorded `usage_details={"input": 197, "input_cache_read": 4224, "output": 5, "total": 4426}`. So `input` is the **uncached**
  part, and `cost_details.input_cache_read` uses the `input_cache_read` price. Without that price, cached tokens cost 0.
- **Observations API:** the `usage` field group = `usageDetails, costDetails, totalCost, usagePricingTierName`; the `model`
  group = `model, internalModelId, modelParameters`.
- **Metrics API v2:** `api.metrics.metrics(query=json.dumps({...}))`, `view="observations"`, real-time.
  - Measures: `totalCost`, `inputCost`, `outputCost`, `inputTokens`, `outputTokens`, `totalTokens`, `count`. Result keys
    look like `sum_totalCost`.
  - `sessionId`, `userId` and `traceId` are **high-cardinality: filters only**. Grouping by `sessionId` returns 400.
  - Grouping works for `version`, `release`, `providedModelName`, `type`, `name`, `environment`. `tags` groups by the whole array.
  - Filter run-scoped with `{"column": "tags", "operator": "any of", "value": [RUN_TAG], "type": "arrayOptions"}`.
  - An empty match returns `[{"sum_totalCost": None}]`, not 0.
- **Lab bug found:** `make_llm(expensive=True)` sent `temperature=0`, and `gpt-5.2-chat` rejects it (400 "Only the
  default (1) value is supported"). So `cost_spike` could never run. Fix: omit temperature for the expensive deployment.

---

### Task 1: lab: make cost_spike runnable + module 03 demo conversations

**Files:** modify `langfuse_lab/agent.py` (`make_llm`), `langfuse_lab/traffic.py`, `tests/test_agent.py` (or `test_config.py`),
`tests/test_traffic.py`.

- `make_llm(settings, expensive=True)` doesn't pass `temperature` (the model only accepts its default). The agent deployment
  keeps `temperature=0`. Test it without network: build both and assert `.temperature`.
- Add `cost_demo_conversations(*, run_id, env="dev") -> list[ConversationSpec]`, which returns exactly:
  1. `s_refund_followup` (cust_00, 2 turns), release `v1`
  2. `s_shipping_then_policy` (cust_01, 2 turns), release `v1`
  3. single-turn "What is your refund policy?" (cust_05), release `v1`
  4. `s_login_then_2fa` (cust_02, 2 turns), `failures=("cost_spike",)`, release `v2`. "v2 shipped the expensive model
     and a bloated prompt."
  Ids follow the run-scoped scheme. Seven turns, four sessions, four users.
- Tests: shape, exactly one spike conversation (on release v2), run-scoped ids, `estimate_llm_calls == 21`.
- Commit: "lab: cost_spike runnable on gpt-5.2-chat; module 03 demo conversations".

### Task 2: verify: module 03 checks

**Files:** modify `verify.py`, tests in `tests/test_verify_m03.py` with fakes shaped like the live responses above.

- `MODEL_DEFS = ("azure-gpt-4.1-mini", "azure-gpt-5.2-chat")`; `COST_FIELDS = FIELDS + ",usage,model"`.
- `custom_models(client) -> dict[name, list[model dict]]`: non-managed definitions, paginated.
- `module_03_checks(run_tag, client, *, session_costs, user_costs, flagged_sessions, expected_traces=7, spike_factor=5.0)`.
  It uses one snapshot per attempt (run traces with `COST_FIELDS` + custom models). The checks:
  1. **model definitions defined once:** each `MODEL_DEFS` name exists exactly once among custom definitions, with
     unit TOKENS and positive `input`, `output` and `input_cache_read` prices.
  2. **every generation priced by your definitions:** every GENERATION in the run has `total_cost > 0` and
     `internal_model_id` in the learner's definition ids. The failure names the model string, e.g.
     "`gpt-chat-latest` has no cost: no definition matches it", or "priced by a built-in definition: your pattern
     doesn't match `gpt-4.1-mini-2025-04-14`".
  3. **usage details captured:** every generation has input+output tokens. Where `input_cache_read > 0`, the cost
     detail `input_cache_read` must be > 0 (cache price set). Report the cached-token count.
  4. **cost per session (your query):** `session_costs` keys == the run's session ids, and each value is within 1% of the
     truth (the sum of `total_cost` over the session's observations, from the observations API).
  5. **cost per user (your query):** the same, for `user_costs` keyed by user id (run-scoped).
  6. **planted cost_spike caught:** every `cost_spike` trace costs ≥ `spike_factor` × the median clean trace cost (and
     > 0), and `flagged_sessions` == exactly the spike session(s).
- Missing traces raise `NotYet`. An unpriced generation returns `False` (retried, then fails with the message).
- `check_module_03(run_tag, *, session_costs, user_costs, flagged_sessions, client=None, expected_traces=7, retries=12)`.
- Unit tests: one passing fixture, plus one counterexample per check (duplicate definition, unpriced generation,
  built-in-priced generation, cached tokens unpriced, wrong session sum, missing user, spike not flagged / clean flagged,
  weak spike).
- Commit: "lab: module 03 checks (model definitions, priced generations, usage, cost rollups, planted cost_spike)".

### Task 3: learner pure functions: `langfuse_mastery/cost.py` + tests

- `per_token(prices_per_million: dict) -> dict`: vendor pages quote USD per 1M tokens, while Langfuse wants per token.
- `cost_from_usage(usage: dict, prices: dict) -> float`: sum of `usage[k] * prices[k]` for every priced usage type;
  `total` is skipped unless it is the only price; unknown types cost 0. This is the same math Langfuse runs, so the
  learner can reconcile a generation by hand.
- `over_budget(costs: dict[str, float | None], budget: float) -> list[str]`: ids strictly over the budget, most
  expensive first. `None` counts as 0.
- `cost_spikes(costs: dict[str, float | None], *, factor=5.0) -> list[str]`: ids whose cost is ≥ factor × the median of all
  costs, most expensive first. Median 0 → every positive cost is a spike. Empty → `[]`.
- About 20 parametrized tests. Commit: "mastery: module 03 cost stubs + tests (learner implements)".

### Task 4: Module 03 notebook + live validation

`src/03_cost_tokens.py` → `03_cost_tokens.ipynb`. The setup is the same as module 06, with `RUN_TAG = f"run:m03-…"`
and `ENV = "dev"`.

1. **Concept:** the usage → model definition → cost pipeline; built-in vs custom; match_pattern on the *reported* model
   name; per-token prices and pricing tiers; cost is computed at ingestion (not retroactive); cached tokens; where cost
   rolls up (trace, session, user, version, model); Metrics API vs observations.
2. **Given:** the instrumentation recap (`traced_turn` with `propagate_attributes(session_id, user_id, tags, version=release)`
   plus the CallbackHandler), and `run_conversation(spec)`, which picks the planted agent for `cost_spike`.
3. **Probe (given demo):** one tiny call per deployment, then print each generation's `model`, `usage_details` and
   `total_cost`. The expensive one shows `gpt-chat-latest` with **no cost**.
4. **TODO 1, `ensure_model_definitions() -> dict[name, Model]`:** get-or-create both definitions with patterns that match the
   reported names (including the failed-call name `gpt-5.2-chat`) and per-token prices (via `cost.per_token`). The cell
   runs it twice and asserts the ids are equal, and it asserts every probed model name matches a pattern.
5. **TODO 2, `cost.py` + pytest.**
6. **Run:** `cost_demo_conversations` (7 turns, about 21 Azure calls), defined **after** TODO 1. Then a given reconcile
   cell: `cost.cost_from_usage(gen.usage_details, your prices)` ≈ `gen.total_cost` for one cached generation.
7. **TODO 3, `metrics_cost(filters, dimensions=()) -> list[dict]`** (the Metrics API, run-scoped by tag) and
   `cost_per_session(ids)` / `cost_per_user(ids)`. Session and user are filter-only, so it runs one query per id.
8. **TODO 4, `cost_report()`:** cost by version and by model (grouped queries), per-session costs, `over_budget(…,
   SESSION_BUDGET)` and `cost_spikes(…)`. It returns `flagged_sessions`.
9. **Verify:** `verify.check_module_03(RUN_TAG, session_costs=, user_costs=, flagged_sessions=, expected_traces=7)`.
10. **Not retroactive (given demo):** re-read the probe trace. The expensive generation is still unpriced.
11. **UI tour:** Settings → Models; a generation's cost/usage panel; the Traces cost column; the Sessions and Users
    tables (cost columns); the Dashboard cost widgets by model.
12. **Production notes and leadership line.**

**Live validation:** same as modules 02/06 (`_validate_03.ipynb` + a reference `cost.py`; `MODULE PASSED`, with the
spike caught). Then restore the stubs and **delete the reference model definitions**, so the learner's code creates its own.

---

### As implemented (2026-11-11)

- **TODO order swapped:** TODO 1 = `cost.py` (pure functions, 23 tests), TODO 2 = `ensure_model_definitions`, because the
  definitions use `cost.per_token`. TODO 3 = `metrics_cost` + `cost_per_session` / `cost_per_user`, and TODO 4 =
  `cost_report` (returns the flagged sessions).
- **Ingestion race found live:** in one validation run, the learner's per-session query ran before the spike session's
  last (cached) generation was ingested. It read $0.00822 against an actual $0.01066, and verify's retries can't fix numbers the
  learner already computed. Fix: a given `wait_for_ingestion(trace_ids)` runs after the traffic (every root present,
  and the observation counts stable across two polls 4s apart). The verify failure message now says "if yours is LOWER,
  re-run your cost cell".
- **Validation:** `run:m03-b87294` passed. `run:m03-146d4d` failed (the race above, before the fix). `run:m03-585631` passed
  after the fix: 16 generations, 22,400 cached input tokens priced, v2 $0.0106 vs v1 $0.0025 (5 turns vs 2), and each spike trace
  ≥9× the clean median.
- **Non-retroactivity demo, shown live in the first run:** the probe sent before the definitions stayed `total_cost=None`
  (expensive) and stayed priced by the built-in (agent).
- **Cleanup:** the reference `azure-gpt-*` and `zz_probe_*` model definitions were deleted (`api.models.delete`).
- **Module 06 source:** its test cell now runs only `tests/test_scoring.py`, so module 03's failing stubs don't show up
  there. The learner's existing `06_scores.ipynb` was not touched; it still runs the whole folder.
