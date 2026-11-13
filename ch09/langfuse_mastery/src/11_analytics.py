# %% [markdown]
# # Module 11: Analytics (agent-health dashboard, Metrics API, weekly metrics pull, export)
#
# **Goal:** modules 06–10 fill Langfuse with evidence: code scores, judge verdicts, human reviews, user feedback,
# levels, latencies, costs, versions. Nobody reads 10,000 traces. This module turns that evidence into the two things
# an AI team actually looks at:
# - an **agent-health dashboard** (traffic, latency per tool, errors, cost, quality per version), built through the
#   API so it lives in code, not in someone's browser;
# - a **weekly metrics pull**: a script a Monday-morning cron job runs, that compares this week with last week and
#   **flags regressions** in a Markdown report.
#
# Module 03 used the Metrics API for cost and tokens. This module goes wider (quality and operations) and shows where
# the Metrics API stops and the public API + client-side aggregation takes over.
#
# | Concept | What it is | In our agent |
# |---|---|---|
# | **Metrics API v2** | server-side aggregation over a **view** (`observations`, `scores-numeric`, `scores-boolean`, `scores-categorical`): measures × aggregations, grouped by dimensions, filtered, per time bucket | p95 latency per tool, pass rate per score × source, error rate |
# | **Percentiles** | `p50`/`p95`/`p99` of `latency` (ms) per observation **name** | `lookup_order` p95, request p95 |
# | **Level / error rate** | the share of requests whose root is `WARNING`/`ERROR` | guardrail blocks, tool errors |
# | **Score sources** | `API` (code, stand-in judge, feedback), `EVAL` (LLM judge), `ANNOTATION` (human) | judge vs human pass rate |
# | **version / release / environment** | the agent version per trace (`propagate_attributes(version=)`), the deployment (`Langfuse(release=)`), the stage | v1 vs v2, one release, `dev` |
# | **High-cardinality dimensions** | `userId`, `sessionId`, `traceId`: only **top-N** (`row_limit` + `orderBy desc`) | "top 5 users by cost" yes, "every user" no |
# | **Client-side aggregation** | pull observations + scores (public API, cursor paging), join per trace yourself | cost per **resolved** request, judge–human agreement, cost per user |
# | **Custom dashboard** | widgets (one chart = one Metrics query) placed on a dashboard with dashboard-level filters | `agent-health` (7 widgets) |
# | **Export** | public API pull (portable), blob storage integration (scheduled, to S3/MinIO), UI batch export | JSONL files, MinIO bucket `langfuse` |
#
# ```
#  traffic (2 windows) ──► Langfuse ──┬─► Metrics API ───────────────► health_metrics()  (TODO 2) ┐
#   prev: v1               traces,    │   (aggregates, top-N)                                     ├─► weekly report
#   curr: v2 + slow_tool   scores     ├─► observations + scores API ─► rollups()         (TODO 3) ┘   (analytics.compare
#                                     │   (every row, cursor paging)                                     + flag, TODO 1)
#                                     ├─► unstable.dashboards / dashboard_widgets ─► agent-health   (TODO 4)
#                                     └─► export: JSONL pull · blob storage → MinIO · UI batch export (TODO 5)
# ```
#
# **The planted failure:** agent **v2** ships `slow_tool` (the lookup tools sleep 3 s). Answers stay correct, costs
# don't move: only latency analytics can catch it. The dashboard must show v2 ≫ v1 for a lookup tool, and the weekly
# report must flag it.
#
# **"Weekly" in a notebook.** We can't wait a week, so the "previous week" and the "current week" are two windows a
# few seconds apart: the same 7 requests on v1, then on v2. The weekly pull script takes the two windows as arguments
# (`--prev FROM TO --curr FROM TO`); without them it uses the last two ISO weeks. Same code either way.
#
# Things verified live on this server (4.77 / SDK 5.2) that shape the TODOs:
# - Grouping by `userId`/`sessionId`/`traceId` is a 400 *unless* the query has `config.row_limit` and an `orderBy`
#   **desc** on a measure: top-N only.
# - Only `PIVOT_TABLE` widgets may have more than one dimension; unknown fields are rejected with 400.
# - Score views filter time on the **score's** timestamp (a review written next Monday counts next week).
# - Observations API latency is in **seconds**, Metrics API latency in **milliseconds**; the tag filter column is
#   `traceTags` in one and `tags` in the other.
# - Blob storage integrations need an **organization** API key (the project key gets 403); UI batch export is
#   switched off in this stack's compose file. Both are optional here (⚠️ in verify).
#
# Docs: https://langfuse.com/docs/metrics/features/metrics-api · https://langfuse.com/docs/metrics/features/custom-dashboards ·
# https://langfuse.com/docs/api-and-data-platform/features/export-to-blob-storage ·
# https://langfuse.com/docs/api-and-data-platform/features/export-from-ui · https://api.reference.langfuse.com

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import importlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from langfuse_lab import verify, traffic, kb, standin_judge, dashboard_spec, s3
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.failures import FailureConfig
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_lab.oracle_llm import OracleChatModel
from langfuse_mastery import analytics, weekly_pull

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
RUN_TAG = f"run:m11-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
ENV = "dev"
OUT = LAB_ROOT / "langfuse_mastery" / "analytics_out" / RUN_ID  # git-ignored: the report + the export

# Dashboards, widgets and score configs are PROJECT-GLOBAL. Keep PREFIX empty.
PREFIX = ""
# True = rehearse with the oracle LLM (no Azure calls). verify refuses dry traffic, so the graded run is False.
DRY_RUN = os.getenv("ANALYTICS_DRY_RUN") == "1"

fixtures = load_fixtures()
CASES = traffic.analytics_cases(run_id=RUN_ID)
print("run tag:", RUN_TAG, "| dry run:", DRY_RUN, "| LLM turns:",
      sum(c.case_id not in traffic.ANALYTICS_NO_LLM for c in CASES))
for c in CASES[:7]:
    print(f"  {c.case_id:<20} {c.user_id}  expect {c.expected_handoff:<19} {c.message}")

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/analytics.py` (pure functions + pytest)
#
# The report's logic is plain functions, tested here and identical in the cron job. Open
# `ch09/langfuse_mastery/analytics.py` (its docstring lists **the metric keys** every later TODO uses) and implement:
# - `week_windows(now)`: the last complete ISO week and the one before (what the cron job reports on);
# - `flatten`, `shares`, `error_rate`: Metrics API rows → the flat metric dict;
# - `per_trace`, `latest_value`, `resolved`, `rollup`, `cost_per_resolved`, `agreement_rate`: the client-side joins;
# - `compare` and `flag`: week-over-week deltas and the regression rules (`DEFAULT_RULES` is given).
#
# The cell runs the tests and reloads your module. Re-run it until it shows `20 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_analytics.py", "-q",
                         "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
analytics = importlib.reload(analytics)
weekly_pull = importlib.reload(weekly_pull)

# %% [markdown]
# ## Given: the instrumentation and the evaluation layer (modules 01–08 recap)
# This is a recap, not a TODO. Everything the analytics read is written here, the way earlier modules taught it:
# - **The client** is built first with `release=` (the deployment; one per process). `version=` (the agent
#   version) is per trace, through `propagate_attributes`.
# - `traced_turn` (modules 01/02): one `support-request` trace per request, session, user, tags, environment,
#   version. The root's **level** is `WARNING` when the guardrail blocks or a tool fails (module 01): that is what
#   the error rate counts.
# - `score_turn` (modules 06–08), all inside the trace context (so the scores carry the trace's environment):
#   `handoff_correct` (code score), `routing_judge` (module 08's stand-in keyword judge, source API) and
#   `user_feedback` (thumbs down when the routing was wrong or the reply took longer than `SLOW_REPLY_S`).
# - `human_review` (module 08): a simulated reviewer gives an **ANNOTATION** verdict `human_routing_ok` (it needs a
#   score config: get-or-create below) on every judge-flagged request plus one control per window.
# - `wait_until_ingested`: traces **and** scores (module 03's lesson: never compute numbers mid-ingestion).

# %%
from langfuse import Langfuse, observe, propagate_attributes
from langfuse.api.scores.types import CreateScoreRequest
from langfuse.langchain import CallbackHandler


def _git_sha():
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=LAB_ROOT, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else "local"


RELEASE = f"agent-{_git_sha()}"
langfuse = Langfuse(release=RELEASE)  # the FIRST client in this kernel (module 05: later settings are ignored)
PROJECT_ID = langfuse.api.projects.get().data[0].id
UI = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")
JUDGE, HUMAN, FEEDBACK = weekly_pull.JUDGE, PREFIX + weekly_pull.HUMAN, weekly_pull.FEEDBACK
SLOW_REPLY_S = 6.0


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


def score_turn(case, run, elapsed):
    actual = run.summary["handoff_target"]
    ok = actual == case.expected_handoff
    langfuse.score_current_trace(name="handoff_correct", value=1.0 if ok else 0.0, data_type="BOOLEAN",
                                 comment=f"expected {case.expected_handoff}, got {actual}",
                                 metadata={"evaluator": "code:v1"})
    judge_ok, reasoning = standin_judge.judge_routing(case.message, actual)
    langfuse.score_current_trace(name=JUDGE, value=1.0 if judge_ok else 0.0, data_type="BOOLEAN", comment=reasoning,
                                 metadata={"evaluator": standin_judge.EVALUATOR})
    happy = ok and elapsed <= SLOW_REPLY_S
    langfuse.score_current_trace(name=FEEDBACK, value="thumbs_up" if happy else "thumbs_down",
                                 data_type="CATEGORICAL", comment="" if happy else
                                 ("wrong team" if not ok else f"slow reply ({elapsed:.1f} s)"))
    return ok, judge_ok


@observe(name="support-request", as_type="agent", capture_input=False, capture_output=False)
def traced_turn(case, agent):
    """One request = one trace. Returns a row for the run table."""
    tags = [RUN_TAG, *case.tags, *(["dry-run"] if DRY_RUN else [])]
    with propagate_attributes(session_id=case.session_id, user_id=case.user_id, tags=tags, environment=ENV,
                              version=case.version):
        started = time.perf_counter()
        run = agent.invoke(case.message, config={"callbacks": [CallbackHandler()]})
        elapsed = time.perf_counter() - started
        level, why = ("WARNING", "blocked by the guardrail") if run.blocked else \
            ("WARNING", f"{run.summary['retry_count']} tool error(s)") if run.summary["retry_count"] else ("DEFAULT", None)
        langfuse.update_current_span(input=case.message, output=run.summary["final_answer"], level=level,
                                     status_message=why, metadata={"handoff_target": run.summary["handoff_target"]})
        ok, judge_ok = score_turn(case, run, elapsed)
        return {"case": case, "trace_id": langfuse.get_current_trace_id(), "handoff": run.summary["handoff_target"],
                "ok": ok, "judge_ok": judge_ok, "level": level, "elapsed": elapsed}


def ensure_human_config():
    """Module 06/08 pattern: get-or-create the BOOLEAN config by name, skipping archived ones."""
    page = 1
    while True:
        resp = langfuse.api.score_configs.get(page=page, limit=100)
        for c in resp.data:
            if c.name == HUMAN and not c.is_archived:
                return c.id
        if page >= (resp.meta.total_pages or 1):
            break
        page += 1
    return langfuse.api.score_configs.create(name=HUMAN, data_type="BOOLEAN",
                                             description="human reviewer: the agent routed this request correctly").id


HUMAN_CONFIG_ID = ensure_human_config()


def human_review(rows):
    """Module 08's reviewer, simulated: every judge-flagged request + the first passed one as a control. The answer
    key is the case's expected team. ANNOTATION scores need the config id (else they're dropped silently)."""
    flagged = [r for r in rows if not r["judge_ok"]]
    controls = [r for r in rows if r["judge_ok"]][:1]
    now = datetime.now(timezone.utc)  # the server clock may lag: stamp the review time explicitly
    langfuse.api.scores.create(request=[
        CreateScoreRequest(id=f"{r['trace_id']}-human", trace_id=r["trace_id"], name=HUMAN,
                           value=1.0 if r["ok"] else 0.0, data_type="BOOLEAN", source="ANNOTATION",
                           config_id=HUMAN_CONFIG_ID, environment=ENV, timestamp=now,
                           comment="simulated reviewer (module 08 answer key)") for r in flagged + controls])
    return {r["trace_id"] for r in flagged + controls}


def wait_until_ingested(rows, reviewed, *, timeout=180, settle=4):
    """Every trace's root, its 3 scores (+ the human one when reviewed), and stable observation counts."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        seen = {}
        for r in rows:
            t = r["trace_id"]
            o = langfuse.api.observations.get_many(trace_id=t, fields="core", limit=200).data
            sc = langfuse.api.scores.get_many(trace_id=t, limit=50).data
            seen[t] = (any(x.parent_observation_id is None for x in o), len(o), len(sc) >= 3 + (t in reviewed))
        if all(root and scored for root, _, scored in seen.values()) and seen == last:
            print(f"ingested: {len(rows)} traces, {sum(n for _, n, _ in seen.values())} observations, scores complete")
            return
        last = seen
        time.sleep(settle)
    print("warning: ingestion still settling; re-run the cells from the weekly pull on")


def make_agent(failures=()):
    llm = OracleChatModel() if DRY_RUN else None
    return SupportAgent(failures=FailureConfig.from_names(failures), llm=llm,
                        guardrail_fn=observed_guardrail, search_fn=observed_search)


print("release:", RELEASE, "| human config:", HUMAN, HUMAN_CONFIG_ID)

# %% [markdown]
# ## Run: last week (v1), then this week (v2 ships `slow_tool`)
# The same 7 requests twice, about 28 Azure calls in total (the injection is blocked before any LLM call). Each
# window's reviews are written **inside** the window: score views count a score in the window of its own timestamp.
# The window boundaries are what the weekly pull receives.

# %%
WINDOWS, ROWS, REVIEWED = {}, [], set()
for window, version, failures in traffic.ANALYTICS_WINDOWS:
    start = datetime.now(timezone.utc)
    agent = make_agent(failures)
    rows = [traced_turn(c, agent) for c in CASES if c.window == window]
    REVIEWED |= human_review(rows)
    langfuse.flush()
    time.sleep(3)  # keep the two windows apart
    WINDOWS[window] = (start, datetime.now(timezone.utc))
    ROWS += rows
    print(f"window {window} ({version}{', ' + ', '.join(failures) if failures else ''}): "
          f"{WINDOWS[window][0]:%H:%M:%S} -> {WINDOWS[window][1]:%H:%M:%S}")
    for r in rows:
        print(f"  {r['case'].case_id:<20} -> {r['handoff']:<19} {'ok ' if r['ok'] else 'BAD'} "
              f"judge {'ok ' if r['judge_ok'] else 'BAD'} {r['level']:<8} {r['elapsed']:5.1f} s")
wait_until_ingested(ROWS, REVIEWED)
PREV, CURR = WINDOWS["prev"], WINDOWS["curr"]
SCOPE = weekly_pull.Scope(tags=(RUN_TAG,), environment=ENV, prefix=PREFIX)

# %% [markdown]
# ### Given: what the Metrics API can and can't group by
# `weekly_pull.query(langfuse, view, metrics, window=…, dimensions=…, filters=…, order_by=…, row_limit=…)` is one
# Metrics API v2 call (module 03 wrote the same thing for cost). Two quick queries over both windows:
# grouping by the agent **version** (a low-cardinality dimension) just works; grouping by **userId** fails until you
# ask for a top-N.

# %%
BOTH = (PREV[0], CURR[1])
RUN_FILTER = [{"column": "tags", "operator": "any of", "value": [RUN_TAG], "type": "arrayOptions"}]
for row in weekly_pull.query(langfuse, "observations", [("count", "count"), ("latency", "p95")], window=BOTH,
                             dimensions=["version"], filters=RUN_FILTER + weekly_pull.ROOTS):
    print(f"version {row['version']}: {row['count_count']} requests, request p95 {row['p95_latency']:,.0f} ms")
try:
    weekly_pull.query(langfuse, "observations", [("totalCost", "sum")], window=BOTH, dimensions=["userId"],
                      filters=RUN_FILTER)
except Exception as exc:
    print("group by userId ->", getattr(exc, "status_code", ""), str(getattr(exc, "body", exc))[:160])
top = weekly_pull.query(langfuse, "observations", [("totalCost", "sum")], window=BOTH, dimensions=["userId"],
                        filters=RUN_FILTER, order_by=[("sum_totalCost", "desc")], row_limit=3)
print("top 3 users by cost:", [(r["userId"], r["sum_totalCost"]) for r in top])

# %% [markdown]
# ## TODO 2: the window's health from the Metrics API
#
# In `ch09/langfuse_mastery/weekly_pull.py`, implement `scope_filters(scope)` and `health_metrics(langfuse, window,
# scope)` (the docstrings list every query). One Metrics API call per question; the results go through your
# `analytics.flatten` / `shares` / `error_rate` into the flat metric keys:
# request count and p50/p95, **p95 per tool**, generation p95, level shares and error rate, total cost and tokens,
# **pass rate per score name × source** (code score, stand-in judge, human ANNOTATION), feedback shares, a
# **per-version** breakdown, and the **top users by cost** (top-N: `row_limit` + `orderBy desc`).
#
# Expected: both windows show 7 requests and the same error rate (2 of 7: the unknown order and the blocked
# injection) and about the same cost (same requests; a GPT-5-family deployment, which runs at its default temperature,
# varies a little more); `latency_ms.tool.lookup_order.p95` jumps from a few ms
# to about 3,000 ms, and `feedback_share.thumbs_down` from 0 to ~0.4 (users notice slow replies). Report every level
# and feedback label, with 0 when absent: a share that is missing last week can't be compared (the docstring says how).

# %%
weekly_pull = importlib.reload(weekly_pull)
HEALTH = {w: weekly_pull.health_metrics(langfuse, win, SCOPE) for w, win in WINDOWS.items()}
keys = sorted(set(HEALTH["prev"]["metrics"]) | set(HEALTH["curr"]["metrics"]))
print(f"{'metric':<44} {'prev':>12} {'curr':>12}")
for k in keys:
    a, b = HEALTH["prev"]["metrics"].get(k), HEALTH["curr"]["metrics"].get(k)
    fmt = lambda v: "–" if v is None else f"{v:,.4g}"
    print(f"{k:<44} {fmt(a):>12} {fmt(b):>12}")
print("by version:", {w: h["by_version"] for w, h in HEALTH.items()})
print("top users (curr):", HEALTH["curr"]["top_users"])
assert HEALTH["prev"]["metrics"]["requests"] == HEALTH["curr"]["metrics"]["requests"] == 7

# %% [markdown]
# ## TODO 3: what the Metrics API can't answer (pull + client-side joins)
#
# "Cost per **resolved** request" needs, per trace, its cost **and** its `handoff_correct` verdict **and** its
# level: three sources joined on the trace id. "Judge vs human" needs both verdicts *on the same trace*. "Cost per
# user" for **every** user is a high-cardinality group-by. The Metrics API does none of these, so:
#
# implement `fetch_observations(langfuse, window, scope)` (every observation of the window, **all pages**) and
# `rollups(langfuse, window, scope)` in `weekly_pull.py`: `analytics.per_trace` → `fetch_scores` (given) →
# `analytics.resolved` → `rollup` by user and by session, `cost_per_resolved`, and the judge–human
# `agreement_rate` (judge = scores that `is_judge`, human = ANNOTATION `scope.prefix + HUMAN`).
#
# Expected: 5 of 7 requests resolved per window (the two WARNING requests are not, even though their routing is
# right); cost per resolved request ≈ 1.4 × cost per request; judge–human agreement **0.5** on 2 pairs per window:
# the control agrees, the stand-in judge's "refund policy → billing" false alarm doesn't. In production that number is
# your judge's credibility.

# %%
weekly_pull = importlib.reload(weekly_pull)
ROLL = {w: weekly_pull.rollups(langfuse, win, SCOPE) for w, win in WINDOWS.items()}
for w, r in ROLL.items():
    print(w, {k: (round(v, 6) if isinstance(v, float) else v) for k, v in r["metrics"].items()})
    for user, row in sorted(r["by_user"].items()):
        print(f"   {user:<8} {row['requests']} request(s)  ${row['cost_usd']:.5f}  resolved {row['resolved']}")

# %% [markdown]
# ## Run the weekly pull, like the cron job would
# `python -m langfuse_mastery.weekly_pull` is a separate process with its own client: it reads Langfuse only and
# writes `weekly-report.json` + `weekly-report.md`. In production a scheduler runs it on Monday morning without
# `--prev/--curr` (`analytics.week_windows`) and posts the Markdown to the team channel.

# %%
REPORT_DIR = OUT / "report"
cmd = [sys.executable, "-m", "langfuse_mastery.weekly_pull", "--tag", RUN_TAG, "--env", ENV, "--prefix", PREFIX,
       "--prev", PREV[0].isoformat(), PREV[1].isoformat(), "--curr", CURR[0].isoformat(), CURR[1].isoformat(),
       "--out", str(REPORT_DIR)]
r = subprocess.run(cmd, cwd=LAB_ROOT, capture_output=True, text=True, timeout=600)
print(f"exit {r.returncode}:", (r.stdout or r.stderr)[-1500:])
REPORT = json.loads((REPORT_DIR / "weekly-report.json").read_text(encoding="utf-8"))
print((REPORT_DIR / "weekly-report.md").read_text(encoding="utf-8")[:3000])

# %% [markdown]
# ## TODO 4: the agent-health dashboard, as code
#
# Custom dashboards are a public (unstable) API on this server: `langfuse.api.unstable.dashboard_widgets`
# (`list(page=, limit=)`, `create`, `update`) and `langfuse.api.unstable.dashboards` (`list`, `create`, `get`,
# `update`, `add_placement`). The spec is data: `langfuse_lab.dashboard_spec.WIDGETS` (7 widgets) and
# `dashboard_spec.dashboard_filters(ENV)`.
#
# Write:
# - `ensure_widget(spec) -> widget id`: get-or-create by name `dashboard_spec.widget_name(spec, PREFIX)` (names are
#   not unique: list all pages and pick yours). Create with `name=`, `description=`, `view=spec["view"]`,
#   `dimensions=[{"field": d}, …]`, `metrics=[{"measure": m, "agg": a}, …]`, `filters=spec["filters"]`,
#   `chart_type=spec["chart_type"]`. If it exists but `dashboard_spec.widget_problems(w.dict(), spec, prefix=PREFIX)`
#   is not empty, `update(w.id, …)` it: the spec in code wins over hand edits.
# - `ensure_dashboard() -> Dashboard`: get-or-create `PREFIX + dashboard_spec.DASHBOARD` with the description and the
#   dashboard-level `filters=dashboard_spec.dashboard_filters(ENV)` (so judge and experiment traces never leak in;
#   update them if they differ), then `add_placement(dashboard_id, request={"type": "widget", "widgetId": …,
#   "x": …, "y": …, "width": 6, "height": 6})` for every widget **not yet placed** (`dashboard.definition.widgets`,
#   each with a `.widget_id`). Return the dashboard re-read with `get`.
#
# The cell runs it twice: the second run must create nothing.

# %%
# TODO 4: your code here

# %%
DASH = ensure_dashboard()
again = ensure_dashboard()
assert again.id == DASH.id and len(again.definition.widgets) == len(DASH.definition.widgets) == len(dashboard_spec.WIDGETS), \
    "the second run changed the dashboard: get-or-create, and only place widgets that aren't placed yet"
print(f"{DASH.name}: {len(DASH.definition.widgets)} widgets, filters {DASH.filters}")
print(f"open it: {UI}/project/{PROJECT_ID}/dashboards/{DASH.id}  (set the time range to 'Past 1 hour')")

# %% [markdown]
# ## TODO 5: export
#
# Three ways out of Langfuse, from most to least portable:
#
# **5a, the public API pull (portable: works everywhere, any plan).** Write `export_jsonl(folder, window) -> dict`:
# every observation of the run in `window` (reuse your `weekly_pull.fetch_observations`, scope `SCOPE`) to
# `folder/observations.jsonl`, and every score of those traces (`langfuse.api.scores.get_many(trace_id=…,
# fields="details,subject", limit=100, cursor=…)`, follow the cursor until a page comes back empty or without a
# cursor) to `folder/scores.jsonl`. One JSON object per line (`json.dumps(…, default=str)`: datetimes). Return the
# counts. This is what you'd feed a warehouse or a notebook of your own.
#
# **5b, the blob storage integration (scheduled, optional).** Langfuse can push the project's data to S3-compatible
# storage on a schedule; this stack has MinIO. The endpoint needs an **organization** API key (the project key gets
# 403), and **self-hosted OSS cannot issue one**: org keys need the Enterprise `admin-api` entitlement, so the
# "Organization settings → API keys" page is hidden and every org-key call is refused (module 13 shows the live 403).
# On this stack, read 5b and skip it; the public API pull above is your export. On Langfuse Cloud or with an
# Enterprise license, put the org key in `.env` as `LANGFUSE_ORG_PUBLIC_KEY` / `LANGFUSE_ORG_SECRET_KEY` and
# write `configure_blob_export()`: `LangfuseAPI(base_url=UI, username=…, password=…)` then
# `.blob_storage_integrations.upsert_blob_storage_integration(project_id=PROJECT_ID, type="S3_COMPATIBLE",
# bucket_name=m.bucket, endpoint=m.internal_endpoint, region="auto", access_key_id=m.access_key,
# secret_access_key=m.secret_key, prefix=s3.BLOB_EXPORT_PREFIX, export_frequency="hourly", enabled=True,
# force_path_style=True, file_type="JSONL", export_mode="FROM_TODAY", export_source="OBSERVATIONS_V2")` with
# `m = s3.minio_settings()`. The **internal** endpoint (`http://minio:9000`) is the one the Langfuse containers
# reach; Langfuse test-uploads a file when you save. Without the keys, print that it's skipped.
#
# **5c, the UI batch export (optional).** Traces → filter → Export. It writes to MinIO `exports/`, but this stack's
# compose file ships with `LANGFUSE_S3_BATCH_EXPORT_ENABLED: "false"`; the verify hint says how to switch it on.

# %%
# TODO 5: your code here (export_jsonl, configure_blob_export)

# %%
EXPORT_DIR = OUT / "export"
print("exported:", export_jsonl(EXPORT_DIR, (PREV[0], CURR[1])), "->", EXPORT_DIR)
print("blob storage integration:", configure_blob_export())
print("MinIO now holds:", {p: len(s3.list_objects(p)) for p in (s3.BLOB_EXPORT_PREFIX, s3.BATCH_EXPORT_PREFIX)})

# %% [markdown]
# Verify reads what Langfuse stored and recomputes it:
# - the two windows hold the 7 + 7 requests (v1, then v2 with `slow_tool`), each inside the report's window, not dry;
# - your **Metrics API numbers** match the raw data (requests, error rate, level shares, cost, pass rates per score
#   × source, tool p95, the top user);
# - your **client-side rollups** match (per user and session, resolved rate, cost per resolved request,
#   judge–human agreement);
# - the **dashboard** matches the spec, once, with the environment filter, every widget placed once;
# - **planted:** its tool-latency widget, run on this run's traffic, shows a lookup tool's p95 v2 ≥ v1 + 2 s;
# - the JSONL export holds every observation and score of the run;
# - blob storage integration and UI batch export: ⚠️ unless you set them up (optional);
# - **planted:** the weekly report flags the latency regression.

# %%
verify.check_module_11(RUN_TAG, report=REPORT, export_dir=EXPORT_DIR, prefix=PREFIX, environment=ENV)

# %% [markdown]
# ## UI tour
# - [ ] **Home → Dashboards → Langfuse Dashboard** (built in): traces, latency percentiles, model cost, scores. Set
#   the environment filter to `dev`: these are the charts you get for free, with no code.
# - [ ] **Dashboards → `agent-health`**: time range *Past 1 hour*. *Tool latency p50/p95 by version* shows
#   `lookup_order` / `lookup_shipment` at ~3,000 ms on v2 and a few ms on v1. *Requests by level* shows the WARNING
#   share; *Pass rate by score and source* puts `routing_judge` (API) next to `human_routing_ok` (ANNOTATION).
# - [ ] On the dashboard, **add a widget by hand** (e.g. *Generation latency p95 by version*), then re-run TODO 4:
#   your code only touches its own widgets. Edit one of yours in the UI (change the chart type) and re-run TODO 4: it
#   is put back (the spec in code wins).
# - [ ] **Traces**: filter `tags = <your run tag>`, add the *Latency* and *Scores* columns, sort by latency: the v2
#   lookups are on top. That's the drill-down from a chart to the evidence.
# - [ ] **Scores** (left nav): filter `source = ANNOTATION`, then `name = routing_judge`: the same verdicts the
#   report's judge–human agreement is computed from.
# - [ ] **Traces → Export** (if you enabled batch export): download the JSON and compare it with your JSONL export.
# - [ ] **Settings → Integrations → Blob storage** (org admin): if you configured 5b, its last sync time.
#
# ## Production notes
# - **Dashboards as code.** A dashboard someone clicked together is lost with them. Keep widget specs in the repo,
#   apply them with get-or-create + update (TODO 4), filter every dashboard by environment so judge, experiment and
#   staging traffic never pollute production charts.
# - **Know the Metrics API's limits.** It aggregates fast and in real time, but only by low-cardinality dimensions
#   (top-N for users/sessions/traces) and never joins scores with costs per trace. For "cost per resolved request",
#   per-customer tables and judge agreement, pull rows (cursor paging) and aggregate yourself, or export to a
#   warehouse.
# - **Percentiles, not averages.** One slow tool hides in an average request latency; p95 per observation name finds
#   it. Alert on p95/p99 per tool (module 12).
# - **Mind the time axis.** Observations are bucketed by start time, scores by **their own** timestamp: a review
#   backlog cleared on Monday moves last week's quality into this week. Report both "scored this week" and
#   "traffic of this week" if reviews lag.
# - **Rules before eyeballs.** The weekly report flags only what crosses explicit thresholds (`DEFAULT_RULES`): an
#   absolute floor stops 1 ms → 3 ms from paging anyone, a relative one stops normal growth from looking like a
#   regression. Tune them per metric; review the false alarms monthly.
# - **Exports are data products.** The blob storage integration gives finance and data teams the raw rows on a
#   schedule (needs an org key, so Cloud or Enterprise only; secrets stay in the integration). The public API pull is the portable fallback; keep
#   the exported schema versioned. Exports contain inputs/outputs: mask them first (module 05).
# - **Why it matters (leadership):** one link answers "is the agent healthy?", and every Monday a report says what
#   got worse, by how much, and in which version, before a customer does. Quality, speed and cost per resolved
#   request become numbers we manage, not anecdotes.
