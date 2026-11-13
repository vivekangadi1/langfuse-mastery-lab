# Langfuse Mastery: Module 11 (Analytics) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 06–10 plans.

**Goal:** module 11 turns everything modules 06–10 write into Langfuse (scores from code, judges and humans, levels,
latency, cost, versions) into **operational analytics**: an **agent-health dashboard** built through the API, and a
**weekly metrics pull** (a script a cron job could run) that compares two weeks and flags regressions. Module 03 already
taught the Metrics API for *cost and tokens*; module 11 does not repeat that and goes wider:
- quality: scores by name × source (code / stand-in judge / human ANNOTATION) and by version; judge vs human;
- operations: request and **per-observation** latency percentiles (p50/p95 per tool), level and error rates;
- business: **cost per resolved request** (needs a per-trace join: client side);
- grouping by `version` / `release` / `environment`, filtering by tags; time series;
- the Metrics API's limits and the workaround (top-N on high-cardinality dimensions, full pulls + client-side
  aggregation for everything else);
- custom dashboards + widgets through the (unstable) public API, the built-in dashboards in the UI;
- export: the portable public-API pull (JSONL), the **blob storage integration** to the stack's MinIO (org key), and the
  UI batch export.

Scenario (design row 11): "Agent health dashboard; weekly metrics pull". **Planted failure:** `slow_tool` (lookup tools
sleep 3 s) ships in agent **version v2**, which serves the "current week". Nothing else changes. The dashboard's tool
latency widget must show v2 ≫ v1 for a lookup tool and the weekly report must flag the latency regression.

**Time compression.** A real weekly pull compares two ISO weeks (`analytics.week_windows(now)`). The lab cannot wait a
week, so the notebook sends the "previous week" and the "current week" as two **back-to-back windows** a few seconds
apart, and passes those windows to the same script (`--prev FROM TO --curr FROM TO`). The code path is identical.

**Conventions:** as in modules 07–10. Project-global names are `PREFIX + base`; the learner uses `PREFIX = ""`,
validation `zz_ref_m11_`.

| object | name (`PREFIX=""`) |
|---|---|
| dashboard | `agent-health` (environment filter = the run's environment) |
| widgets | `agent-health · <widget name>` (7, from `langfuse_lab.dashboard_spec.WIDGETS`) |
| score config | `human_routing_ok` (BOOLEAN, module 08's; get-or-create in a GIVEN cell) |
| blob storage integration | one per project (S3_COMPATIBLE → MinIO bucket `langfuse`, prefix `blob-exports/`), optional |

**Verified live (probe, 2026-11-12, server 4.77 / SDK 5.2; probe objects `zz_probe_m11_*`):**
- **Metrics API v2 high-cardinality grouping works as top-N:** grouping by `userId` / `sessionId` / `traceId` returns 400
  *"require both 'config.row_limit' and 'orderBy' with direction 'desc' on a measure field"*; with
  `config.row_limit` (≤1000) and `orderBy=[{"field": "sum_totalCost", "direction": "desc"}]` it works, on the
  observations **and** the scores views. Ascending order → 400. (Module 03's note "grouping by sessionId returns 400" is
  the no-row_limit case.) So the Metrics API can answer "top 5 users by cost", never "every user" or "bottom N".
- Observations view: dimensions `name`, `type`, `level`, `version`, `release`, `environment`, `traceName`, `tags`
  (whole array); measures `count`, `latency` (**ms**), `totalCost`, `totalTokens`, …; aggregations incl. `p50`/`p95`/
  `p99`/`histogram` (`config.bins`). `timeDimension: {"granularity": "minute"|"day"|…}` → `time_dimension` rows.
  Result keys `<agg>_<measure>` (`p95_latency`, `count_count`, `sum_totalCost`).
- Score views: `scores-boolean` `avg(value)` is the pass rate; dimensions `name`, `source`, `traceVersion`,
  `traceRelease`, `environment`, `booleanValue`; `scores-categorical` groups by `stringValue`; the `tags` filter (trace
  tags) works on score views. **Score views filter time on the score's timestamp**, not the trace's: a score written
  5 minutes after its trace falls in the later window (probed with a late score on an old trace).
- `release` comes from the **client** (`Langfuse(release=…)`), `version` per trace from `propagate_attributes(version=…)`;
  both group in the observations view and as `traceRelease`/`traceVersion` on scores.
- Observations API (`get_many`) latency is in **seconds** (`0.301`), the Metrics API in **ms** (`300`). The tags filter
  column is `traceTags` there and `tags` in the Metrics API. Cursor paging (`meta.cursor`) on observations and scores.
- **Custom dashboards are public API on OSS** (`api.unstable.dashboards` / `api.unstable.dashboard_widgets`):
  create/get/list/update widgets (views observations + 3 score views, chart types incl. `LINE_TIME_SERIES`,
  `PIVOT_TABLE`, `PIE`, `NUMBER`), create dashboards with dashboard-level `filters`, `add_placement` (server appends a
  6×6 tile when no position). Plain dicts work for metrics/filters/placements. Server-side validation: **only
  PIVOT_TABLE may have >1 dimension** (400), unknown dimension / filter column → 400. Lists are page-based.
- **Blob storage integrations** need an **organization-scoped** API key: the project key gets 403 *"The projects:read
  permission is required"*. The server's plan table gives the `oss` plan the `scheduled-blob-exports` entitlement, so the
  feature exists on OSS; an org key is created in the UI only (Organization settings → API keys), so the build could
  not exercise it live → verify ⚠️ unless `LANGFUSE_ORG_PUBLIC_KEY`/`LANGFUSE_ORG_SECRET_KEY` are set.
- **UI batch export** writes to MinIO `langfuse/exports/`, but the compose file sets
  `LANGFUSE_S3_BATCH_EXPORT_ENABLED: "false"` → verify ⚠️ (optional) unless an export object newer than the run exists.
- MinIO (`ch09_langfuse_minio`, S3 API on `localhost:9090`, user `minio`, password `LANGFUSE_MINIO_ROOT_PASSWORD`) can be
  listed with a stdlib SigV4 request (no boto3 in the venv); `exports/` is empty.

---

### Task 1: lab (non-Langfuse)
- `traffic.analytics_cases(*, run_id)`: 7 requests × 2 windows (`prev` = v1 clean, `curr` = v2 `slow_tool`), the same
  requests in both: double charge 8129 (billing, lookup_order), shipping 8103 (none, lookup_shipment), unknown order 7777
  (none, tool error → WARNING), login cust_02 (account_security), refund policy (none, KB; the stand-in judge's false
  alarm), cancel sub_03 (none), injection (guardrail-blocked → WARNING, no LLM call). ≈14 Azure calls per window.
- `oracle_llm.OracleChatModel`: a deterministic tool-calling chat model for the dry rehearsal (`ANALYTICS_DRY_RUN=1`).
- `dashboard_spec.py`: `DASHBOARD`, `WIDGETS` (7 widget specs), `dashboard_filters(env)`, `widget_problems(widget,
  spec, prefix)`.
- `s3.py`: read-only `list_objects(...)` (SigV4, stdlib) + `minio_settings()`.
- Tests. Commit: "lab: module 11 analytics cases, oracle LLM, dashboard spec, MinIO listing".

### Task 2: verify: module 11 checks
`module_11_checks(run_tag, client, *, report, export_dir, prefix="", environment="dev", run_started=None,
allow_dry=False)`, one snapshot: all run observations (one paged pull), scores per trace, dashboards + widgets, verify's
own Metrics API queries (tool p95 per window, each widget's query on this run), the blob integration, MinIO listings.
1. two windows of traffic (7 + 7 roots, v1/v2, slow_tool only in `curr`, every root inside one report window, not dry).
2. the Metrics API numbers (TODO 2) match the stored data: requests, error rate, level shares, total cost, pass rates by
   name × source, tool p95, top user by cost.
3. the client-side rollups (TODO 3) match: per user / per session requests + cost + resolved, resolved rate, cost per
   resolved request, judge–human agreement.
4. the `agent-health` dashboard: once, environment filter, every spec widget placed once and matching its spec.
5. **planted, on the dashboard:** the tool-latency widget's query on this run shows a lookup tool's p95 v2 ≥ v1 + 2 s,
   and every widget shows data.
6. portable export: the JSONL files hold every observation and score of the run.
7. blob storage integration → MinIO (⚠️ unless an org key is configured).
8. UI batch export (⚠️ optional unless an `exports/` object newer than the run exists).
9. **planted, in the report:** the weekly report flags a latency regression (lookup tool or request p95) in the current
   window, and the stored data agrees (slow tool spans only in `curr`).
Commit: "lab: module 11 checks (...)".

### Task 3: learner `langfuse_mastery/analytics.py` + tests
Pure: `week_windows`, `flatten`, `shares`, `error_rate`, `per_trace`, `latest_value`, `resolved`, `rollup`,
`cost_per_resolved`, `agreement_rate`, `compare`, `flag` (+ given `DEFAULT_RULES`). Commit: "mastery: module 11
analytics stubs + tests (learner implements)".

### Task 4: `weekly_pull.py`, notebook `src/11_analytics.py`, validation
`weekly_pull.py`: given `Scope`, `query`, `fetch_scores`, `build_report`, `markdown`, CLI `main`; learner TODO 2
`scope_filters` + `health_metrics`, TODO 3 `fetch_observations` + `rollups`. Notebook: TODO 1 analytics.py; given
instrumentation + evaluation recap; traffic (2 windows); a top-N demo; TODO 2; TODO 3; the weekly pull as a subprocess;
TODO 4 dashboard; TODO 5 export (+ optional blob integration, UI export); verify; UI tour; notes.
Budget: ≈28 agent LLM calls (12 LLM turns); dry rehearsal first (0 Azure), then one live run.

---

### As implemented (2026-11-12)

- **Files:** lab `traffic.analytics_cases`, `oracle_llm.py`, `dashboard_spec.py`, `s3.py` (+ `tests/test_analytics_lab.py`),
  `verify.module_11_checks` (+ `tests/test_verify_m11.py`, 19 tests); learner `analytics.py` (12 functions, 20 tests) and
  `weekly_pull.py` (TODO 2 `scope_filters` + `health_metrics`, TODO 3 `fetch_observations` + `rollups`; `query`,
  `fetch_scores`, `is_judge`, `build_report`, `markdown`, CLI given); notebook TODOs 4 (dashboard) and 5 (export).
- **Rehearsal:** the whole notebook ran first with `DRY_RUN = True` (oracle LLM, 0 Azure calls) and `allow_dry=True`:
  MODULE PASSED (`run:m11-7d4da3`), which also exercised the dashboard/widget create path.
- **Validation:** one live run `run:m11-5b9f65` (`PREFIX="zz_ref_m11_"`, reference files swapped in) printed **MODULE
  PASSED** on the first try (7 ✅, 2 ⚠️ optional). Real behaviour: every request routed correctly in both windows, cost
  identical in both ($0.002744: temperature 0, same requests), `lookup_order` p95 1 → 3,001 ms and `lookup_shipment`
  0 → 3,001 ms, request p95 5.2 → 7.4 s (+43%, under the 50% rule), 5/7 resolved per window, $0.00055 per resolved vs
  $0.00039 per request, judge–human agreement 0.5 on 2 pairs (the stand-in's "refund policy" false alarm).
- **Design fix after the live run:** the reference reported only feedback labels that occurred, so
  `feedback_share.thumbs_down` was missing last week and its 0 → 0.43 jump (slow replies) was never compared.
  `health_metrics` now starts from every level and label at 0 (`LEVELS`, `FEEDBACK_LABELS`, documented in the stub).
  Re-ran the weekly pull and verify **on the stored data** (no new traffic): MODULE PASSED, the report now also flags
  `feedback_share.thumbs_down`. Negative controls on the same data: a report without the latency anomalies fails the
  planted check; a request count off by one fails the Metrics check.
- **Azure:** ≈28 agent LLM calls (12 LLM turns; the injection is guardrail-blocked), 0 in probes and rehearsal.
- **Leftovers (deletes blocked):** dashboard `zz_ref_m11_agent-health` (cmwda2dh70038nz66od7jfhhg) + its 7 widgets
  `zz_ref_m11_agent-health · …`; probe dashboard `zz_probe_m11_dash` (cmwd9auhk0036nz66apbvjrj7) + widgets
  `zz_probe_m11_tool_p95`, `zz_probe_m11_trend`; archived config `zz_ref_m11_human_routing_ok`; probe traces/scores
  tagged `zz_probe_m11_35042b` (environment `zz-probe`); the run traces of `run:m11-7d4da3` (dry) and `run:m11-5b9f65`.
