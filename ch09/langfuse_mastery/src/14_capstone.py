# %% [markdown]
# # Module 14 (capstone): the production loop, on one planted regression
#
# **Goal:** modules 01–13 built every piece on its own. A production team never uses them on their own: an incident
# goes through **all** of them, in order, and somebody has to show afterwards what happened. In this module one bad
# deploy flows through the whole loop, and you write the parts that connect the stages and the decisions between them:
#
# ```
#  traffic ─► judge ─► alert ─► review queue ─► human labels ─► dataset items ─► prompt fix ─► experiment ─► CI gate
#  (01-05)   (07/08)  (11/12)     (08)            (08)            (09)            (04)         (09/10)      (10)
#     ▲                                                                                                       │
#     └──────── recovered traffic ◄── the alert resolves ◄── promote (production + live, audited) ◄── ship ◄──┘
#                                                                                  a quick fix that breaks billing: BLOCK
# ```
#
# | Stage | What happens | Reuses (your code) | You write here |
# |---|---|---|---|
# | traffic | the agent serves the prompt label **`live`**; `production` = the version the gate approved | 01–05, `prompting` (04) | |
# | judge | live traffic has **no ground truth**: the LLM judge (07) or the stand-in (08) scores every request | `judging` (07) | |
# | alert | the DIY alert job (12) compares the judge pass rate with the baseline window | `analytics`, `weekly_pull` (11), `alerting`, `alert_job` (12) | (given: `loop.LOOP_RULES`) |
# | review | what the judge flagged + a control goes to the annotation queue `incident-review`; a human labels it | `review` (08), `scoring` (06) | **TODO 2** (+ 1b) |
# | dataset | every **confirmed** failure becomes a regression test, labelled by the human | `experiments` (09) | **TODO 3** (+ 1c) |
# | fix + experiment + CI | a fix is a new prompt version (label `hotfix`); the CI gate runs it next to production on the pinned dataset | `ci_gate`, `ci/release_gate`, `ci/test_release_gate` (10) | **TODO 4** (+ 1a policy, 1d, 1e) |
# | promote | only a shipped fix that passes the incident's own tests moves `production` + `live` | | **TODO 4** |
# | evidence | a timeline of every stage, with timestamps, numbers and links, read back from Langfuse | | **TODO 5** (+ 1f) |
#
# **The planted regression.** Someone deploys module 04's `staging` candidate (login → billing) **straight to `live`**,
# skipping the gate. Nothing errors and cost and latency don't move: only the judge sees it.
#
# **The loop must not be fooled.** The first fix is a **quick fix**: a find-and-replace of `billing_specialist` →
# `account_security` over the routing rules. It repairs login, so it passes the incident's new tests, but it also sends
# double charges to account security. The gate must **block** it and the loop must not promote it. The second fix
# (production's rules plus "never send login issues to billing") must ship and be promoted; the traffic after it must be
# healthy and the alert must resolve.
#
# **This module is also your leadership demo.** It ends with an incident report (`incident-report.html` + `.md`) whose
# every claim is a Langfuse link. The section "Presenting it" near the end says what to show, in which order.
#
# Things verified live on this server (4.77 / SDK 5.2) that shape the module:
# - A label move bumps `updatedAt` on **both** versions it touches and the prompt keeps no label history (audit logs
#   are Enterprise, module 13). So every label move here also writes a **comment on the prompt version**
#   (`api.comments.create(object_type="PROMPT", object_id=<version id>)`): that is the deploy log the report reads.
# - `update_prompt(..., new_labels=["production", "live"])` moves both labels in one call; other labels on the version
#   stay. Prompt reads carry the version's `id`, `createdAt` and `commitMessage`.
# - Score views of the Metrics API filter on the **score's** timestamp (module 11) and live judge scores arrive ~30 s
#   after the trace: each traffic window closes only after its judge scores exist.
# - Native monitors and webhooks can't reach this machine until the compose SSRF whitelist is added (module 12): the
#   alert runs through your DIY job.
#
# Docs: https://langfuse.com/docs/evaluation/overview · https://langfuse.com/docs/prompt-management/features/prompt-version-control ·
# https://langfuse.com/docs/evaluation/evaluation-methods/annotation · https://langfuse.com/docs/evaluation/experiments/datasets

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import importlib
import json
import os
import secrets
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from langfuse import get_client
from langfuse_lab import verify, traffic, kb, prompts, loop_spec, alert_spec, standin_judge, regression
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_lab.oracle_llm import OracleChatModel
from langfuse_lab.webhook_receiver import Receiver

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
langfuse = get_client()
RUN_TAG = f"run:m14-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
INCIDENT = loop_spec.incident_id(RUN_ID)
ENV = loop_spec.ENVIRONMENT
OUT = LAB_ROOT / "langfuse_mastery" / "loop_out" / RUN_ID  # git-ignored: receiver log, ticks, gate artifacts, report

# Prompts, datasets, configs and queues are PROJECT-GLOBAL (queues and datasets can't be deleted). Keep PREFIX empty.
PREFIX = ""
# True = rehearse with the oracle LLM and a dry gate (no Azure calls). verify refuses dry traffic, so the graded run is False.
DRY_RUN = os.getenv("LOOP_DRY_RUN") == "1"
# True = a scripted reviewer labels whatever you haven't reviewed in the UI (rehearsals, or when you're short of time).
SCRIPTED_REVIEW = os.getenv("LOOP_SCRIPTED_REVIEW") == "1"

NAMES = loop_spec.names(PREFIX)
PROMPT, DATASET = NAMES["prompt"], NAMES["dataset"]
LIVE, PRODUCTION, HOTFIX = loop_spec.LIVE_LABEL, loop_spec.PRODUCTION_LABEL, loop_spec.HOTFIX_LABEL
CASES = traffic.loop_cases(run_id=RUN_ID)
print("run tag:", RUN_TAG, "| incident:", INCIDENT, "| dry run:", DRY_RUN, "| scripted review:", SCRIPTED_REVIEW)
for window, served, why in traffic.LOOP_WINDOWS:
    print(f"  window {window:<9} served by the {served} version: {why}")
for c in CASES[:3]:
    print(f"    {c.case_id:<14} {c.user_id}  {c.message}")

# %% [markdown]
# ## Prerequisites: your modules 04–12
# The capstone runs YOUR code from earlier modules. This cell checks each one and tells you exactly what to finish
# first (the module, the file, the TODO). Nothing in Langfuse is touched yet.

# %%
from langfuse_mastery import weekly_pull, alert_job
from langfuse_mastery.ci import release_gate


def _tests(path):
    r = subprocess.run([sys.executable, "-m", "pytest", f"langfuse_mastery/{path}", "-q", "-p", "no:cacheprovider",
                        "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or [r.stderr[-200:]])[-1]
    return r.returncode == 0, last


def _implemented(fn, *args, **kwargs):
    """A stub raises NotImplementedError before touching its arguments; an implementation fails on the dummy ones."""
    try:
        fn(*args, **kwargs)
    except NotImplementedError:
        return False, "still a stub (NotImplementedError)"
    except Exception:
        return True, "implemented"
    return True, "implemented"


gate_test = (LAB_ROOT / "langfuse_mastery" / "ci" / "test_release_gate.py").read_text(encoding="utf-8")
PREREQS = [
    ("04", "prompting.py (TODO 1)", _tests("tests/test_prompting.py")),
    ("06", "scoring.py (TODO 2)", _tests("tests/test_scoring.py")),
    ("07", "judging.py (TODO 1)", _tests("tests/test_judging.py")),
    ("08", "review.py (TODO 1)", _tests("tests/test_review.py")),
    ("09", "experiments.py (TODO 1)", _tests("tests/test_experiments.py")),
    ("10", "ci_gate.py (TODO 1)", _tests("tests/test_ci_gate.py")),
    ("10", "ci/release_gate.py run_variant + verdicts (TODO 2)",
     _implemented(release_gate.run_variant, None, None, "x", role="x")),
    ("10", "ci/release_gate.py baseline_for (TODO 3)", _implemented(release_gate.baseline_for, None, None)),
    ("10", "ci/release_gate.py evaluate_candidate + record_gate (TODO 4)",
     _implemented(release_gate.evaluate_candidate, None, None)),
    ("10", "ci/test_release_gate.py (TODO 5a)",
     (False, "still pytest.fail(...)") if 'pytest.fail("TODO 5a' in gate_test else (True, "implemented")),
    ("11", "analytics.py (TODO 1)", _tests("tests/test_analytics.py")),
    ("11", "weekly_pull.py scope_filters + health_metrics (TODO 2)", _implemented(weekly_pull.health_metrics, None, None, None)),
    ("12", "alerting.py (TODO 1)", _tests("tests/test_alerting.py")),
    ("12", "alert_job.py run_once (TODO 3)", _tests("tests/test_alert_job.py")),
]
for module, what, (ok, detail) in PREREQS:
    print(f"{'✅' if ok else '❌'} module {module}: {what}: {detail}")
MISSING = [f"module {m}: {w}" for m, w, (ok, _) in PREREQS if not ok]
if MISSING:
    raise RuntimeError("finish these first (the capstone runs your code from them):\n  " + "\n  ".join(MISSING))
print("\nall prerequisites done")

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/loop.py` (the loop's decisions + pytest)
#
# Open `ch09/langfuse_mastery/loop.py`. `LOOP_RULES` (what pages on live traffic: the judge pass rate) is given.
# Implement, using your earlier modules where the docstrings say so:
# - **1a `LOOP_POLICY`**: the release policy as data (re-runs, tolerated regressions and drop, how often the fix must
#   pass the incident's tests). It becomes the gate's thresholds. Comment each value: it's a decision.
# - **1b `review_selection`**: what goes into the review queue (judge flags, unjudged requests, controls).
# - **1c `incident_items`**: human labels → dataset items (only ANNOTATION scores; only confirmed failures).
# - **1d `gate_env`**: the policy as module 10's `GATE_*` variables.
# - **1e `promotion_decision`**: when a gate run may be promoted (ship AND the incident's tests pass AND the gate ran
#   at least as strictly as your policy).
# - **1f `timeline`**: order the evidence, flag gaps and causality violations, measure detect / review / fix / recover.
#
# Re-run the cell until it shows `16 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_loop.py", "-q",
                         "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
from langfuse_mastery import loop
loop = importlib.reload(loop)
print("LOOP_POLICY:", loop.LOOP_POLICY)

# %% [markdown]
# ## Given: the stack the loop runs on (modules 04–12 recap)
# This is setup, not a TODO:
# - **Prompt, config, dataset:** module 10's `harness.bootstrap` get-or-creates `support-agent-system` (labels
#   `production` / `staging` / `candidate` only when missing), the `handoff_correct` config and the regression dataset
#   (seeded only when it doesn't exist). `ensure_version` gets a version **by content** (`create_prompt` never dedupes).
# - **Module 09's label fix, if it is still pending.** Module 10's seed keeps `cases.json`'s label for "Can you send me
#   the invoice for March?" (`none`), which contradicts the routing policy (invoice → billing). Validation showed what a
#   wrong label does here: the gate **blocked the correct fix** (it routed the invoice to billing; production happened
#   not to hand off). A regression set is only as good as its labels: `ensure_label_fix` writes module 09's fix (a new
#   item version) when your dataset doesn't have it yet.
# - **The audit trail:** `move(version, labels, action, why)` = one `update_prompt` + one **audit comment** on the
#   prompt version (`[run tag] action: …`). `audit_log()` reads them back. Every label move of this module goes through
#   it: the start, the planted deploy, the fix candidates and the promotion.
# - **The start state:** `production` and `live` on the approved (good) version.
# - **Review:** module 08's configs `human_routing_ok` + `review_error_type`, a new CATEGORICAL `expected_handoff`
#   (which team the request should have gone to: that's what turns a label into a test), and the queue
#   **`incident-review`** with all three.
# - **The judge:** `JUDGE_MODE = "live"` when your module 07 rule `support-live-eval` exists and is enabled (traffic
#   is tagged `live-eval`; the worker judges it, ~2 judge calls per request), else module 08's **stand-in** keyword
#   judge writes the same `routing_judge` scores (source API).
# - **The alert receiver** (module 12) on `127.0.0.1:80/m14/alerts`, signed with a per-run secret.

# %%
from langfuse import observe, propagate_attributes
from langfuse.api import NotFoundError
from langfuse.api.scores.types import CreateScoreRequest
from langfuse.langchain import CallbackHandler
from langfuse_mastery import alerting, experiments, judging, prompting, review, scoring
from langfuse_mastery.ci import harness

PROJECT_ID = langfuse.api.projects.get().data[0].id
UI = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")
BOOT = harness.bootstrap(langfuse, prefix=PREFIX)


def ensure_version(template, why):
    """Get-or-create the version with this text and module 04's config (create_prompt never dedupes)."""
    metas = [m for m in langfuse.api.prompts.list(name=PROMPT, limit=100).data if m.name == PROMPT]
    for v in sorted({v for m in metas for v in (m.versions or [])}):
        row = langfuse.api.prompts.get(PROMPT, version=v)
        if row.prompt == template and all((row.config or {}).get(k) == val for k, val in prompts.PROMPT_CONFIG.items()):
            return v
    return langfuse.create_prompt(name=PROMPT, prompt=template, type="text", config=prompts.PROMPT_CONFIG,
                                  commit_message=why).version


def prompt_row(version):
    """The stored prompt version as a dict: prompt, labels, commitMessage, and its own id / createdAt."""
    return langfuse.api.prompts.get(PROMPT, version=version).dict()


def prompt_url(version):
    return f"{UI}/project/{PROJECT_ID}/prompts/{quote(PROMPT)}?version={version}"


def trace_url(trace_id):
    return f"{UI}/project/{PROJECT_ID}/traces/{trace_id}"


def move(version, labels, action, why):
    """Move labels to a version AND record it: a comment on the prompt version is the audit log OSS doesn't have."""
    langfuse.update_prompt(name=PROMPT, version=version, new_labels=list(labels))
    langfuse.api.comments.create(project_id=PROJECT_ID, object_type="PROMPT", object_id=prompt_row(version)["id"],
                                 content=loop_spec.audit_text(RUN_TAG, action, f"{' + '.join(labels)} -> v{version}: {why}"))
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {action}: {' + '.join(labels)} -> v{version} ({why})")


def audit_log():
    """This run's audit comments on every version of the prompt, oldest first:
    [{"action", "text", "version", "at" (server time), "comment_id"}]."""
    out = []
    for v in sorted({v for m in langfuse.api.prompts.list(name=PROMPT, limit=100).data if m.name == PROMPT
                     for v in (m.versions or [])}):
        for c in langfuse.api.comments.get(object_type="PROMPT", object_id=prompt_row(v)["id"], limit=100).data:
            parsed = loop_spec.parse_audit(c.content)
            if parsed and parsed[0] == RUN_TAG:
                out.append({"action": parsed[1], "text": parsed[2], "version": v, "at": c.created_at.isoformat(),
                            "comment_id": c.id})
    return sorted(out, key=lambda a: a["at"])


def active_items():
    """The dataset's ACTIVE items (archived ones are not listed), as API objects."""
    return harness._active_items(langfuse, DATASET)


def ensure_label_fix():
    """Module 09's label fix when it is still pending (the seeded invoice request expects "none"; the policy says billing)."""
    case, team = regression.LABEL_FIX
    iid = regression.dataset_item_id(DATASET, regression.curated(case).message)
    try:
        item = langfuse.api.dataset_items.get(iid)
    except NotFoundError:
        return f"{case}: not in the dataset"
    have = (item.expected_output or {}).get("handoff")
    if verify._plain(item.status) != "ACTIVE" or have == team:
        return f"{case}: expects {have!r} already"
    langfuse.create_dataset_item(dataset_name=DATASET, id=iid, input=item.input,
                                 expected_output={**(item.expected_output or {}), "handoff": team},
                                 metadata=item.metadata)
    langfuse.flush()
    return f"{case}: label fixed {have!r} -> {team!r} (module 09's label fix, a new item version)"


LABEL_FIX = ensure_label_fix()
GOOD = ensure_version(prompts.SYSTEM_PROMPT_TEMPLATE, "module 04: the approved routing rules")
BAD = ensure_version(prompts.WRONG_HANDOFF_TEMPLATE, "module 04: staging candidate (routing rules refresh)")
move(GOOD, [PRODUCTION, LIVE], "start", "the capstone starts healthy: the approved version serves live traffic")

REVIEW_CONFIGS = {
    NAMES["human"]: dict(data_type="BOOLEAN", description="human reviewer: the agent routed this request correctly"),
    NAMES["error_type"]: dict(data_type="CATEGORICAL", description="human reviewer: what went wrong",
                              categories=[{"label": l, "value": float(i)} for i, l in enumerate(loop_spec.ERROR_TYPES)]),
    NAMES["expected"]: dict(data_type="CATEGORICAL", description="human reviewer: the team this request should go to",
                            categories=[{"label": l, "value": float(i)} for i, l in enumerate(loop_spec.EXPECTED_TEAMS)]),
}


def ensure_configs():
    """Module 06 pattern: get-or-create by name, skipping archived configs. Returns {name: config dict}."""
    have, page = {}, 1
    while True:
        resp = langfuse.api.score_configs.get(page=page, limit=100)
        for c in resp.data:
            if c.name in REVIEW_CONFIGS and not c.is_archived:
                have.setdefault(c.name, c.dict())
        if page >= (resp.meta.total_pages or 1):
            break
        page += 1
    for name, spec in REVIEW_CONFIGS.items():
        if name not in have:
            have[name] = langfuse.api.score_configs.create(name=name, **spec).dict()
    return have


def ensure_queue(configs):
    """Module 08's get-or-create (queues can't be edited or deleted: get the configs right the first time)."""
    ids = [configs[n]["id"] for n in REVIEW_CONFIGS]
    page = 1
    while True:
        resp = langfuse.api.annotation_queues.list_queues(page=page, limit=100)
        for q in resp.data:
            if q.name == NAMES["queue"]:
                missing = set(ids) - set(q.score_config_ids)
                if missing:
                    raise RuntimeError(f"queue {q.name} lacks configs {missing}: queues can't be edited through the "
                                       "API; fix it in the UI or use another PREFIX")
                return q
        if page >= (resp.meta.total_pages or 1):
            break
        page += 1
    return langfuse.api.annotation_queues.create_queue(
        name=NAMES["queue"], score_config_ids=ids,
        description="production loop: requests the judge flagged during an alert, plus controls")


def detect_judge_mode():
    rules, cursor = [], None
    while True:
        resp = langfuse.api.evaluation_rules.list(limit=100, **({"cursor": cursor} if cursor else {}))
        rules += resp.data
        cursor = resp.meta.cursor
        if not cursor:
            break
    return "live" if any(r.name == NAMES["rule"] and r.enabled for r in rules) else "stand-in"


CONFIGS = ensure_configs()
QUEUE = ensure_queue(CONFIGS)
QUEUE_URL = f"{UI}/project/{PROJECT_ID}/annotation-queues/{QUEUE.id}"
JUDGE_MODE = detect_judge_mode()
# JUDGE_MODE = "stand-in"   # uncomment to force the stand-in judge (no Azure judge calls)
JUDGE_SOURCE = "EVAL" if JUDGE_MODE == "live" else "API"
JUDGE_METRIC = f"score.{NAMES['judge']}.{JUDGE_SOURCE}"  # what the alert job measures (module 11's metric key)

SECRET = os.environ.setdefault(loop_spec.SECRET_ENV, "lf-whsec_" + secrets.token_hex(16))
NATIVE_PROMPT_SECRET = os.getenv(alert_spec.SECRET_ENV["prompt"])  # module 12's prompt automation, if you set it up
if "RX" in globals():
    RX.stop()  # re-running this cell: free port 80 first
RX = Receiver(OUT / "deliveries.jsonl", secrets={loop_spec.ALERT_PATH: SECRET,
                                                 **({alert_spec.PROMPT_PATH: NATIVE_PROMPT_SECRET}
                                                    if NATIVE_PROMPT_SECRET else {})}).start()
print(f"prompt {PROMPT}: good v{GOOD}, bad v{BAD}; labels {BOOT['labels']} (+ live)")
print(f"dataset {DATASET}: {BOOT['items']} active item(s){' (seeded now)' if BOOT['seeded'] else ''}; {LABEL_FIX}")
print("review configs:", {k: v["id"] for k, v in CONFIGS.items()}, "| queue:", QUEUE.name, QUEUE_URL)
print("judge mode:", JUDGE_MODE, "| alert metric:", JUDGE_METRIC, "| receiver:", RX.url(loop_spec.ALERT_PATH))

# %% [markdown]
# ## Given: the instrumentation and the traffic windows (modules 01–05, 07/08 recap)
# - `traced_turn`: one `support-request` trace per request, served by whatever `live` points at **right now**
#   (`cache_ttl_seconds=0`). Your module 04 code compiles it (`prompting.compile_strict`: a missing variable is an error,
#   not a silent `{{max_sentences}}`) and turns its config into model kwargs (`prompting.llm_params`). The trace carries
#   `version="prompt-v<n>"`, the prompt link, the run tag, the stable tag `prod-loop`, `metadata.handoff_target`, and
#   **no code score**: production traffic has no expected answer. The judge is the only quality signal.
# - `run_window(name)`: the window's 3 requests, then the judge (stand-in: right away; live: wait ~30 s for the worker),
#   and only then the window closes (score views use the score's timestamp). Windows open on a whole millisecond.

# %%
fixtures = load_fixtures()


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


@observe(name=loop_spec.ROOT_NAME, as_type="agent", capture_input=False, capture_output=False)
def traced_turn(case, agent):
    prompt = langfuse.get_prompt(PROMPT, label=LIVE, cache_ttl_seconds=0)
    system = prompting.compile_strict(prompt.prompt, prompts.PROMPT_VARIABLES)
    params = prompting.llm_params(prompt.config)
    tags = [RUN_TAG, NAMES["tag"], *case.tags, *([NAMES["live_tag"]] if JUDGE_MODE == "live" else []),
            *(["dry-run"] if DRY_RUN else [])]
    with propagate_attributes(session_id=case.session_id, user_id=case.user_id, tags=tags, environment=ENV,
                              version=alert_spec.agent_version(prompt.version), prompt=prompt):
        run = agent.invoke(case.message, config={"callbacks": [CallbackHandler()]}, system_prompt=system,
                           llm_params=params)
        actual = run.summary["handoff_target"]
        langfuse.update_current_span(input=case.message, output=run.summary["final_answer"],
                                     metadata={"handoff_target": actual})
        return {"case": case, "trace_id": langfuse.get_current_trace_id(),
                "root_id": langfuse.get_current_observation_id(), "version": prompt.version, "handoff": actual}


def judge_standin(row):
    """Module 08's stand-in judge: a deterministic keyword policy, stored where an EVAL score would land."""
    ok, reasoning = standin_judge.judge_routing(row["case"].message, row["handoff"])
    # environment=ENV: outside the trace's context a score gets the CLIENT's environment ("default"), and the alert's
    # Metrics query (environment = production) would never see it
    langfuse.create_score(name=NAMES["judge"], value=1.0 if ok else 0.0, data_type="BOOLEAN", trace_id=row["trace_id"],
                          observation_id=row["root_id"], comment=reasoning, environment=ENV,
                          metadata={"evaluator": standin_judge.EVALUATOR})


def judge_verdicts(trace_ids, *, timeout=300):
    """{trace_id: {"ok", "comment", "at"}} from each trace's latest judge score; waits until every trace has one."""
    deadline = time.time() + timeout
    while True:
        out = {}
        for t in trace_ids:
            rows = [verify._row(s) for s in langfuse.api.scores.get_many(trace_id=t, fields="details", limit=100).data]
            rows = [s for s in rows if s["name"] == NAMES["judge"] and review.is_judge_score(s)]
            if rows:
                s = max(rows, key=lambda x: str(x["timestamp"]))
                out[t] = {"ok": judging.as_verdict(s["value"]), "comment": s.get("comment"),
                          "at": alerting.iso(s["timestamp"])}
        if len(out) == len(trace_ids) or time.time() > deadline:
            if len(out) < len(trace_ids):
                raise TimeoutError(f"{len(trace_ids) - len(out)} trace(s) without a judge score: "
                                   "docker logs --tail 50 ch09_langfuse_worker")
            return out
        time.sleep(5)


AGENT = SupportAgent(settings, llm=OracleChatModel() if DRY_RUN else None, guardrail_fn=observed_guardrail,
                     search_fn=observed_search)
WINDOWS, ROWS = {}, {}


def run_window(name):
    time.sleep(2)  # a label move always lands before the window's first request (two clocks: allow for skew)
    start = datetime.now(timezone.utc)
    start = start.replace(microsecond=start.microsecond // 1000 * 1000)  # stored times are whole ms (module 12)
    rows = [traced_turn(c, AGENT) for c in CASES if c.window == name]
    if JUDGE_MODE == "stand-in":
        for r in rows:
            judge_standin(r)
    langfuse.flush()
    verdicts = judge_verdicts([r["trace_id"] for r in rows])
    time.sleep(2)  # keep the windows apart
    WINDOWS[name], ROWS[name] = (start, datetime.now(timezone.utc)), rows
    print(f"window {name}: {WINDOWS[name][0]:%H:%M:%S} -> {WINDOWS[name][1]:%H:%M:%S}")
    for r in rows:
        v = verdicts[r["trace_id"]]
        print(f"  {r['case'].case_id:<14} v{r['version']} -> {r['handoff']:<19} judge {'ok ' if v['ok'] else 'BAD'} "
              f"{(v['comment'] or '')[:70]}")

# %% [markdown]
# ## Run: healthy traffic (the baseline window)

# %%
run_window("baseline")

# %% [markdown]
# ## The planted deploy, and the incident window
# A "routing rules refresh" (module 04's staging candidate) is moved straight to `live`. `production` still points at
# the approved version: the deploy skipped the gate. The audit comment records who moved what, and when.

# %%
move(BAD, [LIVE], "deploy", "routing rules refresh (the staging candidate) deployed straight to live: no gate")
run_window("incident")

# %% [markdown]
# ## Given: the alert job ticks (module 12)
# Your `alert_job.run_once` compares the incident window with the baseline window using `loop.LOOP_RULES`: the judge
# pass rate rule pages; module 12's latency/cost/error rules stay as guard rails. The receiver logs the signed delivery;
# `ALERT` is the payload the on-call bot received (what TODO 2 starts from).
#
# Expected: `[FIRING critical] judge-routing-drop: score.routing_judge.<source> 1 -> 0.3333 (version prompt-v<bad>)`.

# %%
alert_job = importlib.reload(alert_job)
SCOPE = weekly_pull.Scope(tags=(RUN_TAG,), environment=ENV, prefix=PREFIX)
STATE = OUT / "alert-state.json"
STATE.unlink(missing_ok=True)
LINKS = {"traces": f"{UI}/project/{PROJECT_ID}/traces", "prompt": f"{UI}/project/{PROJECT_ID}/prompts/{quote(PROMPT)}",
         "review queue": QUEUE_URL}


def tick(current):
    return alert_job.run_once(langfuse, WINDOWS["baseline"], current, SCOPE, state_path=STATE,
                              url=RX.url(loop_spec.ALERT_PATH), secret=SECRET, rules=loop.LOOP_RULES, links=LINKS)


def delivered(status):
    """The accepted deliveries at the alert path with this status (firing / resolved), as (log row, payload)."""
    out = []
    for row in RX.deliveries(loop_spec.ALERT_PATH):
        body = json.loads(row["body"])
        if row["status"] == 200 and not row["duplicate"] and body.get("status") == status \
                and RUN_TAG in (body.get("scope") or {}).get("tags", []):
            out.append((row, body))
    return out


TICKS = {"fire": tick(WINDOWS["incident"])}
for s in TICKS["fire"]["sent"]:
    print(s["payload"]["title"], "->", "delivered" if s["delivery"]["ok"] else "FAILED")
ALERT = next(body for _, body in delivered("firing") if body["rule"] == loop_spec.JUDGE_RULE)
print("\nthe on-call bot received:", json.dumps({k: ALERT[k] for k in ("id", "rule", "metric", "baseline", "current",
                                                                         "version", "links")}, indent=1))

# %% [markdown]
# ## TODO 2: from the alert to the review queue
#
# The alert names a window, a scope and a version. Somebody has to look at the conversations behind it, and not only
# at the ones the judge flagged: a control the judge passed is how you find out what the judge misses.
#
# Write `fill_review_queue(alert) -> list[dict]`:
# 1. `sc = alerting.alert_scope(alert)` (module 12): the window (`from`, `to`), the tags, the environment.
# 2. The window's requests: `langfuse.api.observations.get_many(fields="core,basic,io", filter=json.dumps([...]),
#    limit=100, cursor=...)`, following `resp.meta.cursor`: `startTime` `>=` from / `<` to (`datetime`, ISO),
#    `isRootObservation` `=` True (`boolean`), `name` `=` `loop_spec.ROOT_NAME` (`string`), the tags on **`traceTags`**
#    (`arrayOptions`, `any of`), `environment` `=` (`string`). Oldest first (sort by `start_time`).
# 3. Their scores: `langfuse.api.scores.get_many(trace_id=..., fields="details,subject,annotation", limit=100)` →
#    `[s.dict() for s in ...data]`, as `{trace_id: [score dicts]}` in time order.
# 4. `sel = loop.review_selection(scores, judge=NAMES["judge"], salt=RUN_ID)` (your TODO 1b).
# 5. The queue doesn't dedupe: list its items (`langfuse.api.annotation_queues.list_queue_items(QUEUE.id, page=...,
#    limit=100)`, paging to `meta.total_pages`, `.dict()` each) and add only `review.items_to_add(existing,
#    sel["queue"])` with `create_queue_item(QUEUE.id, object_id=trace_id, object_type="TRACE")`.
# 6. Return one row per queued trace, in `sel["queue"]` order: `{"trace_id", "reason": sel["reasons"][id],
#    "message": message_of(root.input), "handoff": (root.metadata or {}).get("handoff_target")` (needs the field group
#    `metadata` too), `"judge_comment"` (the latest judge score's comment)`}`.
#
# Expected: the two login requests (`routing_judge=False`) and the double charge (`control`). The cell runs it twice:
# the second call must add nothing.

# %%
def message_of(value):
    """A root's input as read back: the message string, possibly JSON-encoded."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value.strip()
    if isinstance(value, dict):
        value = value.get("message", "")
    return str(value or "").strip()


def queue_items():
    """Every item of the queue (all runs), as dicts."""
    out, page = [], 1
    while True:
        resp = langfuse.api.annotation_queues.list_queue_items(QUEUE.id, page=page, limit=100)
        out += [i.dict() for i in resp.data]
        if page >= (resp.meta.total_pages or 1):
            return out
        page += 1

# %%
# TODO 2: your code here (fill_review_queue)

# %%
QUEUED = fill_review_queue(ALERT)
n_items = len([i for i in queue_items() if i["objectId"] in {q["trace_id"] for q in QUEUED}])
fill_review_queue(ALERT)
mine = [i for i in queue_items() if i["objectId"] in {q["trace_id"] for q in QUEUED}]
assert len(mine) == n_items == len(QUEUED), "fill_review_queue must be idempotent (the queue API does not dedupe)"
for q in QUEUED:
    print(f"  {q['reason']:<22} {q['handoff']:<19} {q['message'][:60]}  {trace_url(q['trace_id'])}")
print("queue:", QUEUE_URL)

# %% [markdown]
# ## The human review (you, in the UI)
#
# Open the queue (link above; **Annotation Queues** → `incident-review` → **Process queue**). For each item:
# - read the trace: the request, the agent's answer, `metadata.handoff_target`, the `create_handoff` tool call, and the
#   judge's score + reasoning;
# - set **`human_routing_ok`** (was the routing right?), and when it was wrong, **`expected_handoff`** (the team it should
#   have gone to: this becomes the test's expected answer) and **`review_error_type`** (`wrong_team` when it went to
#   the wrong team, `missed_handoff` when it went nowhere);
# - leave a comment on the trace (what you saw), then **Mark Completed**.
#
# Then run the next cell. It waits up to `REVIEW_WAIT_S` for every item to be completed. With
# `SCRIPTED_REVIEW = True` a scripted reviewer (the lab's answer key) labels whatever is still pending, through the
# public scores API (`source="ANNOTATION"`, with the config, the queue, `environment` and `timestamp`); your module 06
# `scoring.validate_score` checks every value first (the API drops an invalid one silently).

# %%
REVIEW_WAIT_S = 0 if SCRIPTED_REVIEW else 900
ROW_BY_TRACE = {r["trace_id"]: r for rows in ROWS.values() for r in rows}


def run_queue_items():
    return [i for i in queue_items() if i["objectId"] in {q["trace_id"] for q in QUEUED}]


def scripted_review(items):
    """The simulated reviewer: the lab's answer key (case.expected_handoff) vs what the agent did."""
    for item in items:
        tid, row = item["objectId"], ROW_BY_TRACE[item["objectId"]]
        expected, actual = row["case"].expected_handoff, row["handoff"]
        label = review.error_type(expected, actual)
        values = {NAMES["human"]: (1.0 if label is None else 0.0, "BOOLEAN"), NAMES["expected"]: (expected, "CATEGORICAL")}
        if label:
            values[NAMES["error_type"]] = (label, "CATEGORICAL")
        now = datetime.now(timezone.utc)
        reqs = []
        for name, (value, dtype) in values.items():
            ok, why = scoring.validate_score(CONFIGS[name], value)
            if not ok:
                raise ValueError(f"the scripted reviewer would send an invalid {name}: {why}")
            reqs.append(CreateScoreRequest(id=f"{tid}-{name}", name=name, value=value, data_type=dtype,
                                           config_id=CONFIGS[name]["id"], trace_id=tid, source="ANNOTATION",
                                           queue_id=QUEUE.id, environment=ENV, timestamp=now,
                                           comment=f"scripted reviewer: expected {expected}, agent chose {actual}"))
        langfuse.api.scores.create(request=reqs)
        langfuse.api.comments.create(project_id=PROJECT_ID, object_type="TRACE", object_id=tid,
                                     content=f"**Review ({INCIDENT}, scripted):** {'ok' if label is None else label}: "
                                             f"expected {expected}, the agent chose {actual}.")
        langfuse.api.annotation_queues.update_queue_item(QUEUE.id, item["id"], status="COMPLETED")
    return len(items)


deadline = time.time() + REVIEW_WAIT_S
while True:
    pending = [i for i in run_queue_items() if i["status"] != "COMPLETED"]
    if not pending or time.time() > deadline:
        break
    print(f"{len(pending)} item(s) still pending: review them at {QUEUE_URL} (checking again in 20 s)")
    time.sleep(20)
if pending and SCRIPTED_REVIEW:
    print("scripted reviewer labelled", scripted_review(pending), "item(s)")
elif pending:
    raise RuntimeError(f"{len(pending)} item(s) still pending in {QUEUE_URL}: review them in the UI and re-run this "
                       "cell (or set SCRIPTED_REVIEW = True)")
# the labels, read back (ingestion is async)
for _ in range(30):
    HUMAN = {}
    for q in QUEUED:
        rows = [s.dict() for s in langfuse.api.scores.get_many(trace_id=q["trace_id"], fields="details,annotation",
                                                                limit=100).data]
        h = [s for s in rows if s["name"] == NAMES["human"] and s["source"] == "ANNOTATION"]
        if h:
            HUMAN[q["trace_id"]] = judging.as_verdict(max(h, key=lambda s: str(s["timestamp"]))["value"])
    if len(HUMAN) == len(QUEUED):
        break
    time.sleep(3)
JUDGED = judge_verdicts([q["trace_id"] for q in QUEUED])
AGREEMENT = review.agreement([(JUDGED[t]["ok"], HUMAN.get(t)) for t in JUDGED])
for q in QUEUED:
    print(f"  {q['reason']:<22} judge {JUDGED[q['trace_id']]['ok']!s:<5} human {HUMAN.get(q['trace_id'])!s:<5} "
          f"{q['message'][:60]}")
print("human vs judge:", {k: AGREEMENT[k] for k in ("n", "agree", "judge_flagged", "human_flagged", "judge_false_alarm",
                                                     "judge_missed")})

# %% [markdown]
# ## TODO 3: the confirmed failures become regression tests
#
# Write `labels_to_dataset(queued) -> (items, pin)`:
# 1. For each queued row: its scores (`fields="details,subject,annotation"`, `.dict()`) →
#    `{"trace_id", "message", "handoff", "scores"}`; then `items = loop.incident_items(reviews, names={"human":
#    NAMES["human"], "expected": NAMES["expected"], "error_type": NAMES["error_type"]}, dataset_name=DATASET,
#    incident=INCIDENT)` (your TODO 1c).
# 2. For each item: the stored one (`langfuse.api.dataset_items.get(item["id"]).dict()`, `NotFoundError` → None) and
#    upsert only when `experiments.changed(stored, item)` (module 09: an upsert always creates a new item version):
#    `langfuse.create_dataset_item(dataset_name=DATASET, **item)`. Then `langfuse.flush()`.
# 3. Wait until every item is listed among `active_items()` (given), then pin the dataset: `pin = max(i.version for i
#    in active_items())`, a **server** timestamp (module 09: never pin with this machine's clock). Return
#    `(items, pin)`.
#
# Expected: 2 items (both login requests, expected `account_security`). The cell calls it twice: the second call writes
# nothing, so the pin doesn't move.

# %%
# TODO 3: your code here (labels_to_dataset)

# %%
ITEMS, PIN = labels_to_dataset(QUEUED)
again, pin2 = labels_to_dataset(QUEUED)
assert [i["id"] for i in again] == [i["id"] for i in ITEMS] and pin2 == PIN, "a re-run must not write new item versions"
DATASET_ID = langfuse.api.datasets.get(DATASET).id
for it in ITEMS:
    print(f"  {it['id']}  expect {it['expected_output']['handoff']:<17} {it['metadata']['error_type']:<15} "
          f"{it['input']['message'][:55]}")
print(f"dataset {DATASET}: {len(active_items())} active items, pinned at {PIN.isoformat()}")
print("open it:", f"{UI}/project/{PROJECT_ID}/datasets/{DATASET_ID}")

# %% [markdown]
# ## Given: the two fixes and the CI gate as a subprocess (modules 04 + 10)
# - `FIXES`: the **quick** fix (a find-and-replace `billing_specialist → account_security`) and the **fix** (production's
#   rules, the clean candidate's login wording, plus "never send login issues to billing"). Each one becomes a prompt
#   version by content, with a commit message.
# - `run_gate(kind)`: `pytest langfuse_mastery/ci` in a separate process, exactly like CI (module 10), with
#   `CANDIDATE_LABEL=hotfix`, the dataset pinned at `PIN` (so the gate runs the incident's new tests), your policy as
#   `GATE_*` variables (`loop.gate_env`) and the run tag. Returns `(exit code, gate-report.json as a dict)`.
#   About 20 agent turns for the first gate (the production baseline, the candidate, re-runs of suspects), about 8 for
#   the second (the baseline is cached).

# %%
FIXES = {
    "quick": (prompts.QUICK_FIX_TEMPLATE, "production loop: quick fix: replace billing_specialist with "
                                          "account_security in the routing rules"),
    "fix": (prompts.LOOP_FIX_TEMPLATE, "production loop: restore login -> account_security (with or without a customer "
                                       "id) and never route login issues to billing"),
}


def run_gate(kind):
    folder = OUT / "gate" / kind
    folder.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CANDIDATE_LABEL": HOTFIX, "GATE_PREFIX": PREFIX, "GATE_RUN_TAG": RUN_TAG,
           "GATE_RUN_ID": f"{RUN_ID}-{kind}-{uuid.uuid4().hex[:4]}", "GATE_DATASET_VERSION": PIN.isoformat(),
           "GATE_ARTIFACTS": str(folder), "GATE_DRY_RUN": "1" if DRY_RUN else "", **loop.gate_env(loop.LOOP_POLICY)}
    r = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/ci", "-q", "-p", "no:cacheprovider",
                        "--tb=no", "--color=no"], cwd=LAB_ROOT, env=env, capture_output=True, text=True, timeout=1800)
    print(f"$ CANDIDATE_LABEL={HOTFIX} pytest langfuse_mastery/ci  ->  exit {r.returncode}")
    summary, rep = folder / "gate-summary.md", folder / "gate-report.json"
    print(summary.read_text(encoding="utf-8") if summary.exists() else r.stdout[-2000:] + r.stderr[-2000:])
    return r.returncode, (json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else None)

# %% [markdown]
# ## TODO 4: release a fix: candidate → gate → decision → promote (or not)
#
# Write `release(kind, template, why) -> dict`:
# 1. `version = ensure_version(template, why)`; put it up for review: `move(version, [HOTFIX], "candidate", ...)`.
# 2. `exit_code, report = run_gate(kind)` (a `None` report means the gate crashed: treat it as `{}`).
# 3. `decision = loop.promotion_decision(report, exit_code, incident_ids=[i["id"] for i in ITEMS],
#    policy=loop.LOOP_POLICY)` (your TODO 1e).
# 4. Promote only when `decision["promote"]`: `move(version, [PRODUCTION, LIVE], "promote", <why: the gate experiment
#    id and its decision>)`. Otherwise print the reasons: this is what the PR author reads.
# 5. Return `{"kind", "version", "exit_code", "report", "decision", "promoted": bool}`.
#
# The quick fix first: the gate must block it (it regresses the double charge) and nothing may be promoted. Then the
# real fix: it must ship and be promoted. If a fix you believe in is blocked, read the summary before touching the
# policy: a "regression" on an item whose label contradicts the routing policy is a **test bug** (fix the label, a new
# item version, module 09), never a reason to loosen `max_regressions`.

# %%
# TODO 4: your code here (release)

# %%
RELEASES = {"quick": release("quick", *FIXES["quick"])}
print("quick fix:", RELEASES["quick"]["decision"])
assert not RELEASES["quick"]["promoted"], "the loop promoted a fix the gate should have blocked"

# %%
RELEASES["fix"] = release("fix", *FIXES["fix"])
print("fix:", RELEASES["fix"]["decision"])
assert RELEASES["fix"]["promoted"], "the fix was not promoted: read the gate summary and the reasons above"
FIX = RELEASES["fix"]["version"]
print({label: langfuse.api.prompts.get(PROMPT, label=label).version for label in (PRODUCTION, LIVE, HOTFIX)})

# %% [markdown]
# ## Run: traffic after the promotion, and the alert resolves

# %%
run_window("recovered")
TICKS["resolve"] = tick(WINDOWS["recovered"])
for s in TICKS["resolve"]["sent"]:
    print(s["payload"]["title"], "->", "delivered" if s["delivery"]["ok"] else "FAILED")
_ = (OUT / "ticks.json").write_text(json.dumps(TICKS, indent=1, default=str), encoding="utf-8")

# %% [markdown]
# ## TODO 5: the evidence trail (the incident report)
#
# Leadership asks three questions after an incident: **how long** (to detect, to fix, to recover), **how bad** (who was
# affected) and **how do we know** (show me). The answer must come from the system of record, not from someone's notes:
# write `collect_evidence() -> dict` that **reads back** every stage and returns
# `{"incident": INCIDENT, "run_tag": RUN_TAG, "prompt": PROMPT, "environment": ENV, "judge_mode": JUDGE_MODE,
# "events": [...]}`. One event per stage (two for `fix`, `experiment` and `gate`: one per release), built with the given
# `ev(stage, at, title, numbers, refs=None, links=None)`; `loop_spec.EVENT_SCHEMA` lists the numbers / refs / links each
# stage needs. Where each one lives:
#
# | stage | `at` | numbers / refs | links |
# |---|---|---|---|
# | `baseline`, `recovered` | the window's first root (`window_roots(w)[0]["startTime"]`) | `requests`, `judge_pass_rate` (`weekly_pull.health_metrics(langfuse, WINDOWS[w], SCOPE)["metrics"][JUDGE_METRIC]`), `version` (the roots' `version`) | `prompt`: `prompt_url(...)` |
# | `deploy` | the `deploy` entry of `audit_log()` | `from_version` (GOOD), `to_version` (its version) | `prompt` |
# | `judge` | the latest judge score of the incident window (`judge_verdicts(...)[t]["at"]`) | `requests`, `judge_pass_rate`, `flagged`; refs `trace_ids` (judged False) | one per flagged trace: `trace_url` |
# | `alert` | the firing delivery's `received_at` (`delivered("firing")`) | `rule`, `metric`, `baseline`, `current` (the payload's values), `version`; refs `id` | |
# | `review` | the last queue item's `completedAt` (`run_queue_items()`) | `queued`, `flagged`, `controls`, `confirmed_failures` (human False), `false_alarms` (judge False, human True); refs `queue_id`, `trace_ids` | `queue`: QUEUE_URL |
# | `dataset` | the latest `createdAt` of your items (`api.dataset_items.get`) | `items`, `pinned_version` (PIN); refs `dataset_id`, `item_ids` | `dataset` |
# | `fix` ×2 | the `candidate` entries of `audit_log()` | `kind`, `version` | `prompt` |
# | `experiment` ×2 | the candidate experiment's `start_time` (`experiment(id)`) | `kind`, `candidate_version`, `baseline_version`, `items` (its item count); refs `candidate_experiment_id`, `baseline_experiment_id` (from the release's report) | `candidate`, `baseline`: `experiment_url(id)` |
# | `gate` ×2 | the `ci_gate` score's `timestamp` (`gate_score(experiment id)`) | `kind`, `version`, `decision` (the score's value), `exit_code`, `regressed` (case ids of the report's `regressed` rows); refs `experiment_id` | |
# | `promote` | the `promote` entry of `audit_log()` | `version`, `labels` (`[PRODUCTION, LIVE]`) | `prompt` |
# | `resolved` | the resolved delivery's `received_at` (`delivered("resolved")`) | `rule`, `current` (its current value); refs `id` | |
#
# The given cell after yours orders the events with your `loop.timeline` (TODO 1f), adds its durations and problems,
# and writes `evidence.json`, `incident-report.md` and `incident-report.html` to `loop_out/<run id>/`.

# %%
def ev(stage, at, title, numbers, refs=None, links=None):
    return {"stage": stage, "at": alerting.iso(at), "title": title, "numbers": numbers, "refs": refs or {},
            "links": links or {}}


def window_roots(window):
    """The window's support-request roots as dicts, oldest first."""
    start, end = WINDOWS[window]
    flt = [{"type": "datetime", "column": "startTime", "operator": ">=", "value": start.isoformat()},
           {"type": "datetime", "column": "startTime", "operator": "<", "value": end.isoformat()},
           {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
           {"type": "string", "column": "name", "operator": "=", "value": loop_spec.ROOT_NAME},
           {"type": "arrayOptions", "column": "traceTags", "operator": "any of", "value": [RUN_TAG]}]
    rows = langfuse.api.observations.get_many(fields="core,basic", filter=json.dumps(flt), limit=100).data
    return sorted((verify._row(r) for r in rows), key=lambda r: str(r["startTime"]))


def experiment(experiment_id):
    """One experiment (id, name, startTime, itemCount, metadata) from the public experiments API."""
    rows = langfuse.api.experiments.list(from_start_time=datetime.now(timezone.utc) - timedelta(hours=24),
                                         id=experiment_id, fields="core,metadata").data
    return verify._row(rows[0])


def experiment_url(experiment_id):
    return f"{UI}/project/{PROJECT_ID}/experiments/results?baseline={experiment_id}"


def gate_score(experiment_id):
    """The run-level ci_gate score module 10's record_gate wrote on that experiment (value, timestamp, comment)."""
    rows = [verify._row(s) for s in langfuse.api.scores.get_many(experiment_id=experiment_id, fields="details",
                                                                  limit=50).data]
    return max((s for s in rows if s["name"] == harness.GATE_SCORE), key=lambda s: str(s["timestamp"]))

# %%
# TODO 5: your code here (collect_evidence)

# %%
EVIDENCE = collect_evidence()
T = loop.timeline(EVIDENCE["events"])
EVIDENCE.update(events=T["events"], durations=T["durations"], problems=list(T["problems"]))
EVIDENCE["problems"] += loop_spec.evidence_problems(EVIDENCE)
PATHS = loop_spec.write_report(EVIDENCE, OUT)
# what verify needs besides Langfuse, kept with the report (verify can be re-run later on the stored data)
_ = (OUT / "loop-run.json").write_text(json.dumps({"run_tag": RUN_TAG, "queued": QUEUED, "items": ITEMS, "pin": PIN,
                                                   "releases": RELEASES}, indent=1, default=str), encoding="utf-8")
print(PATHS["markdown"].read_text(encoding="utf-8"))
print("open the report:", PATHS["html"].as_uri())

# %% [markdown]
# Verify reads what Langfuse stored, the receiver's log, your tick reports, your queue rows, items and releases, and
# your evidence file:
# - the traffic: baseline on the approved version, the incident on the planted one, recovered on the promoted fix;
# - live traffic judged, the incident's login requests judged False;
# - one signed firing alert with the stored judge pass rates and the incident's version;
# - the queue holds the window's judge-flagged requests plus a control, no duplicates; every item reviewed;
# - every confirmed failure is a dataset item with the human's expected team, linked to its trace;
# - **planted:** the quick fix was blocked by the gate (a confirmed regression outside the incident's tests) and never
#   promoted or served;
# - the fix shipped through a gate that ran the incident's tests on the pinned version, and was promoted (audited);
# - recovered traffic is healthy and the alert resolved once;
# - your evidence trail matches all of the above;
# - native prompt webhooks (⚠️ until the compose SSRF whitelist is added, module 12).

# %%
verify.check_module_14(RUN_TAG, prefix=PREFIX, environment=ENV, deliveries=RX.log_path, alert_secret=SECRET,
                       ticks=TICKS, queued=QUEUED, items=ITEMS, pin=PIN, releases=RELEASES,
                       evidence=PATHS["evidence"])

# %% [markdown]
# ## Presenting it (the leadership demo)
# Ten minutes, in this order. Open everything before you start; every step is one click from the report.
# 1. **The report** (`incident-report.html`): the headline tiles first. "A bad deploy reached customers at
#    <deploy>; we knew <time to detect> later, a fix was live <time to fix> after the alert, and a fix that would have
#    broken billing never shipped."
# 2. **Customers affected:** click a flagged trace. The customer asked about a login; the `create_handoff` call went
#    to billing. The judge's score and its reasoning sit on the trace.
# 3. **The alert:** the payload names the metric, baseline vs now and the version (prompt-v<bad>). No one read a
#    dashboard to find this.
# 4. **People in the loop:** the `incident-review` queue: the human labels, the control the judge passed, the comments.
#    The judge decides what humans look at; humans decide what becomes a test.
# 5. **Failures become tests:** the dataset items, each linked to the conversation it came from.
# 6. **The gate:** the quick fix's experiment next to the baseline: the double-charge row is red; `ci_gate = block`
#    with the reason. Then the fix: `ship`. "This is what stops the second incident."
# 7. **Promotion with an audit trail:** Prompts → `support-agent-system`: `production` + `live` on the fix; the comments
#    on each version say who moved what, when and why.
# 8. **Healthy again:** the recovered window's judge pass rate, the resolved alert.
#
# Close with the ask, not the tool: what it would take to run this loop on every agent (the judge on all traffic, an
# owner for the queue, the gate on every PR, a policy for who may move `production`).

# %% [markdown]
# ## UI tour
# - [ ] **Traces**: filter on your run tag, add the *Version* and *Scores* columns: three versions in three windows.
# - [ ] **Annotation Queues → `incident-review`**: the completed items, the labels, who labelled them.
# - [ ] **Datasets → `support-regression`**: the incident items (metadata `incident`, *source trace* links) and the
#   experiments of both gates; select the quick fix's candidate and its baseline → **Compare**.
# - [ ] **Prompts → `support-agent-system`**: versions, labels (`production`, `live`, `hotfix`, `staging`), the commit
#   messages, and the **comments** on the versions (the audit trail).
# - [ ] **Scores**: `ci_gate` on both candidate experiments (`block` with its reasons, `ship`).
# - [ ] `loop_out/<run id>/`: `deliveries.jsonl` (the alert and the resolve), `gate/quick/gate-summary.md` (what the PR
#   author saw), `incident-report.html`.
#
# ## Production notes
# - **Mitigate first, then fix.** Here the loop fixes forward to show every stage. In a real incident the first move is
#   the rollback (one label move, module 12) when the previous version is known to be good; fix forward when the bad
#   deploy carries something you need. Either way the failures become tests.
# - **Close the gap that let it in.** The deploy skipped the gate. Make `live` follow only `production`, and only the
#   gate's promotion step move `production` (protected labels are Enterprise on self-hosted, module 13: enforce it in
#   your deploy tooling, and keep the audit comments).
# - **The judge is the production signal**, so it needs its own care: calibration against humans (module 07/08),
#   controls in every review batch, alerts on judge outages (`unjudged` requests), and a budget.
# - **Humans label, machines triage.** The queue holds what the judge flagged plus controls; the reviewer's
#   `expected_handoff` is what turns a conversation into a test. A judge false alarm is a judge bug, not an agent test.
# - **A blocked good fix is often a bad test.** During validation the gate blocked the right fix on a mislabelled item
#   (production happened to match the wrong label). Review regression labels like code; fix the label, keep the policy.
# - **A gate checks "nothing that worked broke"; promotion also needs "the incident is fixed".** Keep both rules, and
#   keep the policy as reviewed data (`LOOP_POLICY`): who may loosen it is a governance decision.
# - **Evidence from the system of record.** The report is rebuilt from Langfuse and the alert log, with links; a
#   post-mortem written from memory is a story, this is an audit.
# - **Why it matters (leadership):** the same loop that caught this incident in minutes turns every incident into a
#   permanent test, stops a plausible-looking bad fix before customers see it, and produces its own audit trail: AI
#   quality becomes an operated process with measurable time to detect and time to recover, not a hope.

# %% [markdown]
# ## When you're done: stop the receiver (frees port 80)

# %%
RX.stop()
print("receiver stopped")
