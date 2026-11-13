# Langfuse Mastery: Module 04 (Prompt management) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 02, 03 and 06 plans.

**Goal:** Module 04 answers "which prompt produced this answer, and how fast can we undo a bad one?". It teaches:
- **Prompt versions and labels:** every `create_prompt` makes an immutable version; labels (`production`, `staging`, and the
  automatic `latest`) are movable pointers. The app fetches **by label**, so moving a label is a deploy with no code change.
- **Client-side caching:** `get_prompt` caches per process (default TTL 60s, stale-while-revalidate) and a `fallback`
  keeps the agent answering when the prompt can't be fetched.
- **Linking prompt versions to generations:** every LLM call in a trace records which prompt name + version produced it.
- **Prompt config:** model parameters stored with the version and applied at runtime.
- **The Playground:** UI tour only.
- **Planted failure `wrong_handoff` as a BAD prompt version:** v2 routes login issues to billing, gets promoted to
  `production`, misroutes live traffic, and is rolled back by moving the label. Verify proves it from the
  generation↔prompt-version links.

Module 04 builds on modules 01 and 02 (instrumentation, tags). It is independent of modules 03 and 06.

**Conventions:** the same as the scaffold, module 02, 03 and 06 plans:
- Work from `ch09/` and use `$PY = .venv-langfuse/Scripts/python.exe`.
- Lab tests run with `$PY -m pytest langfuse_lab/tests -q`.
- `git add` files explicitly.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Branch: `langfuse-mastery`.
- `langfuse_lab/` contains no Langfuse code except `verify.py`, which only reads data. The learner writes all the Langfuse code.

**Downstream contract (module 09 reuses it):** prompt name **`support-agent-system`** (`prompts.PROMPT_NAME`), type
**text**, variables `{{company}}` and `{{max_sentences}}` (`prompts.PROMPT_VARIABLES`), config `{"temperature": 0,
"max_tokens": 400}` (`prompts.PROMPT_CONFIG`). After module 04: label **`production`** = the good version
(`SYSTEM_PROMPT_TEMPLATE`), label **`staging`** = the bad candidate (`WRONG_HANDOFF_TEMPLATE`). Module 09's v1-vs-v2
experiment is "production vs staging" by label, never by version number (numbers depend on the learner's history).

**Verified live (planner probe, 2026-11-11, server 4.77 / SDK 5.2):**
- **Create:** `langfuse.create_prompt(name=, prompt=str | [chat messages], labels=[...], tags=, type="text"|"chat",
  config=dict, commit_message=)` → `TextPromptClient` with `.name .version .labels .config .commit_message .prompt
  .variables .is_fallback`. The new version always gets `latest` too.
- **No dedupe:** creating a version with **identical** text and config makes a **new version** (v1 → v2, same content).
  So get-or-create must compare content itself: list versions, fetch each, compare `prompt` + `config`.
- **Read:** `api.prompts.list(name=)` → `.data[0].versions` (all version numbers), `.labels`, `.last_config`.
  `api.prompts.get(name, version=|label=)` → `Prompt_Text` (`.dict()` keys: type, prompt, name, version, config,
  labels, tags, commitMessage, id, createdAt, …). Without version/label it returns the `production` version.
- **Labels are unique per prompt:** `langfuse.update_prompt(name=, version=, new_labels=["production"])` *adds* the labels
  to that version and removes them from whichever version had them (other labels of the target are kept). Raw API:
  `api.prompt_version.update(name, version, new_labels=[...])`, same semantics. Langfuse keeps **no label history**,
  so "roll back to the previous version" means remembering which version held the label before you moved it.
- **Caching:** `get_prompt(name, label=, cache_ttl_seconds=)` caches per process. `update_prompt` invalidates the
  local cache for that name, so the *same process* sees a move immediately. A move made **elsewhere** (UI, another
  service, `api.prompt_version.update`) is **stale-while-revalidate**: the old version is served until the TTL
  expires, then **once more** while a background refresh runs, then the new one (seen: 1, 1, 1, 3 with TTL 4s).
- **Fallback:** on any fetch error (404 name/label, network), `get_prompt(..., fallback=str)` returns a
  `TextPromptClient` with `is_fallback=True`, `version=0`, `config={}`, and logs a warning. Without a fallback it raises
  `NotFoundError`. Fallback prompts are **never linked** to generations (SDK skips them).
- **`compile()` is lenient:** a missing variable is left as `{{max_sentences}}` in the text, silently.
- **Linking on SDK 5.2 + LangGraph (both work, every GENERATION linked, including all tool-loop steps):**
  1. `propagate_attributes(prompt=prompt_client)` around `agent.invoke(...)` (new in SDK 5.x; framework-agnostic).
  2. LangChain: `config={"callbacks": [handler], "metadata": {"langfuse_prompt": prompt_client}}` on `graph.invoke`
     (the CallbackHandler registers it on chain runs and links the next generation below them).
- **Observation fields:** field group **`prompt`** = `promptId, promptName, promptVersion`. Generations carry the link;
  every non-generation observation has `promptName=""` and `promptVersion=None`. Field group `model` carries
  `modelParameters`: per-turn `llm.bind(temperature=0.2, max_tokens=300)` shows as `{'temperature': 0.2, 'max_tokens': 300}`.
- **TOOL input** of `create_handoff` is a JSON string `{"target": "account_security", "reason": "…"}`.
- **Delete:** `api.prompts.delete(name, version=|label=)` exists and works (all versions when neither is given).
- Probe objects `zz_probe_m04_prompt`, `zz_probe_m04_prompt_chat`, `zz_probe_m04_sys` were deleted afterwards.

---

### Task 1: lab: prompt template, agent prompt/params hook, module 04 demo cases

**Files:** modify `langfuse_lab/prompts.py`, `langfuse_lab/agent.py`, `langfuse_lab/traffic.py`, tests
`tests/test_failures_prompts.py`, `tests/test_agent.py`, `tests/test_traffic.py`.

- `prompts.py`: `PROMPT_NAME`, `PROMPT_VARIABLES`, `PROMPT_CONFIG`, `SYSTEM_PROMPT_TEMPLATE` (with `{{company}}`,
  `{{max_sentences}}`), `WRONG_HANDOFF_TEMPLATE`, and `render()`. `SYSTEM_PROMPT` / `WRONG_HANDOFF_PROMPT` become the
  rendered templates and stay **byte-identical** to before (test against the literal first and last lines).
- `agent.py`: `build_graph(prompt=str | callable, llm_params=callable | None)`; `SupportAgent(system_prompt=None)`;
  `SupportAgent.invoke(..., system_prompt=None, llm_params=None)` overrides the prompt / binds model kwargs **for one turn**
  (reset in `finally`). Existing callers unchanged. Tests with a recording fake model: default prompt, constructor
  override, per-turn override then reset, `llm_params` reaching the model call.
- `traffic.py`: `PromptCase(case_id, conversation_id, session_id, user_id, message, phase, expected_handoff, failures)`
  with `.tags = (f"phase:{phase}", case_id, *failures)`, and `prompt_demo_cases(run_id)`: 6 single-turn cases in
  3 phases, `PROMPT_PHASES = ("healthy", "bad", "rollback")`:
  - healthy: login cust_02 (→ account_security), double charge cust_00 (→ billing_specialist)
  - bad: login cust_02, locked out cust_03, both `failures=("wrong_handoff",)` (served by the bad version)
  - rollback: the same two login requests
- Commit: "lab: module 04 prompt template, per-turn prompt/params hook, demo cases".

### Task 2: verify: module 04 checks

**Files:** `verify.py`, tests in `tests/test_verify_m04.py` (fakes shaped like the live `Prompt_Text.dict()` and
`ObservationV2` with the `prompt` + `model` groups).

- `PROMPT_FIELDS = FIELDS + ",prompt,model"`, `prompt_versions(client, name) -> dict[int, dict]` (`api.prompts.list` +
  `api.prompts.get(version=)`), `prompt_label(client, name, label) -> int | None`.
- `module_04_checks(run_tag, client, *, prompt_name=PROMPT_NAME, expected_traces=6)`, one snapshot per attempt:
  1. **prompt versions, no duplicates:** a version with exactly `SYSTEM_PROMPT_TEMPLATE` and one with
     `WRONG_HANDOFF_TEMPLATE`, each with `PROMPT_CONFIG`; no two versions share (text, config).
  2. **labels: production = good, staging = bad** (the rollback happened; the candidate stays on staging for 09).
  3. **every generation linked to a prompt version** (`promptName == prompt_name`, int `promptVersion`).
  4. **prompt config applied:** each generation's `modelParameters` temperature/max_tokens equal its linked version's config.
  5. **planted bad version caught by its links:** every `phase:bad` trace's generations link the bad version and at
     least one bad-phase trace handed off to a team other than `account_security`; `phase:healthy` traces link the good one.
  6. **rollback restored the good version:** every `phase:rollback` trace links the good version and hands off to
     `account_security`.
- Missing traces → `NotYet`; a missing prompt → fail with "run ensure_version".
- `check_module_04(run_tag, *, client=None, prompt_name=PROMPT_NAME, expected_traces=6, retries=12)`.
- Unit tests: passing fixture + one counterexample per check.
- Commit: "lab: module 04 checks (versions, labels, generation links, config, planted bad version + rollback)".

### Task 3: learner pure functions: `langfuse_mastery/prompting.py` + tests

- `template_variables(template) -> list[str]`: unique `{{ name }}` names in first-seen order, whitespace-trimmed.
- `compile_strict(template, variables) -> str`: substitute every variable; **raise `ValueError` naming the missing ones**
  (Langfuse's `compile()` silently leaves them). Extra variables are ignored.
- `llm_params(config) -> dict`: the model kwargs to send, from a prompt config: keeps `temperature` (0..2),
  `max_tokens` (positive int, bool rejected), `top_p` (0..1); ignores other keys; raises `ValueError` on a bad value.
- About 18 parametrized tests. Commit: "mastery: module 04 prompting stubs + tests (learner implements)".

### Task 4: Module 04 notebook + live validation

`src/04_prompt_management.py` → `04_prompt_management.ipynb`. Setup like module 03 (`RUN_TAG = f"run:m04-…"`, `ENV="dev"`).

1. Concept table + diagram (versions, labels, cache, fallback, link, config), leadership framing.
2. Given: instrumentation recap (`observed_guardrail`, `observed_search`, one clean `SupportAgent`).
3. TODO 1: `prompting.py` + pytest (its own test file only).
4. TODO 2: `ensure_version(template, *, config, labels=(), commit_message="") -> int`: get-or-create **by content**.
   The cell runs it twice for the good template and asserts one version.
5. TODO 3: `serve_prompt(label="production") -> (prompt, system_text, params)` with `cache_ttl_seconds=CACHE_TTL` and a
   fallback, compiling with `prompting.compile_strict` / `prompting.llm_params`; and `traced_turn(case)` that links the
   prompt (either linking method). The cell checks the fallback path with a missing label.
6. TODO 4: `promote(version, label="production") -> int | None` (returns the version that held the label before) and
   `label_table()`.
7. Run: promote good → healthy phase; create bad with `staging`, promote → bad phase; roll back with the remembered
   version → rollback phase. Given `wait_until_served(version)` between phases. ~6 turns, ~18 Azure calls.
8. Verify: `verify.check_module_04(RUN_TAG, expected_traces=6)`.
9. Given demo: cache staleness when a label moves **elsewhere** (raw API on `staging`, TTL 5s) and back.
10. UI tour (Prompts list, versions + labels, diff, linked generations, metrics per version, Playground) + production notes.

**Live validation:** `_validate_04.ipynb` + reference `prompting.py`, `MODULE PASSED`. Then restore stubs and **delete
the reference `support-agent-system` prompt** (`api.prompts.delete`), so the learner's code creates it.

---

### As implemented (2026-11-11)

- **Validation:** `run:m04-ba7de6` passed on the first run (15 generations, all linked; both bad-phase logins routed
  to `billing_specialist` under v2; both rollback logins to `account_security` under v1). `run:m04-d96c8b`, a second
  full run against the existing prompt, also passed and created **no** new versions (get-or-create by content works).
- **Live negative check:** with `production` moved back to v2 after the run, verify reported
  `❌ labels: production = good, staging = bad: production is on v2, not the good v1: roll back …`.
- **Metrics API** groups observations by `promptName` / `promptVersion` (verified; used in the given per-version cell).
- **Cache demo output (TTL 5s, label moved via raw API):** v2 for t=1..6s, v1 from t=7s (stale-while-revalidate).
- **Fallback** on a missing label logs a long 404 warning (with response headers) from the SDK. Expected; the
  notebook says so.
- **Cleanup:** the reference `support-agent-system` prompt was deleted (`api.prompts.delete`); the project has no prompts.
