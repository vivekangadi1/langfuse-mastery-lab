# Langfuse Mastery — Module 02 (Trace context) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (same workflow as the scaffold plan).

**Goal:** Module 02 teaches trace *context*: sessions (multi-turn), users, tags, metadata, environments, releases/versions,
and **distributed tracing** (a handoff handled in a separate OS process that continues the same trace).

**Prerequisite refactor:** verify's per-attempt run snapshot (deferred from the scaffold reviews).

**Conventions:** identical to `docs/plans/2026-11-10-langfuse-mastery-scaffold.md` ("Conventions for every task"):
work from `ch09/`, `$PY = .venv-langfuse/Scripts/python.exe`, tests `$PY -m pytest langfuse_lab/tests -q`, explicit
`git add`, commit trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, branch `langfuse-mastery`.
No Langfuse code in `langfuse_lab/` except `verify.py` (reads data). The learner writes all Langfuse code in the notebook.

**Verified SDK v5.2 facts (planner, 2026-11-10):**
- `propagate_attributes(user_id=, session_id=, tags=, metadata=, version=, environment=, trace_name=)` labels the current
  span + all spans started inside. Environment must match `^(?!langfuse)[a-z0-9_-]+$`. Metadata values ≤200 chars (JSON).
- `release` is client-level (`LANGFUSE_RELEASE` env or `Langfuse(release=...)`); per-trace `version=` via propagate_attributes.
- Cross-process: parent reads `langfuse.get_current_trace_id()` + `langfuse.get_current_observation_id()`; child process
  continues with `langfuse.start_as_current_observation(trace_context={"trace_id": ..., "parent_span_id": ...}, name=..., as_type=...)`
  or `@observe` + call kwargs `langfuse_trace_id=` / `langfuse_parent_observation_id=`. Child must `flush()` before exit.
- ObservationV2 has `user_id`, `session_id`, `environment`, `version`, `metadata` (metadata only with the `metadata` field group).

---

### Task 1: verify — per-attempt run snapshot + shared module runner (refactor, no behavior change)

**Files:** modify `ch09/langfuse_lab/verify.py`, tests in `ch09/langfuse_lab/tests/test_verify*.py`.

- Add `fetch_run(client, run_tag, *, hours=WINDOW_HOURS, expected_traces=None) -> list[tuple[root, list[obs]]]`
  (raises `NotYet` when no roots / fewer than expected; uses the existing truncation guard).
- Add a per-attempt cache so ALL checks in one attempt share one `fetch_run` result: e.g. `run_checks(checks, ..., snapshot=lambda: fetch_run(...))`
  where check fns accept the snapshot (`fn(snapshot)`), OR a small `RunSnapshot` object with a `.get()` memoized per attempt
  and reset by `run_checks` between attempts. Pick the simpler design; keep `Check(name, fn)`; module 00's checks that don't
  need traces must keep working.
- Add `_check_module(checks, client, *, retries)` = flush once + run_checks + print_report; make check_module_00/01 one-liners on it.
- Prefer `NotYet` over `AssertionError` for "not ingested yet" everywhere (keep AssertionError retryable for compatibility).
- Test that one attempt of module 01's checks performs exactly 1 roots query + N trace queries (not ×checks), using a counting fake.
- All existing tests must still pass (adapt fixtures only if signatures change). Commit: "lab: verify fetches one run snapshot per attempt".

### Task 2: specialist (pure business logic) + subprocess runner

**Files:** create `ch09/langfuse_lab/specialist.py`, `ch09/langfuse_lab/tests/test_specialist.py`.

- `resolve_handoff(target: str, reason: str, conversation: list[str]) -> dict` — deterministic, no LLM: returns e.g.
  `{"team": target, "ticket_id": f"{prefix}-{sha1(reason+target)[:6]}", "sla_hours": {...}[target], "summary": <=120 chars}`
  for `billing_specialist | account_security | shipping_specialist`; unknown target → `{"error": "unknown_team"}`.
- `run_worker(script: Path, payload: dict, *, timeout=60) -> dict`: runs `[sys.executable, str(script)]` with the payload as
  JSON on stdin, inherits `os.environ` (so LANGFUSE_* keys reach the child), returns the JSON the child prints on its LAST
  stdout line; raises `RuntimeError` with stderr on non-zero exit. No Langfuse imports.
- Tests: resolve_handoff per team + unknown; run_worker with a tiny temp script echoing `{"echo": payload, "pid": os.getpid()}`
  → pid differs from the test process; non-zero exit raises with stderr text.
- Commit: "lab: specialist handoff logic + subprocess worker runner".

### Task 3: traffic — deterministic demo conversations for module 02

**Files:** modify `ch09/langfuse_lab/traffic.py`, `tests/test_traffic.py`.

- `demo_conversations(*, run_id: str, env: str = "staging", release: str = "v1") -> list[ConversationSpec]` returning exactly:
  1. fixture session `s_refund_followup` (cust_00, 2 turns, billing → handoff expected),
  2. fixture session `s_login_then_2fa` (cust_02, 2 turns, login → handoff to account_security expected),
  3. single-turn "What is your refund policy?" for cust_05.
  Ids follow the existing run-scoped scheme; `multi_turn` set correctly; `tags` property works.
- Tests: 3 specs, 5 turns total, 3 distinct users, 2 multi-turn sessions, ids scoped by run_id.
- Commit: "lab: deterministic demo conversations for module 02".

### Task 4: verify — module 02 checks

**Files:** modify `verify.py`, tests in `tests/test_verify_modules.py`. Use the Task 1 snapshot. Fetch with a field set that
includes `metadata` (e.g. `FIELDS + ",metadata"` for these queries).

`module_02_checks(run_tag, client, *, expected_traces, environment, version)` — checks over all roots tagged `run_tag`:
1. **every turn has user + session** — every root has non-empty `user_id` and `session_id`.
2. **multi-turn sessions grouped** — ≥2 session_ids each with ≥2 root traces.
3. **users tracked** — ≥3 distinct user_ids.
4. **business metadata** — every root's metadata contains `intent` and `conversation_id` (metadata may arrive JSON-encoded
   per v5 — decode values with json.loads when they are strings).
5. **environment + version** — every root has `environment == environment` and `version == version`.
6. **handoff continued in another process** — some trace contains an observation named `specialist-<team>` whose
   metadata `pid` differs from its root's metadata `pid`, and whose parent chain reaches the root (same trace_id).
`check_module_02(run_tag, *, client=None, expected_traces=5, environment="staging", version="v1", retries=12)`.
Unit tests with fakes: all pass; each check failing on its own counterexample. Commit: "lab: module 02 checks (sessions, users, metadata, env/version, distributed handoff)".

### Task 5: Module 02 notebook + live validation

**Files:** create `ch09/langfuse_mastery/src/02_trace_context.py`, build `02_trace_context.ipynb`; add
`ch09/langfuse_mastery/.gitignore` with `_specialist_worker.py` and `_validate_*.ipynb`.

Notebook (module template; same bootstrap/setup as module 01; `RUN_TAG = f"run:m02-..."`, `ENV = "staging"`, `RELEASE = "v1"`):
1. **Concept**: trace vs session vs user; tags vs metadata (filterable labels vs structured context); environment
   (isolation of dev/staging/prod data) vs release/version (what code produced it); in-process context (contextvars) vs
   cross-process context (pass trace_id + parent span id explicitly). Diagram of the target: session → 2 traces, the second's
   handoff continues in a `specialist-*` span from another process.
2. **Demo**: `traffic.demo_conversations(...)` printed as a table (session, user, turns, tags) — no LLM calls.
3. **TODO 1 — per-turn root with context**: `handle_turn(spec, message, history, agent) -> run` with
   `@observe(name="support-request", as_type="agent", capture_input=False)`; `propagate_attributes(user_id=, session_id=,
   tags=[RUN_TAG, *spec.tags], metadata={"intent", "conversation_id", "pid": str(os.getpid())}, environment=ENV, version=RELEASE)`;
   CallbackHandler; `update_current_span(input=message, output=final_answer)`; returns the AgentRun. Plus
   `run_conversation(spec, agent)` looping turns, passing `history=run.messages` (one trace per turn, shared session_id).
   Expected: the Sessions view shows 2 two-turn sessions.
4. **TODO 2 — the specialist worker (separate process)**: a `%%writefile _specialist_worker.py` cell the learner completes:
   read JSON payload from stdin (trace_id, parent_span_id, target, reason, conversation), continue the trace with
   `start_as_current_observation(trace_context=..., name=f"specialist-{target}", as_type="agent")`, call
   `langfuse_lab.specialist.resolve_handoff(...)`, set input/output + metadata `{"pid": str(os.getpid())}`, flush, print the
   result JSON on the last line. (Scaffold the file skeleton: bootstrap + stdin/stdout plumbing given; Langfuse parts are TODOs.)
5. **TODO 3 — dispatch the handoff from the parent**: inside `handle_turn`, when `run.summary["handoff_target"] != "none"`,
   call `specialist.run_worker(Path("_specialist_worker.py"), {trace_id: langfuse.get_current_trace_id(),
   parent_span_id: langfuse.get_current_observation_id(), ...})` and attach the ticket to the root output/metadata.
6. **Run**: `for spec in demo_conversations(run_id=RUN_TAG..., env=ENV, release=RELEASE): run_conversation(spec, agent)`; flush.
   ~5 turns ≈ 15 Azure calls.
7. **Verify**: `verify.check_module_02(RUN_TAG, expected_traces=5, environment=ENV, version=RELEASE)`.
8. **UI tour**: Sessions view (replay a conversation), Users view (per-user traces), filter by environment/version/tag,
   metadata panel, the `specialist-*` span inside the parent trace (note its pid differs), agent graph.
9. **Production notes**: set context once at the request boundary; tags = low-cardinality filters, metadata = high-cardinality
   context; never put secrets/PII in tags/metadata (masking is module 05); separate environments instead of separate
   projects for staging/prod data hygiene; for HTTP services prefer W3C `traceparent` header propagation (OTel) — same idea;
   leadership line: "one link shows a customer's whole conversation across every service that touched it".

Build with `$PY -m langfuse_mastery._nbbuild 02`. **Live validation** exactly like modules 00/01: `_validate_02.ipynb` inside
`ch09/langfuse_mastery/` filled with a reference solution, executed with `-m nbconvert` from that dir, must print
`MODULE PASSED`; then delete it. Fix hints/checks against REAL observation data (ask before changing verify semantics).
Committed notebook: TODOs only, no outputs. Update `ch09/langfuse_mastery/README.md` module list (02 available).
Commit: "mastery: module 02 (sessions, users, env/version, distributed handoff)".
