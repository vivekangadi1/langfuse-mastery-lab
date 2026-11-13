# %% [markdown]
# # Module 13: Production ops (separation, access, keys, retention, erasure, SDK robustness, scale-out)
#
# **Goal:** modules 01–12 made the agent observable, evaluated and alerting. Before it goes live, the platform team
# owns a different set of questions, and this module answers each one with data, code and a measurement:
# - **What is separated from what?** Environments vs projects vs organizations, and what each boundary does NOT isolate.
# - **Who may do what?** Organization and project roles, API keys per workload, least privilege, key rotation.
# - **How long does data live?** Retention per environment, and who enforces it on this edition.
# - **How is a customer forgotten?** GDPR erasure across environments, with an asynchronous delete that has to be
#   verified, not assumed.
# - **What does the SDK do to the app?** Batching, retries, timeouts, flushing, and what happens when Langfuse is
#   down or the process is killed.
# - **How does it scale?** Web, worker, ClickHouse, Postgres, Redis, S3 (on paper + what the stack shows live).
#
# | Concept | What it is | In this module |
# |---|---|---|
# | **Environment** | a label on traces, observations, scores, sessions (`production`, `staging`); a filter in the UI and APIs | both deployments write to one project, separated only by it |
# | **Project** | the isolation boundary: its own data, API keys, prompts, datasets, configs, settings | your plan puts production in its own project |
# | **Organization** | holds projects and members; on OSS a member's org role applies to **every** project | your plan uses a 2nd org to give people different roles |
# | **Role** | OWNER > ADMIN > MEMBER > VIEWER > NONE | `ops.can(role, action)` |
# | **Project API key** | one project; permission Full access / Viewer / **Ingestion** / Scores ingestion; optional expiry | one key per workload, least privilege |
# | **Retention** | delete data older than N days; **native: Enterprise**, on OSS: a deletion job | `ops.expired` (dry run) |
# | **Trace deletion** | `api.trace.delete` / `delete_multiple`: accepted at once, **executed by the worker every ~2 min** | GDPR erasure, verified |
# | **SDK batching** | spans queue in a background thread; exported every `flush_interval` s or `flush_at` spans; `timeout` = retry budget per batch | 7 deployments measured through a tap |
#
# ```
#  support-api@production ─┐                          ┌─ environment=production ─┐
#  support-api@staging ────┼─► ch09-eval-project ─────┼─ environment=staging ────┼─► one key, one prompt, one set of members
#  7 SDK-lab deployments ──┘  (through the OTLP tap)  └─ environment=sdk-lab ────┘
#        │ planted: Langfuse down 6 s, a killed job                  │
#        └─► ops.delivery_report (TODO 1)                             └─► your plan (TODO 2) says how it SHOULD be split
#  erasure (TODO 4): find user ─► guarded delete ─► wait for the worker ─► re-check (planted straggler) ─► erase again
# ```
#
# **The planted failures:**
# 1. **Langfuse is down for 6 s** (the tap answers 503) while a deployment serves 30 requests: the request path must
#    not notice, your report must name exactly the traces the default retry budget lost, and a hardened config must
#    lose none.
# 2. **A batch job is killed** (`os._exit`: an OOM kill, a frozen Lambda) before its buffer is exported; the same job
#    ending with your `finish()` delivers everything.
# 3. **A straggler:** the customer you erase still has a trace in another process's SDK buffer. It arrives after the
#    deletion and re-creates their data. Your erasure procedure must catch it.
#
# Things verified live on this server (4.77 / SDK 5.2) and in its source (tag v4.77.0) that shape the module:
# - This stack is the **open-source edition**. Enterprise-only: organization API keys and the organization management
#   API (projects, keys, memberships via API), SCIM, project-level roles, native data retention, audit logs,
#   protected prompt labels. The project key gets **403** on every organization endpoint.
# - **Project API key permissions exist on OSS**: Full access, Viewer, Ingestion (writes traces/scores/media, reads
#   nothing, not even prompts), Scores ingestion.
# - Trace deletion is asynchronous: the API answers in ~50 ms, the worker deletes all pending ids **every 2 minutes**
#   (observations, scores and the raw events in S3 together), so a deletion shows up after up to ~2.5 minutes. Data
#   that arrives **after** the deletion is stored normally.
# - SDK `timeout` (default 5 s) is the exporter's whole retry budget per batch (backoff 1, 2, 4, 8 s …); after it the
#   batch is **dropped**. The client flushes on a normal exit (`atexit`), never on `os._exit`/SIGKILL.
#
# Docs: https://langfuse.com/docs/administration/rbac · https://langfuse.com/docs/observability/features/environments ·
# https://langfuse.com/docs/administration/data-retention · https://langfuse.com/docs/administration/data-deletion ·
# https://langfuse.com/self-hosting · https://langfuse.com/self-hosting/scaling · https://api.reference.langfuse.com

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
from collections import Counter
from datetime import datetime, timedelta, timezone
from langfuse import get_client
from langfuse_lab import verify, traffic, kb, prompts, ops_lab
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_lab.oracle_llm import OracleChatModel
from langfuse_lab.otlp_tap import Tap
from langfuse_mastery import ops, ops_plan

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
langfuse = get_client()
RUN_TAG = f"run:m13-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
OUT = LAB_ROOT / "langfuse_mastery" / "ops_out" / RUN_ID  # git-ignored: the tap's log, worker results
OUT.mkdir(parents=True, exist_ok=True)
BASE_URL = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")

# Prompts are PROJECT-GLOBAL. Keep PREFIX empty.
PREFIX = ""
# True = rehearse with the oracle LLM (no Azure calls). verify refuses dry traffic, so the graded run is False.
DRY_RUN = os.getenv("OPS_DRY_RUN") == "1"

TAG = PREFIX + ops_lab.OPS_TAG     # on every trace of this module
PROMPT = PREFIX + prompts.PROMPT_NAME
fixtures = load_fixtures()
CASES = traffic.ops_cases(run_id=RUN_ID)
SUBJECT = traffic.ops_subject(RUN_ID)  # the customer who will ask to be forgotten (TODO 4)
print("run tag:", RUN_TAG, "| dry run:", DRY_RUN, "| requests:", len(CASES), "| GDPR subject:", SUBJECT)
for c in CASES:
    print(f"  {c.deployment:<23} {c.user_id:<20} expect {c.expected_handoff:<19} {c.message}")

# %% [markdown]
# ## Given: what this edition can do (and what your plan must work around)
# Langfuse gates features by **entitlements** per plan. This stack has no Enterprise license, so it runs plan `oss`.
# The cell prints what that rules out and the OSS workaround for each, then asks the server: the **project** key (the
# one in `.env`) can read its own project, including its retention setting, and gets **403** from every organization
# endpoint. On Enterprise, an **organization** key manages projects, keys and members through the API (and SCIM);
# on OSS such a key cannot even be created.

# %%
EDITION, WHY_EDITION = ops_lab.stack_edition()
ENTITLEMENTS = ops_lab.ENTITLEMENTS[EDITION]
print(f"edition: {EDITION} ({WHY_EDITION})\nentitlements: {sorted(ENTITLEMENTS)}\n")
for feature, (entitlement, means, workaround) in ops_lab.ENTERPRISE_ONLY.items():
    print(f"- {feature}  [{entitlement}]\n    on OSS: {means}\n    instead: {workaround}")
for feature, note in ops_lab.CLOUD_ONLY.items():
    print(f"- {feature}  [Cloud only]: {note}")

project = langfuse.api.projects.get().data[0]
PROJECT_ID = project.id
print(f"\nthe .env key's project: {project.id} ({project.name}) in organization {project.organization.id}, "
      f"retention_days={project.retention_days}")
for name, call in {"organization projects": lambda: langfuse.api.organizations.get_organization_projects(),
                   "organization members": lambda: langfuse.api.organizations.get_organization_memberships(),
                   "this project's API keys": lambda: langfuse.api.projects.get_api_keys(PROJECT_ID),
                   "SCIM users": lambda: langfuse.api.scim.list_users(count=1)}.items():
    try:
        call()
        print(f"  {name}: answered")
    except Exception as exc:
        body = getattr(exc, "body", None)
        msg = (body.get("error") or body.get("message") or body.get("detail")) if isinstance(body, dict) else exc
        print(f"  {name}: {getattr(exc, 'status_code', '?')} {msg}")

# %% [markdown]
# ## Given: the stack you would scale (read-only)
# `docker ps` / `docker stats` and the health endpoints, annotated with each component's job. Two stateless services
# (**web**: UI, public API and the OTLP endpoint your SDK posts to; **worker**: queues for ingestion, evaluations,
# deletions, exports and monitors) in front of four stateful ones (**ClickHouse** for traces/observations/scores,
# **Postgres** for everything else, **Redis** for the queues, **S3/MinIO** for raw events and media). Scale-out is in the
# production notes at the end; nothing here changes the stack.

# %%
TOPOLOGY = ops_lab.topology()
for c in TOPOLOGY["containers"]:
    if "error" in c:
        print("docker:", c["error"])
        continue
    kind = "stateful " if c["stateful"] else "stateless"
    print(f"{c['name']:<26} {kind} cpu {c['cpu'] or '?':>7}  mem {c['mem'] or '?':<22} {c['role']}")
    print(f"{'':<26} scales: {c['scales']}")
for name, status in TOPOLOGY["health"].items():
    print(f"health {name:<10} {status}")

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/ops.py` (pure functions + pytest)
#
# Open `ch09/langfuse_mastery/ops.py`. Its top half is given: the roles, what each **action** needs
# (`ROLE_ACTIONS`, from the server's role table), what each **API key permission** grants (`KEY_PERMISSIONS`), the
# environment-name rule. Implement:
# - `effective_role(org_role, project_role, *, entitlements)`: a member's role in one project (a project role only
#   overrides the org role where project-level roles exist);
# - `can(role, action)` and `least_role(actions)`;
# - `lint_plan(plan, *, entitlements)`: every problem of a separation plan, as `"<code>: <detail>"` (the docstring
#   lists the ten codes: names, deployments, production isolation, keys, key grants, delete rights, team needs, team
#   excess, retention, Enterprise features);
# - `expired(rows, policy, *, now)`: what a retention job run at `now` deletes;
# - `delivery_report(sent, exports, stored, *, spans_per_request)`: what one deployment's SDK did to the request path
#   and to the data (attempts, batches, drops, delivered / partial / lost traces).
#
# The cell runs the tests and reloads your module. Re-run it until it shows `34 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_ops.py", "-q",
                         "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
ops = importlib.reload(ops)

# %% [markdown]
# ## TODO 2: write the separation plan (`langfuse_mastery/ops_plan.py`)
#
# The design's deliverable: a **prod/staging separation plan with retention settings**, as data you can review, test
# and diff. `ops_plan.py`'s docstring has the team's requirements and the plan's shape; the skeleton is empty. Fill
# in organizations, projects (with each environment's retention), deployments (environment, tier, release, the keys
# they use, what they need), keys (project, permission, owner, rotation) and teams (roles, what they need where).
#
# Two things the linter will teach you on OSS:
# - **one organization over-privileges people:** without project-level roles, a member's org role applies to every
#   project, so "MEMBER in staging, VIEWER in production" and "contractors never see production" need a second
#   organization (`team-excess`);
# - **no single key permission covers the app:** it writes traces (Ingestion) and reads its prompt (Viewer); Full
#   access would also let a leaked key delete data (`key-delete`).
#
# The cell runs `tests/test_ops_plan.py` (it uses your TODO 1), then prints who can do what where. Re-run it until it
# shows `7 passed`. The production project is the one this lab runs in (`ch09-eval-project`); the non-production
# project doesn't exist yet (`"id": None`; TODO 5 creates it).

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_ops_plan.py", "-q",
                         "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
ops_plan = importlib.reload(ops_plan)
PLAN = ops_plan.PLAN
try:
    print("lint:", ops.lint_plan(PLAN, entitlements=ENTITLEMENTS) or "clean")
    org_of = {p: o for o, v in PLAN["organizations"].items() for p in v.get("projects", [])}
    print(f"\n{'team':<15}" + "".join(f"{p:<18}" for p in PLAN["projects"]))
    for team, t in PLAN["teams"].items():
        roles = [ops.effective_role(t.get("roles", {}).get(org_of.get(p), "NONE"), t.get("project_roles", {}).get(p),
                                    entitlements=ENTITLEMENTS) for p in PLAN["projects"]]
        print(f"{team:<15}" + "".join(f"{r:<18}" for r in roles))
    print()
    for name, d in PLAN["deployments"].items():
        grants = set().union(*(ops.KEY_PERMISSIONS[PLAN["keys"][k]["permission"]] for k in d["keys"]))
        print(f"{name:<24} -> {d['project']}/{d['environment']:<11} keys {d['keys']} grant {sorted(grants)}")
    print("\nretention per environment:", ops.retention_policy(PLAN))
except (NotImplementedError, KeyError) as exc:
    print(f"finish TODO 1 and TODO 2 first ({type(exc).__name__}: {exc})")

# %% [markdown]
# ## Given: the instrumentation and the prompt (modules 01–06 recap)
# This is a recap, not a TODO:
# - **The prompt (module 04).** `ensure_prompt` gets the good version of `support-agent-system` **by content**
#   (creates it only when missing) and sets `production` only when no version has it. Both deployments serve
#   `label="production"`: prompts are project-global, there is no "staging copy" unless you make one.
# - `traced_turn(case, agent)`: one `support-request` trace per request, as the case's **deployment**: its environment
#   and release from your plan. This kernel plays both deployments with ONE client, so it sets them per trace:
#   `propagate_attributes(environment=, version=)` (a trace's `version`; `release` exists only per client). A real
#   deployment sets both once, at startup: `LANGFUSE_TRACING_ENVIRONMENT` / `LANGFUSE_RELEASE` or
#   `Langfuse(environment=, release=)` (TODO 3). Plus a run-scoped user, the tags, the prompt link and a
#   `handoff_correct` score.

# %%
from langfuse import observe, propagate_attributes
from langfuse.api import NotFoundError
from langfuse.langchain import CallbackHandler

SCORE = ops_lab.SCORE_NAME


def ensure_prompt(template):
    """Get-or-create the version with this text and module 04's config (create_prompt never dedupes)."""
    metas = [m for m in langfuse.api.prompts.list(name=PROMPT, limit=100).data if m.name == PROMPT]
    for v in sorted({v for m in metas for v in (m.versions or [])}):
        row = langfuse.api.prompts.get(PROMPT, version=v)
        if row.prompt == template and all((row.config or {}).get(k) == val for k, val in prompts.PROMPT_CONFIG.items()):
            return v
    return langfuse.create_prompt(name=PROMPT, prompt=template, type="text", config=prompts.PROMPT_CONFIG,
                                  commit_message="module 13: get-or-create by content").version


GOOD = ensure_prompt(prompts.SYSTEM_PROMPT_TEMPLATE)
try:
    langfuse.api.prompts.get(PROMPT, label="production")
except NotFoundError:
    langfuse.update_prompt(name=PROMPT, version=GOOD, new_labels=["production"])


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


@observe(name="support-request", as_type="agent", capture_input=False, capture_output=False)
def traced_turn(case, agent):
    """One request = one trace, as the case's deployment (environment + release from the plan)."""
    dep = PLAN["deployments"][case.deployment]
    prompt = langfuse.get_prompt(PROMPT, label="production", cache_ttl_seconds=0)
    tags = [RUN_TAG, TAG, *case.tags, *(["dry-run"] if DRY_RUN else [])]
    with propagate_attributes(session_id=case.session_id, user_id=case.user_id, tags=tags,
                              environment=dep["environment"], version=dep["release"], prompt=prompt):
        run = agent.invoke(case.message, config={"callbacks": [CallbackHandler()]},
                           system_prompt=prompt.compile(**prompts.PROMPT_VARIABLES), llm_params=prompt.config)
        actual = run.summary["handoff_target"]
        langfuse.update_current_span(input=case.message, output=run.summary["final_answer"],
                                     metadata={"handoff_target": actual})
        ok = actual == case.expected_handoff
        langfuse.score_current_trace(name=SCORE, value=1.0 if ok else 0.0, data_type="BOOLEAN",
                                     comment=f"expected {case.expected_handoff}, got {actual}",
                                     metadata={"evaluator": "code:v1"})
        return {"trace_id": langfuse.get_current_trace_id(), "user_id": case.user_id, "deployment": case.deployment,
                "environment": dep["environment"], "case_id": case.case_id, "prompt_version": prompt.version,
                "handoff": actual, "ok": ok}


def wait_until_ingested(rows, *, timeout=180, settle=4):
    """Every trace's root and its score, and stable observation counts."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        seen = {}
        for r in rows:
            o = langfuse.api.observations.get_many(trace_id=r["trace_id"], fields="core", limit=200).data
            sc = langfuse.api.scores.get_many(trace_id=r["trace_id"], limit=50).data
            seen[r["trace_id"]] = (any(x.parent_observation_id is None for x in o), len(o), len(sc) >= 1)
        if all(root and scored for root, _, scored in seen.values()) and seen == last:
            print(f"ingested: {len(rows)} traces, {sum(n for _, n, _ in seen.values())} observations, scores complete")
            return
        last = seen
        time.sleep(settle)
    print("warning: ingestion still settling; wait a little")


print(f"{PROMPT}: production serves v{langfuse.get_prompt(PROMPT, label='production', cache_ttl_seconds=0).version}")

# %% [markdown]
# ## Run: two deployments, one project
# The same 3 requests served by `support-api@production` and `support-api@staging` (6 agent turns, about 16 Azure
# calls). Today both write to `ch09-eval-project`, separated only by **environment**: where most teams start, and
# what your plan moves away from.

# %%
missing = [d for d in traffic.OPS_DEPLOYMENTS if d not in PLAN.get("deployments", {})]
if missing:
    raise RuntimeError(f"your plan has no deployment(s) {missing}: finish TODO 2 first")
agent = SupportAgent(settings, llm=OracleChatModel() if DRY_RUN else None, guardrail_fn=observed_guardrail,
                     search_fn=observed_search)
TRAFFIC = [traced_turn(c, agent) for c in CASES]
langfuse.flush()
for r in TRAFFIC:
    print(f"  {r['deployment']:<23} {r['environment']:<11} {r['user_id']:<20} prompt v{r['prompt_version']} "
          f"-> {r['handoff']:<19} {'ok' if r['ok'] else 'WRONG TEAM'}  {r['trace_id'][:8]}")
wait_until_ingested(TRAFFIC)

# %% [markdown]
# ### Given: what the environment separates, and what it doesn't
# Per environment through the Metrics API (filter `environment`): requests, routing pass rate, cost. Then what both
# environments share. An environment is a **label**: it gives you clean views and the UI's environment filter, but the
# same API key wrote both, both served the same prompt version (a staging experiment that moves `production` moves it
# for production too), and every member sees both.

# %%
def env_report(rows):
    frm, to = datetime.now(timezone.utc) - timedelta(minutes=30), datetime.now(timezone.utc) + timedelta(minutes=1)
    out = {}
    for env in sorted({r["environment"] for r in rows}):
        flt = [{"column": "tags", "operator": "any of", "value": [RUN_TAG], "type": "arrayOptions"},
               {"column": "environment", "operator": "=", "value": env, "type": "string"}]
        q = lambda view, metrics, extra=(): langfuse.api.metrics.metrics(query=json.dumps({  # noqa: E731
            "view": view, "metrics": [{"measure": m, "aggregation": a} for m, a in metrics], "dimensions": [],
            "filters": flt + list(extra), "fromTimestamp": frm.isoformat(), "toTimestamp": to.isoformat()})).data[0]
        n = q("observations", [("count", "count")], [
            {"column": "isRootObservation", "operator": "=", "value": True, "type": "boolean"},
            {"column": "name", "operator": "=", "value": ops_lab.ROOT_NAME, "type": "string"}])["count_count"]
        cost = q("observations", [("totalCost", "sum")])["sum_totalCost"]
        rate = q("scores-boolean", [("value", "avg")], [
            {"column": "name", "operator": "=", "value": SCORE, "type": "string"}])["avg_value"]
        out[env] = {"requests": n, "pass_rate": rate, "cost_usd": cost}
    return out


for env, m in env_report(TRAFFIC).items():
    print(f"{env:<11} {m['requests']} requests, pass rate {m['pass_rate']}, cost ${m['cost_usd'] or 0:.5f}")
print("shared by both environments:",
      {"prompt versions": sorted({r["prompt_version"] for r in TRAFFIC}), "project": PROJECT_ID,
       "API key": os.environ["LANGFUSE_PUBLIC_KEY"][:12] + "…"})

# %% [markdown]
# ## TODO 3: one deployment = one process with its own client (`langfuse_mastery/_ops_worker.py`)
#
# A deployment configures its Langfuse client **once, at startup**, and the first client of a process wins (module
# 05's singleton trap). So every deployment in this lab is its own process: `_ops_worker.py`. Its request handler is
# given (3 cheap observations per request, no LLM). Implement:
# - `make_client(cfg)`: `Langfuse(...)` from the payload's `client` dict: `public_key`, `secret_key`, `base_url`,
#   `environment`, `release`, `flush_at`, `flush_interval`, `timeout`, `sample_rate`. Pass only the keys that are
#   present and not None (missing = the SDK default or the `LANGFUSE_*` variable).
# - `finish(langfuse)`: what a job (or a server's shutdown hook) calls before the process exits, so nothing still
#   buffered is lost and the background threads stop.
#
# The cell below starts one tiny deployment straight against Langfuse and prints what it sent.

# %%
smoke = ops_lab.start_worker({"run_id": RUN_ID, "scenario": "smoke", "n": 2, "exit": "finish",
                              "tags": [RUN_TAG, TAG, "sdk:smoke"], "user_id": f"sdk-{RUN_ID}",
                              "client": {"base_url": BASE_URL, "environment": ops_lab.SDK_ENVIRONMENT,
                                         "release": "sdk-lab"}})
try:
    out = smoke.result(timeout=120)
    print(f"smoke: {len(out['trace_ids'])} requests, latencies {[round(x, 2) for x in out['latencies_ms']]} ms, "
          f"finish() took {out['exit_s']:.2f} s")
except RuntimeError as exc:
    print(str(exc)[-800:])

# %% [markdown]
# ### The SDK lab: 7 deployments through a tap
# A **tap** (`langfuse_lab.otlp_tap`) sits between each deployment and Langfuse: it forwards everything and logs every
# span export (how many spans, which traces, the status, and a hash of the body: a retry resends the same bytes). It
# can also play an **outage** (503). Each scenario is one process (`ops_lab.SDK_SCENARIOS`):
#
# | scenario | client | what it shows |
# |---|---|---|
# | `defaults` | nothing set | one batch at the end (flush_at 512, flush_interval 5 s) |
# | `tuned` | `flush_at=6, flush_interval=0.5` | many small batches while serving |
# | `flush-per-request` | defaults, `flush()` in every request | the request path pays a network round trip |
# | `outage` (planted) | tuned, default `timeout` (5 s) | Langfuse down 6 s: a batch exhausts its retry budget |
# | `outage-hardened` | tuned + `timeout=20` | the same outage, retried through |
# | `killed-job` (planted) | `flush_interval=60`, dies with `os._exit` | the buffer dies with the process |
# | `job` | `flush_interval=60`, ends with `finish()` | everything leaves before exit |
#
# The cell takes about 40 s, then waits for ingestion and runs **your** `ops.delivery_report` per scenario.

# %%
TAP = Tap(OUT / "otlp_exports.jsonl", upstream=BASE_URL).start()
SDK = {}
for sc in ops_lab.SDK_SCENARIOS:
    handle = ops_lab.start_worker({
        "run_id": RUN_ID, "scenario": sc["name"], "n": sc["n"], "pace_s": sc["pace_s"],
        "flush_each": sc.get("flush_each", False), "exit": sc["exit"], "user_id": f"sdk-{RUN_ID}",
        "tags": [RUN_TAG, TAG, f"sdk:{sc['name']}"],
        "client": {"base_url": TAP.url, "environment": ops_lab.SDK_ENVIRONMENT, "release": "sdk-lab", **sc["client"]}})
    if sc.get("outage"):
        TAP.outage(sc["outage"]["seconds"], delay=sc["outage"]["delay_s"])  # the planted outage
    SDK[sc["name"]] = {"sent": handle.result(timeout=180)}
    warn = [ln for ln in SDK[sc["name"]]["sent"]["stderr"] if "export" in ln.lower()]
    print(f"{sc['name']:<18} served {sc['n']} requests in {SDK[sc['name']]['sent']['served_s']:.1f} s"
          + (f" | SDK log: {warn[-1][:110]}" if warn else ""))


def stored_counts():
    """Observations per trace id of this run's sdk-lab environment (all pages)."""
    flt = json.dumps([{"type": "arrayOptions", "column": "traceTags", "operator": "any of", "value": [RUN_TAG]}])
    counts, cursor = Counter(), None
    while True:
        resp = langfuse.api.observations.get_many(fields="core,basic", filter=flt, limit=1000, cursor=cursor)
        counts.update(o.trace_id for o in resp.data if o.environment == ops_lab.SDK_ENVIRONMENT)
        cursor = resp.meta.cursor
        if not cursor:
            return counts


last, stable = None, 0
for _ in range(30):  # every worker has exited, so nothing is in flight: wait until the counts stop moving
    time.sleep(5)
    now_counts = stored_counts()
    stable = stable + 1 if now_counts == last else 0
    last = now_counts
    if stable >= 2:
        break
STORED = dict(last)
print(f"\n{'scenario':<18} {'p50 ms':>7} {'p95 ms':>7} {'attempts':>8} {'batches':>7} {'failed':>6} {'dropped':>7} "
      f"{'delivered':>9} {'lost':>4}")
for name, s in SDK.items():
    try:
        s["report"] = rep = ops.delivery_report(s["sent"], TAP.exports(s["sent"]["trace_ids"]), STORED,
                                                spans_per_request=ops_lab.SPANS_PER_REQUEST)
    except NotImplementedError as exc:
        print(f"finish TODO 1 first ({exc})")
        break
    print(f"{name:<18} {rep['p50_ms']:>7.2f} {rep['p95_ms']:>7.2f} {rep['attempts']:>8} {rep['batches']:>7} "
          f"{rep['failed_attempts']:>6} {rep['dropped_batches']:>7} {rep['delivered']:>6}/{rep['requests']:<2} "
          f"{len(rep['lost']):>4}")

# %% [markdown]
# Read the table like an SRE:
# - `defaults` vs `tuned`: the same data, different freshness and request count. `flush-per-request`: the request
#   path's p50 is a network round trip. Never flush per request in a server; flush once at the end of a job.
# - `outage`: the request path did **not** slow down (export runs in a background thread), but a batch was dropped
#   after its 5 s retry budget: those traces are gone, silently. `outage-hardened` survived the same outage. More
#   `timeout` buys resilience, and costs memory (the queue holds spans meanwhile, max 2048) and a longer `flush()` at
#   shutdown.
# - `killed-job`: nothing left the process. Anything that can die without a normal exit (Lambda, Cloud Run jobs,
#   Kubernetes SIGKILL after the grace period) must call `finish()` before it returns.

# %% [markdown]
# ## TODO 4: GDPR erasure ("delete everything about customer X")
#
# The customer `SUBJECT` asked to be forgotten. Write three functions in the cell below:
# - `find_user_traces(user_id) -> list[str]`: every trace id with an observation of that user, in **every**
#   environment: `langfuse.api.observations.get_many(fields="core", filter=json.dumps([...]), limit=1000,
#   cursor=...)` with the filter `{"type": "string", "column": "userId", "operator": "=", "value": user_id}`, following
#   `resp.meta.cursor` until it is None. Sorted, unique.
# - `delete_traces(trace_ids, *, guard_tag, timeout=300, poll_s=5) -> dict`: the deletion primitive.
#   1. **Guard** (this lab's interlock; in production: an approved dry-run list): for every id, read one observation
#      (`observations.get_many(trace_id=..., fields="core,trace_context", limit=1)`); if it exists and `guard_tag` is
#      not in its `.tags`, raise `PermissionError` before deleting anything.
#   2. `langfuse.api.trace.delete_multiple(trace_ids=...)` in chunks of 100.
#   3. **Wait for the worker** (it deletes every ~2 min): poll every `poll_s` s until no id has an observation
#      (`get_many(trace_id=..., limit=1)`) or a score (`scores.get_many(trace_id=..., limit=1)`), or `timeout`.
#   4. Return a receipt: `{"trace_ids": sorted ids, "requested_at": ISO, "verified_at": ISO, "seconds": float,
#      "remaining": [ids still there]}`.
# - `erase_user(user_id, *, guard_tag, timeout=300) -> dict`: find + delete; the receipt plus `"user_id"` and
#   `"found"` (an empty receipt when there is nothing to delete).
#
# Expected: `erase_user(SUBJECT, guard_tag=RUN_TAG)` deletes the subject's 2 requests (production **and** staging)
# in about 1–2.5 minutes.

# %%
# TODO 4: your code here (find_user_traces, delete_traces, erase_user)

# %% [markdown]
# ### The erasure procedure, with the planted straggler
# A background job (a nightly "conversation summary") already traced the subject, but its spans are still in **its**
# process's SDK buffer (`flush_interval=600`) when the erasure runs. The cell holds that process open, runs your
# erasure, then lets the job finish: its trace arrives **after** the deletion and re-creates the subject's data.
# What catches it is the procedure, not the API: **re-check during a grace period** (longer than any producer's flush
# interval plus ingestion lag) and erase again. Two passes take about 3–5 minutes.

# %%
prod_dep = PLAN["deployments"]["support-api@production"]
job = ops_lab.start_worker({"run_id": RUN_ID, "scenario": "summary-job", "name": "conversation-summary", "n": 1,
                            "exit": "finish", "tags": [RUN_TAG, TAG, "background-job"], "user_id": SUBJECT,
                            "client": {"base_url": BASE_URL, "environment": prod_dep["environment"],
                                       "release": prod_dep["release"], "flush_interval": 600}}, hold=True)
HELD = job.wait_held()["trace_ids"]
print("the job holds a trace of the subject in its buffer:", HELD)
PASSES = [erase_user(SUBJECT, guard_tag=RUN_TAG)]
print(f"pass 1: deleted {len(PASSES[0]['trace_ids'])} trace(s) in {PASSES[0]['seconds']} s, remaining "
      f"{PASSES[0]['remaining']}")
job.release()
print("the job finished:", job.result(timeout=120)["exit"], "- its buffered trace is on its way")
GRACE_S = 90
deadline = time.time() + GRACE_S
while time.time() < deadline:
    time.sleep(10)
    if find_user_traces(SUBJECT):
        PASSES.append(erase_user(SUBJECT, guard_tag=RUN_TAG))
        print(f"re-check found data again -> pass {len(PASSES)}: deleted {PASSES[-1]['trace_ids']} in "
              f"{PASSES[-1]['seconds']} s")
        break
FINAL = find_user_traces(SUBJECT)
ERASURE = {"subject": SUBJECT, "passes": PASSES, "straggler": HELD, "final": FINAL}
(OUT / "erasure.json").write_text(json.dumps(ERASURE, indent=1), encoding="utf-8")
print("final check:", "nothing left" if not FINAL else f"STILL THERE: {FINAL}")

# %% [markdown]
# ## Given: the retention job, as a dry run
# Native retention is Enterprise; on OSS a nightly job enforces your plan's per-environment retention with the same
# deletion primitive. Here it runs **dry**: your `ops.expired` over this run's traces, at a simulated date 15 days
# from now (staging and the SDK lab expire, production stays), and a read-only preview of what tonight's run would
# select in the whole project (Metrics API counts; environments your plan doesn't list are kept). Executing it is
# `delete_traces(ops.expired(...), guard_tag=...)`, nothing new.

# %%
flt = json.dumps([{"type": "arrayOptions", "column": "traceTags", "operator": "any of", "value": [RUN_TAG]},
                  {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True}])
ROWS, cursor = [], None
while True:
    resp = langfuse.api.observations.get_many(fields="core,basic", filter=flt, limit=1000, cursor=cursor)
    ROWS += [{"trace_id": o.trace_id, "environment": o.environment, "timestamp": o.start_time} for o in resp.data]
    cursor = resp.meta.cursor
    if not cursor:
        break
POLICY = ops.retention_policy(PLAN)
SIM_NOW = datetime.now(timezone.utc) + timedelta(days=15)
EXPIRED = ops.expired(ROWS, POLICY, now=SIM_NOW)
RETENTION = {"now": SIM_NOW.isoformat(), "policy": POLICY, "expired": EXPIRED}
env_of = {r["trace_id"]: r["environment"] for r in ROWS}
print(f"policy {POLICY}\ndry run at {SIM_NOW:%Y-%m-%d}: would delete {dict(Counter(env_of[t] for t in EXPIRED))}, "
      f"keep {dict(Counter(e for t, e in env_of.items() if t not in set(EXPIRED)))}")
now = datetime.now(timezone.utc)
print("\ntonight, project-wide (read-only preview):")
for env, days in POLICY.items():
    if env == "*" or days is None:
        continue
    rows = langfuse.api.metrics.metrics(query=json.dumps({
        "view": "observations", "metrics": [{"measure": "count", "aggregation": "count"}], "dimensions": [],
        "filters": [{"column": "isRootObservation", "operator": "=", "value": True, "type": "boolean"},
                    {"column": "environment", "operator": "=", "value": env, "type": "string"}],
        "fromTimestamp": "2020-01-01T00:00:00+00:00", "toTimestamp": (now - timedelta(days=days)).isoformat()})).data
    print(f"  {env:<11} older than {days:>2} days: {rows[0]['count_count'] if rows else 0} trace(s)")

# %% [markdown]
# ## TODO 5 (optional, in the UI): the staging project and an Ingestion key
#
# The first step of your plan, done by hand because OSS has no API for it:
# 1. Create the plan's non-production organization and project (avatar menu / organization switcher → **New
#    organization**, then **New project**). Put its project id (Settings → General) into `ops_plan.PLAN`.
# 2. In that project, **Settings → API keys → Create**, permission **Ingestion**, an expiry date. Copy the pair into
#    the repo-root `.env` as `M13_STAGING_PUBLIC_KEY` / `M13_STAGING_SECRET_KEY` (the secret is shown once).
# 3. Restart the kernel and run the notebook again with a new `RUN_TAG`.
#
# The cell sends 3 staging requests with that key through the tap, then shows what the key cannot do. verify checks the
# isolation (another project; production does not see them) and the least privilege (it cannot read). Without the
# keys verify shows ⚠️ and the module still passes.
#
# **Key rotation** is the same motion: create the new key, deploy it, check the old one's *Last used* stops moving
# (Settings → API keys), delete the old one (deletion is immediate: a forgotten deployment fails at once).

# %%
STAGING_KEYS = [os.getenv(k) for k in ops_lab.STAGING_KEY_ENV]
STAGING = None
if all(STAGING_KEYS):
    from langfuse.api import LangfuseAPI
    staging_dep = PLAN["deployments"]["support-api@staging"]
    st = ops_lab.start_worker({"run_id": RUN_ID, "scenario": "staging-project", "n": 3, "exit": "finish",
                               "tags": [RUN_TAG, TAG, "sdk:staging-project"], "user_id": f"sdk-{RUN_ID}",
                               "client": {"base_url": TAP.url, "public_key": STAGING_KEYS[0],
                                          "secret_key": STAGING_KEYS[1], "environment": staging_dep["environment"],
                                          "release": staging_dep["release"]}}).result(timeout=120)
    STAGING = {"trace_ids": st["trace_ids"]}
    api = LangfuseAPI(base_url=BASE_URL, username=STAGING_KEYS[0], password=STAGING_KEYS[1])
    print("the staging key's project:", api.projects.get().data[0].id, "| production:", PROJECT_ID)
    print("exports:", [(r["spans"], r["status"]) for r in TAP.exports(st["trace_ids"])])
    for what, call in {"read traces": lambda: api.observations.get_many(limit=1),
                       "read prompts": lambda: api.prompts.list(limit=1)}.items():
        try:
            call()
            print(f"  {what}: allowed (not an Ingestion key?)")
        except Exception as exc:
            print(f"  {what}: refused ({getattr(exc, 'status_code', exc)})")
else:
    print("skipped: no M13_STAGING_PUBLIC_KEY / M13_STAGING_SECRET_KEY in .env (optional; verify shows ⚠️)")

# %% [markdown]
# Verify reads what Langfuse stored, the tap's log, your reports, the erasure receipts and the dry run:
# - the deployments: the kept requests in the environments and releases your plan names, the same prompt version in
#   both; the environment filter in the Metrics API separates them (and only them);
# - your plan matches the live stack (edition, the production project's id, no project mixing tiers, every
#   environment of this run has a retention);
# - every SDK report equals the tap log + stored data; `tuned` batches at `flush_at`; flushing per request costs the
#   request path;
# - **planted:** the outage didn't slow the request path, your report names exactly the lost traces, the hardened
#   config lost none; the killed job lost its buffer and `finish()` delivered everything;
# - **planted:** nothing of the subject is left in any environment, the straggler was erased by a later pass, nobody
#   else's trace was touched;
# - the retention dry run = your plan's policy on the stored data;
# - ⚠️ the staging project (until TODO 5), native retention and the organization API (Enterprise on this stack).

# %%
verify.check_module_13(RUN_TAG, plan=PLAN, traffic=TRAFFIC, sdk=SDK, tap_log=TAP.log_path, erasure=ERASURE,
                       retention=RETENTION, staging=STAGING)

# %% [markdown]
# ## UI tour
# - [ ] The **environment selector** (top bar): pick `production`, then `staging`, then both. Every view (Traces,
#   Sessions, Dashboards, Scores) follows it. Pick `sdk-lab`: the SDK lab's 7 deployments.
# - [ ] **Traces**, filter `tags = <your run tag>`: the subject's requests are gone, the other two customers' are
#   there in both environments. Search **Users** for the subject: nothing.
# - [ ] **Prompts → `support-agent-system`**: one `production` label, served to both environments. Imagine a
#   teammate testing a staging prompt by moving it.
# - [ ] **Settings → API keys**: *Create* shows the four permissions and the expiry date; *Last used* is how you find
#   stale keys before deleting them. **Settings → Members**: the roles (and no per-project override on OSS).
# - [ ] **Organization settings**: no *API keys* or *Audit logs* page on OSS; **Settings → Data retention** is
#   missing too. That's the Enterprise boundary from the first cell.
# - [ ] Open `ops_out/<run id>/otlp_exports.jsonl`: the `outage` scenario's 503s, the same `batch` hash retried, and
#   the batch that never got a 200.
#
# ## Production notes
# - **Separate by project, label by environment.** Environments are views: one key, one set of members, one prompt
#   label namespace, one retention. Production deserves its own project (keys, prompts, members, retention); staging,
#   dev and CI can share one with environments. On OSS, people with different roles in prod and non-prod need a
#   **second organization** (project-level roles are Enterprise).
# - **One key per workload, least privilege.** Apps get **Ingestion** (+ a Viewer key or a prompt cache for prompts);
#   jobs that delete get Full access, owned by the platform team, rotated monthly. Set expiry dates; rotate by
#   create → deploy → watch *Last used* → delete. Keys live in a secret manager, never in a notebook.
# - **Prompts are the sharpest edge:** any MEMBER can move `production` on OSS (protected labels are Enterprise).
#   Keep production prompts where people are VIEWERs and let CI (module 10) move labels.
# - **Retention is a policy, not a default:** self-hosted keeps everything forever. On OSS, run a scheduled deletion
#   job per environment (dry run → count check → delete → verify); on Enterprise, set it per project (≥ 3 days).
#   Blob storage exports (module 11) are copies your retention doesn't reach: give the bucket a lifecycle rule.
# - **Erasure is a procedure:** find by user id in every environment → guarded delete → wait (≈2 min ticks) → verify →
#   re-check after a grace period ≥ your longest flush interval → keep a receipt (user hash, counts, times; not the
#   data). Stop tracing the user first (or mask them) so new data doesn't race the deletion. Scores and raw events go
#   with the trace; exports and backups don't.
# - **SDK settings:** keep the defaults for servers (background export costs the request path ~nothing); set
#   `timeout` to ride out short outages (≥ 15–20 s), `flush_interval` lower for fresher data, `sample_rate` for volume
#   (module 05), `environment`/`release` per deployment. Call `flush()`/`shutdown()` at the end of every job and in
#   SIGTERM handlers; never per request. During a long outage the SDK drops data rather than hurting the app: watch
#   for "Failed to export spans batch" in your logs.
# - **Scale-out:** web and worker are stateless; scale web behind a load balancer for UI/API/ingestion and add workers
#   for queue throughput (ingestion, evals, deletions, exports). ClickHouse (a replicated cluster or managed), Postgres
#   and Redis (noeviction) are managed services; S3/GCS/Azure Blob for events and media. Watch queue lag
#   (`/api/health?failIfEventPropagationStuck=true` on the worker), ClickHouse disk, and Redis memory.
# - **Enterprise and Cloud features** (audit logs, SCIM, project-level roles, retention, protected labels, Cloud spend
#   alerts in module 12): decide which you need before you need them, and write the workaround into the plan.
# - **Why it matters (leadership):** observability data is customer data. This module turns "we use Langfuse" into a
#   reviewable plan for who can see and change production, how long data lives, how a deletion request is honoured
#   and proven, and evidence that a Langfuse outage costs the product nothing (and costs data only within a measured
#   budget).

# %% [markdown]
# ## When you're done: stop the tap

# %%
TAP.stop()
print("tap stopped")
