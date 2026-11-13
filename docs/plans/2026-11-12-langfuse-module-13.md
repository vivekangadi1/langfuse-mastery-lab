# Langfuse Mastery: Module 13 (Production ops) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 06–12 plans.

**Goal:** modules 01–12 made the agent observable, evaluated and alerting. Module 13 is what the platform team owns
before the agent goes live: **who and what is separated from what, who may do what, how long data lives, how a
customer is forgotten, and what the SDK does to the app when Langfuse is slow or down**. The learner:
- writes the **separation plan** as data (`ops_plan.PLAN`: projects, environments, deployments, API keys per workload,
  team roles, retention per environment) and the **linter** that enforces it against what this edition can do
  (`ops.py`, pytest): least privilege, production isolation, key rotation, retention, Enterprise-only features flagged;
- configures the SDK of a **deployment** in its own process (`_ops_worker.make_client` / `finish`) and measures it
  through a **tap** in front of Langfuse: export batches, spans per batch, request-path latency, delivered vs lost
  (`ops.delivery_report`);
- writes **GDPR erasure** (find every trace of a user across environments, guarded batch delete, poll the async
  deletion until it is gone, a receipt) and runs it twice around a **planted straggler**;
- runs the **DIY retention job** (the OSS stand-in for Enterprise data retention) as a dry run from the plan;
- optionally (UI): a **staging project + an Ingestion-only key**, checked for isolation and least privilege.

Scenario (design row 13): "Prod/staging separation plan; retention settings". **Planted failures:** (1) Langfuse is
**down for 6 s** while a deployment serves traffic: the request path must not notice, and the report must find exactly
the traces the default retry budget lost (the hardened config loses none); (2) a **killed job** (`os._exit`, like an
OOM kill or a frozen Lambda) loses its whole buffer, the job that calls `finish()` loses nothing; (3) a **straggler**:
the erased customer's last trace is still in another process's SDK buffer when the erasure runs, arrives after the
deletion and re-creates their data; the erasure's grace-period re-check must catch it and delete it.

**Prerequisite:** none (module 12 is not needed). A given cell get-or-creates module 04's prompt by content (labels
only set when missing), as module 12 does.

**Conventions:** as in modules 07–12. Project-global names are `PREFIX + base`; the learner uses `PREFIX = ""`,
validation `zz_ref_m13_`. Users of this module are **run-scoped** (`cust-<run id>-NN`), so an erasure can never touch
another run's or the learner's own data.

| object | name (`PREFIX=""`) |
|---|---|
| managed prompt (module 04) | `support-agent-system`, served by label `production` in both environments |
| stable traffic tag | `ops-lab` |
| deployments (environment, release) | `support-api@production` (`production`, `v13.0`), `support-api@staging` (`staging`, `v13.1-rc1`) |
| SDK lab environment | `sdk-lab` (cheap traces, no LLM) |
| optional staging project keys (.env) | `M13_STAGING_PUBLIC_KEY`, `M13_STAGING_SECRET_KEY` (Ingestion permission) |

**Verified live (probe, 2026-11-12, server 4.77 / SDK 5.2; server source read at tag `v4.77.0` on GitHub, read-only):**
- **The project key's reach.** `api.projects.get()` → `Project(id='ch09-eval-project', organization=…,
  retention_days=None)`. Every org-level endpoint answers **403** with the permission it lacks:
  `organizations.get_organization_projects` ("projects:read"), `get_organization_api_keys`
  ("organization:CRUD_apiKeys"), `get_organization_memberships` ("organizationMembers:read"),
  `get_project_memberships` ("projectMembers:read"), `projects.get_api_keys` ("apiKeys:read"), `scim.*`
  ("organizationMembers:read", SCIM error envelope).
- **Organization API keys do not exist on OSS.** Creating one needs the `admin-api` entitlement
  (`organizationApiKeyRouter.create`: `throwIfNoEntitlement("admin-api")`, "Issuing organization-scoped keys is a paid
  feature"); the *Organization settings → API keys* page is hidden without it; and every public-API call made with an
  org key checks `admin-api` too (`authenticateOrganizationApiRequest`: 403 "This feature is not available on your
  current plan"). `oss` = `["trace-deletion", "scheduled-blob-exports", "in-app-agent"]`; `admin-api`,
  `data-retention`, `rbac-project-roles`, `audit-logs`, `prompt-protected-labels`, `self-host-ui-customization` are
  **self-hosted Enterprise**. So project CRUD / key management / memberships / SCIM through the API, native retention,
  project-level roles, audit logs and protected prompt labels are ⚠️ here; module 11's blob-storage hint ("create an
  org key in the UI") cannot be followed on OSS either.
- **Project API key permissions exist on OSS** (no entitlement; `API_AUTH_MIGRATION` default `enforce`,
  `API_KEY_PROJECT_ROLES_ENABLE` default `true`): *Project settings → API keys → Create* offers Full access (`ADMIN`),
  `VIEWER`, `INGEST` (`traces:create`, `scores:save`, `media:create`, `project:read`: it cannot read prompts or traces)
  and `SCORES_INGEST`; optional `expiresAt`. The `.env` pair is a legacy full-access key (all project scopes except
  key/member/project management).
- **Roles** (`projectAccessRights.ts`): OWNER > ADMIN (no project delete) > MEMBER (prompts:CUD, scores, datasets,
  evaluators; no `traces:delete`, no `apiKeys:CUD`, no settings) > VIEWER (read only) > NONE. Without
  `rbac-project-roles` the **organization role applies to every project**: on OSS, isolating *people* needs a separate
  organization. Without `prompt-protected-labels` any MEMBER can move `production`.
- **Native retention**: Enterprise only (`data-retention`), minimum 3 days, nightly; `retention_days=None` = forever.
- **Environments**: `^(?!langfuse)[a-z0-9-_]+$`, ≤ 40 chars, default `default`; on traces, observations, scores,
  sessions; set per client, per env var `LANGFUSE_TRACING_ENVIRONMENT` or per trace with `propagate_attributes(
  environment=)`. Metrics API v2 groups and filters by `environment` on `observations` and `scores-*` (a `traces` view
  no longer exists: 400). Prompts, datasets, score configs and keys are project-global.
- **Trace deletion is asynchronous and batched.** `api.trace.delete(id)` → "Trace deleted successfully",
  `delete_multiple(trace_ids=)` → "Traces deleted successfully", both in ~50 ms. The worker deletes **all pending ids
  every 2 minutes** (log: "Deleting traces [...] from Postgres / Clickhouse", "Completed blob storage cleanup … tombstone
  flushes": the raw events in MinIO go too). Observations and scores vanish together, 3–30 s after the tick: a deletion
  is visible after up to ~2.5 min. `observations.get_many(filter=[userId = …])` finds a user's observations in every
  environment.
- **A straggler re-creates a deleted trace.** Spans that arrive after the deletion was processed (same trace id, or a
  new trace of the same user) are stored normally (probe 5: S deleted and invisible at 16:59:27, a span for S sent at
  16:59:32 made S visible again). Erasure = delete, then re-check after a grace period ≥ producers' flush interval +
  ingestion lag + one deletion tick, and repeat.
- **SDK batching** (OTel `BatchSpanProcessor` under `LangfuseSpanProcessor`): `flush_at` = max export batch (default
  512), `flush_interval` = schedule delay (default 5 s), `timeout` (default 5 s) = the exporter's **whole retry budget per
  batch**: retries on 429/502/503/504 and connection errors with backoff 1, 2, 4, 8 … s (±20 %), at most 6 tries,
  never past the deadline; then the batch is dropped ("Failed to export spans batch due to timeout, max retries or
  shutdown"). The client registers `atexit` → `shutdown()` (flush). Measured through a forwarding tap (20 traces × 3
  spans): defaults → 1 export of 60 spans; `flush_at=6, flush_interval=0.5` → 10 exports of 6; `flush()` per request
  → 10 exports and request p50 **46 ms** instead of 0.6 ms; a 6 s outage (503) with `timeout=5` → one batch dropped,
  28/30 traces stored, request p95 still 0.95 ms; with `timeout=20` → 30/30; `os._exit` with a 60 s interval → 0
  exports; a normal exit without `flush()` → atexit exported all 30.
- **Stack**: web `/api/public/health` and `/api/public/ready` (`{"status":"OK","version":"4.77.0"}`), worker
  `127.0.0.1:3030/api/health` (`{"status":"ok"}`); compose has no `LANGFUSE_EE_LICENSE_KEY` (OSS).

---

### Task 1: lab (non-Langfuse)
- `langfuse_lab/otlp_tap.py`: `Tap(log_path, upstream=)`: a threaded forwarding proxy (127.0.0.1, free port) in front
  of Langfuse; logs every OTLP export (spans, trace ids, body hash = batch identity across retries, status, planted
  outage); `outage(seconds, delay=)` answers 503 (a planted outage); stdlib + `opentelemetry-proto`.
- `langfuse_lab/ops_lab.py`: `EDITION` facts (`OSS_ENTITLEMENTS`, `ENTERPRISE_ONLY` feature → entitlement → what it
  means), `stack_edition()` (reads compose for `LANGFUSE_EE_LICENSE_KEY`), `topology()` (`docker ps` + health
  endpoints, read-only), `start_worker` (Popen: JSON payload on stdin, optional hold until `release()`),
  `SDK_SCENARIOS`, `DEPLOYMENTS`.
- `traffic.ops_cases(*, run_id)`: 3 requests × 2 deployments, run-scoped users; the subject is one of them.
- Tests. Commit: "lab: module 13 OTLP tap, ops lab helpers, ops cases".

### Task 2: verify: module 13 checks
`module_13_checks(run_tag, client, *, plan, traffic, sdk, tap_log, erasure, retention, staging=None, allow_dry=False)`;
one snapshot: all run observations (+ the `prompt` field group) and the kept traces' scores, the project.
1. deployments: production + staging traffic in their environments, same prompt version, run-scoped users.
2. environment separation: the learner's env report = stored data per environment; what is shared (the prompt).
3. the plan lints clean and matches the live stack (project id, environments, native retention unset → DIY job).
4. SDK batching: the report per scenario = the tap log + stored data (batches, spans, statuses, delivered).
5. **planted outage**: request path unaffected, lost traces = sent − stored (≥ 1 with the default budget), hardened 0.
6. **planted killed job**: its buffer is lost (0 exports) and reported; `finish()` delivers everything.
7. **GDPR erasure + planted straggler**: the subject has no observation or score left in any environment, pass 2
   deleted the straggler, every other user's trace of the run is untouched.
8. retention job (dry run): the selection = the plan's policy applied to the stored data at the simulated date.
9. staging project + Ingestion key (⚠️ until created in the UI): another project, ingests, cannot read.
10. native data retention (⚠️ Enterprise).
11. organization API, project-level roles, audit logs, protected labels, SCIM (⚠️ Enterprise; shows the live 403).
Commit: "lab: module 13 checks (...)".

### Task 3: learner `langfuse_mastery/ops.py` + `ops_plan.py` + tests
`ops.py`: `effective_role`, `can`, `keys_due`, `lint_plan`, `expired`, `delivery_report` (given role/action table and
edition facts). `ops_plan.py`: `PLAN` (stub: an empty skeleton). Tests: `test_ops.py`, `test_ops_plan.py`. Commit:
"mastery: module 13 ops + plan stubs + tests (learner implements)".

### Task 4: worker, notebook `src/13_production_ops.py`, validation
`_ops_worker.py` (TODO 3: `make_client`, `finish`; the given part serves cheap requests, can flush per request, hold, or
die). Notebook: concepts; TODO 1 + 2 (pytest); given topology + Enterprise facts; given instrumentation + prompt;
deployments traffic (6 turns, ≈ 16 Azure calls); TODO 3 + SDK lab (7 scenarios through the tap, 2 planted); TODO 4
erasure with the planted straggler; env report (given); retention dry run; TODO 5 (optional UI); verify; UI tour; notes.
Dry rehearsal first (`OPS_DRY_RUN=1`, oracle LLM, 0 Azure), then one live run.

---

### As implemented (2026-11-12)

- **Files:** lab `otlp_tap.py`, `ops_lab.py`, `traffic.ops_cases` (+ `tests/test_ops_lab.py`, 10 tests);
  `verify.module_13_checks` / `check_module_13` (+ `tests/test_verify_m13.py`, 14 tests, a real tap-format JSONL log);
  learner `ops.py` (TODO 1: 6 functions, 34 tests), `ops_plan.py` (TODO 2: the plan, 7 tests), `_ops_worker.py`
  (TODO 3: `make_client`, `finish`); notebook TODO 4 (`find_user_traces`, `delete_traces`, `erase_user`) and optional
  TODO 5 (staging organization/project + Ingestion key in the UI).
- **Release vs version:** `propagate_attributes` has no `release` (it is per client: `Langfuse(release=)` /
  `LANGFUSE_RELEASE`). The kernel plays both deployments with one client, so it sets the plan's release as the trace
  `version`; the worker processes set `release` on their clients (the SDK lab's traces carry `release=sdk-lab`).
- **Rehearsal:** the whole notebook ran first with `DRY_RUN = True` (oracle LLM, 0 Azure calls), `run:m13-2c60c2`:
  MODULE PASSED, 8 ✅ 3 ⚠️, no design change needed.
- **Validation:** one live run `run:m13-f52c3a` (`PREFIX="zz_ref_m13_"`, references swapped in) printed **MODULE
  PASSED** on the first try: 8 ✅, 3 ⚠️ (staging project: no UI keys; native retention; org API & co). Real behaviour:
  6 agent turns all routed correctly (pass rate 1 in both environments, $0.0016 per environment); SDK lab: defaults 1
  export of 60 spans, tuned 10 × 6, flush per request 10 exports with request p50 31.7 ms vs 0.84 ms; the planted 6 s
  outage made 5 failed attempts and dropped 1 batch (2 of 30 traces) with request p95 1.37 ms vs 1.13 ms, the hardened
  `timeout=20` retried 3 times and lost none; the killed job exported nothing (10/10 lost), the job with `finish()` 1
  export, 10/10; erasure pass 1 deleted the subject's production + staging requests in 111 s, the held job's
  straggler re-appeared and pass 2 deleted it in 111 s; the retention dry run at +15 days selected staging (2) and
  sdk-lab (120), kept production (2).
- **Azure:** ≈16 agent LLM calls (6 turns) in the live run; 0 in the rehearsal and the probes.
- **Cleanup:** the prompt `zz_ref_m13_support-agent-system` was deleted (`api.prompts.delete`), the dry rehearsal's
  124 traces and every probe trace (`zz_probe_m13_*`) were deleted by explicit, tag-guarded ids. Left as evidence: the
  live run's traces (`run:m13-f52c3a`: 4 agent requests in `production`/`staging`, 120 `sdk-lab` traces). No score
  configs, datasets, keys, projects or members were created; the learner project's retention and keys were not touched.
