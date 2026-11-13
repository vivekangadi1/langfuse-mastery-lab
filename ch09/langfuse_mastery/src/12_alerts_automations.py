# %% [markdown]
# # Module 12: Alerts & automations (native monitors, webhooks, a DIY alert job, triage)
#
# **Goal:** module 11 produces numbers and a weekly report. Nobody reads a report at 3 a.m. This module makes the
# numbers **page someone**, the way a production team needs it:
# - an alert fires when quality, latency or cost goes wrong, and reaches a **webhook** the team owns;
# - it carries enough context to act on (metric, baseline vs now, the **version** that broke it, the window);
# - it fires **once**, not every minute, and says when it is **over**;
# - it leads to the **evidence**: the failing conversations behind it.
#
# | Concept | What it is | In our agent |
# |---|---|---|
# | **Monitor** (Langfuse "Alerts") | one Metrics query (view, measure × aggregation, filters) over a **sliding window**, compared with a **fixed threshold** every minute; severity OK / WARNING / ALERT / NO_DATA | `routing-pass-rate`: avg `handoff_correct` over 5 min < 0.9 |
# | **No-data mode** | what an empty window means: `SUBSTITUTE_ZERO` (a sum), `SHOW_NO_DATA` (a rate), `LAST_SEVERITY`, `NOTIFY_NO_DATA` | a quiet night is not a 0% pass rate |
# | **Automation** | trigger (event source + filter) → action (`WEBHOOK`, Slack, GitHub dispatch, PagerDuty) | monitor events and prompt label moves → our webhook |
# | **Prompt automation** | a webhook on every prompt version change (created / updated / deleted) | every deploy and rollback of `live` is announced |
# | **Webhook signature** | `x-langfuse-signature: t=<unix>,v1=<HMAC-SHA256(secret, "t.body")>` | the receiver rejects forgeries and replays |
# | **DIY alert job** | a cron job: current window vs a **baseline** window, per version, with dedup, cooldown, resolve | `alert_job.py` on module 11's metrics |
# | **Fingerprint** | the identity of an incident: rule + metric + scope, never the values | one incident = one page |
# | **Triage** | alert → its window and filters → the failing traces | the two misrouted login requests |
#
# ```
#  traffic: A (baseline) ─ B (clean) ─ deploy: live → bad ─ C ─ rollback ─ D
#       │                                    │
#       ▼                                    └─► prompt automation ──► webhook /m12/prompt   (TODO 2, UI)
#    Langfuse ──► monitor (every minute, fixed threshold) ─► automation ─► webhook /m12/monitor (TODO 2, UI)
#       │
#       └─► alert job (cron): health_metrics(A) vs (B|C|D) ─► to_alerts ─► decide ─► deliver ─► /m12/diy
#           (module 11)                                       (TODO 1: alerting.py)   (TODO 3: run_once)
#                                                                           alert payload ─► triage (TODO 4)
# ```
#
# **The planted failure:** a deploy moves the prompt label **`live`** to module 04's bad version (login → billing).
# Answers look fine, nothing errors, cost doesn't move: only the routing pass rate drops. Your alert job must fire
# **exactly once** (signed, naming the metric, baseline vs current and the version), stay silent on the clean windows,
# survive a planted receiver outage (retry, same id), stay quiet on the next tick (cooldown) and send **resolved** after
# the rollback.
#
# Things verified live on this server (4.77 / SDK 5.2) that shape the module:
# - **Native alerts exist on OSS** ("Alerts" in the left nav), but monitors and automations have **no public API**:
#   you configure them in the UI (TODO 2), and verify checks what they **deliver**.
# - A monitor evaluates every minute over `[now − 30 s − window, now − 30 s]` and notifies only on a **change** of
#   severity (OK → ALERT, ALERT → OK); with re-notify OFF it never repeats itself.
# - Webhooks go to ports **80/443 only**, and the server's **SSRF check** blocks `localhost`, `host.docker.internal`
#   and private IPs unless the host is whitelisted (`LANGFUSE_WEBHOOK_WHITELISTED_HOST`), both when an automation is
#   saved and on every delivery. **This stack's compose file has no whitelist**, so native webhooks cannot reach your
#   machine yet: TODO 2's cell says exactly what to change, and verify shows ⚠️ until you do.
# - Langfuse retries a webhook 4 times with backoff; **5 consecutive failures disable** the automation.
# - A label move emits `updated` for every version it touches: filter the prompt automation on `labels any of [live]`.
# - Start times are stored in whole **milliseconds** (truncated): a window you later compare with stored data must
#   open on a millisecond boundary, or its first trace lands just before it.
#
# Docs: https://langfuse.com/docs/metrics/features/alerts · https://langfuse.com/docs/prompt-management/features/webhooks-slack-integrations ·
# https://langfuse.com/self-hosting/configuration · https://api.reference.langfuse.com

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
from langfuse import get_client
from langfuse_lab import verify, traffic, kb, prompts, alert_spec
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_lab.oracle_llm import OracleChatModel
from langfuse_lab.webhook_receiver import Receiver
from langfuse_mastery import alerting, alert_job, weekly_pull

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings)
langfuse = get_client()
RUN_TAG = f"run:m12-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
ENV = "dev"
OUT = LAB_ROOT / "langfuse_mastery" / "alerts_out" / RUN_ID  # git-ignored: receiver log, job state, tick reports

# Prompts, monitors and automations are PROJECT-GLOBAL. Keep PREFIX empty.
PREFIX = ""
# True = rehearse with the oracle LLM (no Azure calls). verify refuses dry traffic, so the graded run is False.
DRY_RUN = os.getenv("ALERTS_DRY_RUN") == "1"

NAMES = alert_spec.names(PREFIX)     # the stable tag, the monitor and automation names
TAG = NAMES["tag"]                   # on every trace of this module (the native monitors filter on it)
PROMPT = PREFIX + prompts.PROMPT_NAME
fixtures = load_fixtures()
CASES = traffic.alert_cases(run_id=RUN_ID)
print("run tag:", RUN_TAG, "| dry run:", DRY_RUN, "| requests:", len(CASES), "| prompt:", PROMPT)
for window, role, served, failures in traffic.ALERT_WINDOWS:
    print(f"  window {window} ({role}): the {served} prompt version{' (' + ', '.join(failures) + ')' if failures else ''}")
for c in CASES[:4]:
    print(f"    {c.case_id:<16} {c.user_id}  expect {c.expected_handoff:<19} {c.message}")

# %% [markdown]
# ## Prerequisite: module 11 TODOs 1–2
# The alert job measures each window with module 11's code: `weekly_pull.health_metrics` (your TODO 2) and
# `analytics.compare` / `analytics.flag` (your TODO 1). This cell checks they are done.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_analytics.py", "-q",
                         "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
print("module 11 TODO 1 (analytics.py):", (result.stdout.strip().splitlines() or [result.stderr[-300:]])[-1])
try:
    now = datetime.now(timezone.utc)
    probe = weekly_pull.health_metrics(langfuse, (now - timedelta(minutes=1), now),
                                       weekly_pull.Scope(tags=(RUN_TAG,), environment=ENV))
    print("module 11 TODO 2 (health_metrics): ok,", probe["metrics"].get("requests"), "requests in an empty window")
except NotImplementedError as exc:
    print(f"module 11 TODO 2 is not done yet ({exc}): finish weekly_pull.scope_filters + health_metrics first")

# %% [markdown]
# ## TODO 1: implement `langfuse_mastery/alerting.py` (pure functions + pytest)
#
# Module 11's `analytics.flag` answers "what got worse?". An alert job has to turn that into notifications a team can
# live with. Open `ch09/langfuse_mastery/alerting.py` (its docstring shows the **state** the job keeps and the
# **notification** shape) and implement:
# - `to_alerts(anomalies, rules)`: flag rows → alerts with a rule **name** and a **severity** (`ALERT_RULES` is given:
#   same shape as module 11's rules);
# - `fingerprint(alert, scope)`: the identity of an incident (rule, metric, environment, tags; never the values);
# - `decide(state, alerts, scope=, now=, cooldown_s=)`: new → **fire**; still firing within the cooldown →
#   **suppress**; after the cooldown → **renotify**; gone → **resolve**. Never mutates `state`;
# - `alert_scope(envelope)`: where the evidence is, for **our** payloads and **Langfuse's** monitor alerts;
# - `build_payload(...)`: a self-contained webhook body with a **stable id** (a retry carries the same id);
# - `deliver(url, payload, secret, ...)`: one body, signed per attempt the way Langfuse signs its webhooks, retried
#   on 5xx/429/connection errors with exponential backoff, never on other 4xx.
#
# The cell runs the tests and reloads your module. Re-run it until it shows `20 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_alerting.py", "-q",
                         "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
alerting = importlib.reload(alerting)

# %% [markdown]
# ## Given: the webhook receiver
# `langfuse_lab.webhook_receiver.Receiver` stands in for the team's alert endpoint (Slack bridge, PagerDuty, an incident
# bot). It behaves like a production receiver: it **verifies the signature** (and rejects anything older than 5 min:
# replays), **acknowledges a re-delivered id without processing it twice** (webhooks are at-least-once), answers fast,
# and logs every attempt to `deliveries.jsonl` (verify reads that log).
#
# It listens on `127.0.0.1:80`: Langfuse only delivers to ports 80/443, and Docker Desktop forwards
# `host.docker.internal:80` from the containers to this machine. Three paths: `/m12/diy` (your alert job, signed with
# a per-run secret), `/m12/monitor` and `/m12/prompt` (Langfuse's automations, signed with the secrets the UI shows
# you once; put them in `.env` as `M12_MONITOR_WEBHOOK_SECRET` / `M12_PROMPT_WEBHOOK_SECRET`).

# %%
DIY_SECRET = os.environ.setdefault(alert_spec.DIY_SECRET_ENV, "lf-whsec_" + secrets.token_hex(16))
NATIVE_SECRETS = {key: os.getenv(env) for key, env in alert_spec.SECRET_ENV.items()}
NATIVE_PATHS = {"monitor": alert_spec.MONITOR_PATH, "prompt": alert_spec.PROMPT_PATH}
if "RX" in globals():
    RX.stop()  # re-running this cell: free port 80 first
RX = Receiver(OUT / "deliveries.jsonl", secrets={alert_spec.DIY_PATH: DIY_SECRET,
                                                 **{NATIVE_PATHS[k]: s for k, s in NATIVE_SECRETS.items() if s}}).start()
print("receiver:", RX.url("/m12/..."), "| log:", RX.log_path)
print("Langfuse posts to:", RX.docker_url("/m12/...") or "nothing: not on port 80, native webhooks can't reach it")
if RX.bind_error:
    print("note:", RX.bind_error, "(the DIY job still works on this port)")
print("native signing secrets in .env:", {k: bool(v) for k, v in NATIVE_SECRETS.items()})

# %% [markdown]
# ## Given: the instrumentation, the managed prompt and `deploy` (modules 01–06 recap)
# This is a recap, not a TODO:
# - **The prompt (module 04).** `ensure_version` gets the good and the bad version of `support-agent-system` **by
#   content** (create only when missing), and sets module 04's labels `production` / `staging` only when no version
#   has them. This module moves only its own label, **`live`**: the version the agent serves. `deploy(version)` is
#   one `update_prompt(..., new_labels=["live"])`: the label moves, the version that had it loses it. A rollback is
#   the same call with the old version.
# - `traced_turn` (modules 01/02/04/06): one `support-request` trace per request, served by whatever `live` points at
#   **right now** (`cache_ttl_seconds=0`), linked to that prompt version, with `version="prompt-v<n>"` (the trace
#   version is what the alerts name), the stable tag, the run tag, the case tags, and a `handoff_correct` code score.
# - `wait_until_ingested`: roots and scores (module 03's lesson: never compute numbers mid-ingestion).

# %%
from langfuse import observe, propagate_attributes
from langfuse.api import NotFoundError
from langfuse.langchain import CallbackHandler

PROJECT_ID = langfuse.api.projects.get().data[0].id
UI = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")
SCORE = alert_spec.SCORE_NAME


def ensure_version(template):
    """Get-or-create the version with this text and module 04's config (create_prompt never dedupes)."""
    metas = [m for m in langfuse.api.prompts.list(name=PROMPT, limit=100).data if m.name == PROMPT]
    for v in sorted({v for m in metas for v in (m.versions or [])}):
        row = langfuse.api.prompts.get(PROMPT, version=v)
        if row.prompt == template and all((row.config or {}).get(k) == val for k, val in prompts.PROMPT_CONFIG.items()):
            return v
    return langfuse.create_prompt(name=PROMPT, prompt=template, type="text", config=prompts.PROMPT_CONFIG,
                                  commit_message="module 12: get-or-create by content").version


GOOD, BAD = ensure_version(prompts.SYSTEM_PROMPT_TEMPLATE), ensure_version(prompts.WRONG_HANDOFF_TEMPLATE)
for label, version in (("production", GOOD), ("staging", BAD)):  # module 04's labels: only set when missing
    try:
        langfuse.api.prompts.get(PROMPT, label=label)
    except NotFoundError:
        langfuse.update_prompt(name=PROMPT, version=version, new_labels=[label])


def deploy(version, why):
    """A deploy = move the label the agent serves. The rollback is the same call with the previous version."""
    langfuse.update_prompt(name=PROMPT, version=version, new_labels=[alert_spec.LIVE_LABEL])
    print(f"{datetime.now(timezone.utc):%H:%M:%S} deploy: {alert_spec.LIVE_LABEL} -> v{version} ({why})")


def live_prompt():
    """What the `live` label serves right now (cache_ttl_seconds=0: see the production notes on caching)."""
    return langfuse.get_prompt(PROMPT, label=alert_spec.LIVE_LABEL, cache_ttl_seconds=0)


@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


@observe(name="support-request", as_type="agent", capture_input=False, capture_output=False)
def traced_turn(case, agent):
    """One request = one trace, served by the `live` prompt. Returns a row for the run table."""
    prompt = live_prompt()
    tags = [RUN_TAG, TAG, *case.tags, *(["dry-run"] if DRY_RUN else [])]
    with propagate_attributes(session_id=case.session_id, user_id=case.user_id, tags=tags, environment=ENV,
                              version=alert_spec.agent_version(prompt.version), prompt=prompt):
        run = agent.invoke(case.message, config={"callbacks": [CallbackHandler()]},
                           system_prompt=prompt.compile(**prompts.PROMPT_VARIABLES), llm_params=prompt.config)
        actual = run.summary["handoff_target"]
        level, why = ("WARNING", "blocked by the guardrail") if run.blocked else \
            ("WARNING", f"{run.summary['retry_count']} tool error(s)") if run.summary["retry_count"] else ("DEFAULT", None)
        langfuse.update_current_span(input=case.message, output=run.summary["final_answer"], level=level,
                                     status_message=why, metadata={"handoff_target": actual})
        ok = actual == case.expected_handoff
        langfuse.score_current_trace(name=SCORE, value=1.0 if ok else 0.0, data_type="BOOLEAN",
                                     comment=f"expected {case.expected_handoff}, got {actual}",
                                     metadata={"evaluator": "code:v1"})
        return {"case": case, "trace_id": langfuse.get_current_trace_id(), "version": prompt.version,
                "handoff": actual, "ok": ok}


def wait_until_ingested(rows, *, timeout=180, settle=4):
    """Every trace's root and its score, and stable observation counts."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        seen = {}
        for r in rows:
            t = r["trace_id"]
            o = langfuse.api.observations.get_many(trace_id=t, fields="core", limit=200).data
            sc = langfuse.api.scores.get_many(trace_id=t, limit=50).data
            seen[t] = (any(x.parent_observation_id is None for x in o), len(o), len(sc) >= 1)
        if all(root and scored for root, _, scored in seen.values()) and seen == last:
            print(f"ingested: {len(rows)} traces, {sum(n for _, n, _ in seen.values())} observations, scores complete")
            return
        last = seen
        time.sleep(settle)
    print("warning: ingestion still settling; wait a little before the ticks")


deploy(GOOD, "start healthy")
print(f"{PROMPT}: good v{GOOD}, bad v{BAD}; serving", live_prompt().version)

# %% [markdown]
# ## TODO 2: native alerts and automations, in the UI
#
# Langfuse's own alerting lives in the UI (there is no public API for monitors or automations on this server). The
# cell below prints the exact spec for your `PREFIX`. Create, in this order:
# 1. **Automations** (left nav) → two webhook automations: one for **Alerts (monitor events)** → `/m12/monitor`, one
#    for **Prompt versions** (action `updated`, filter: name = the prompt, labels any of `live`) → `/m12/prompt`.
#    Copy each **signing secret** (`lf-whsec_…`, shown once) into the repo-root `.env`, then re-run the receiver cell.
# 2. **Alerts** (left nav) → New alert, three times: the **routing pass rate** (scores-boolean, avg of
#    `handoff_correct`, window 5 min, `< 0.9`, no data = **SHOW_NO_DATA**) is the one the planted deploy must trip;
#    **request latency p95** and **spend per 5 min** are guard rails that must stay quiet here. Each needs at least
#    one automation: pick the monitor automation.
#
# Things to notice while you fill the form: the monitor is ONE Metrics query (module 11) against a FIXED threshold.
# It can't say "worse than last week" or "worse on the new version than on the old one", and a 5-minute pass rate
# over 4 requests is noisy. That's what your DIY job (TODO 3) adds.
#
# **On this stack the webhooks are blocked** (the cell prints `BLOCKED`): the server refuses to save an automation
# whose URL is `host.docker.internal`, and a monitor can't be saved without one. The cell prints the two-line compose
# change that enables it (`LANGFUSE_WEBHOOK_WHITELISTED_HOST`). It is a security decision (it lets the Langfuse
# server call that host), so make it deliberately; until then read the spec, open the New alert form to see its
# preview, skip the saving, and carry on: the rest of the module doesn't depend on it, and verify shows ⚠️ for the two
# native checks.

# %%
READY, WHY = alert_spec.native_webhooks_ready()
print(alert_spec.render(PREFIX, prompt_name=PROMPT, receiver=RX))
print("\nnative webhooks on this stack:", "READY" if READY else "BLOCKED", "-", WHY)
if not READY:
    print(alert_spec.NATIVE_HINT)
elif not all(NATIVE_SECRETS.values()):
    print("missing in .env:", [alert_spec.SECRET_ENV[k] for k, v in NATIVE_SECRETS.items() if not v],
          "(then re-run the receiver cell)")

# %% [markdown]
# ## Run: baseline, clean, the deploy, the rollback
# Four windows of the same 4 requests (two login problems, a double charge, a shipping question; about 44 Azure
# calls in total). Between B and C, `deploy` moves `live` to the bad version; between C and D, the rollback moves it
# back. If your native automations are live, the prompt webhook fires on both moves and the routing monitor should
# go to ALERT within a minute or two of window C.

# %%
agent = SupportAgent(settings, llm=OracleChatModel() if DRY_RUN else None, guardrail_fn=observed_guardrail,
                     search_fn=observed_search)
WINDOWS, ROWS = {}, []
for window, role, served, _failures in traffic.ALERT_WINDOWS:
    if window == "C":
        deploy(BAD, "the planted bad deploy: login -> billing")
    elif window == "D":
        deploy(GOOD, "rollback")
    start = datetime.now(timezone.utc)
    # Langfuse stores times in whole milliseconds (truncated): open the window on one, or a trace that starts 0.3 ms
    # after 13.300512 is stored as 13.300, before its own window.
    start = start.replace(microsecond=start.microsecond // 1000 * 1000)
    rows = [traced_turn(c, agent) for c in CASES if c.window == window]
    langfuse.flush()
    time.sleep(3)  # keep the windows apart
    WINDOWS[window] = (start, datetime.now(timezone.utc))
    ROWS += rows
    print(f"window {window} ({role}): {WINDOWS[window][0]:%H:%M:%S} -> {WINDOWS[window][1]:%H:%M:%S}")
    for r in rows:
        print(f"  {r['case'].case_id:<16} v{r['version']} -> {r['handoff']:<19} {'ok' if r['ok'] else 'WRONG TEAM'}")
    if {r["version"] for r in rows} != {BAD if served == "bad" else GOOD}:
        print(f"  warning: window {window} should be served by the {served} version")
wait_until_ingested(ROWS)
SCOPE = weekly_pull.Scope(tags=(RUN_TAG,), environment=ENV)

# %% [markdown]
# ## TODO 3: one tick of the alert job (`alert_job.run_once`)
#
# `ch09/langfuse_mastery/alert_job.py` is a script a scheduler runs every few minutes. The snapshot (module 11's
# `health_metrics` + cost per request + the window's dominant version), the state file and the CLI are given.
# Implement `run_once(langfuse, baseline, current, scope, *, state_path, url, secret, ...)` (its docstring has the 5
# steps): measure both windows → `analytics.flag` → your `to_alerts` → `decide` against the saved state →
# `build_payload` + `deliver` each notification → save the state. One rule matters: a notification that **failed** to
# deliver must not count as sent (put its state entry back), so the next tick tries again.
#
# The cell runs the tests (they fake module 11's metrics; they need your module 11 TODO 1). Re-run it until it shows
# `6 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_alert_job.py", "-q",
                         "-p", "no:cacheprovider", "--color=no"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
alert_job = importlib.reload(alert_job)

# %% [markdown]
# ### Four ticks, as the scheduler would have run them
# Every tick compares a current window with the **baseline** A:
# 1. **clean** (A vs B): nothing got worse, nothing may be sent;
# 2. **planted** (A vs C): the routing drop fires. The receiver is **down** for the first attempt (a planted 503):
#    `deliver` must retry with the same id and body;
# 3. **repeat** (A vs C again), run the way cron runs it: a **separate process**, the CLI, the state file. The
#    incident is still firing, inside the cooldown: nothing may be sent;
# 4. **recovered** (A vs D, after the rollback): one **resolved** message.
#
# Expected: tick 2 sends `[FIRING critical] routing-quality-drop: score.handoff_correct.API 1 -> 0.5 (version
# prompt-v<bad>)` after 2 attempts; tick 3 prints `1 alert(s): 0 sent, 1 suppressed`; tick 4 sends `[RESOLVED …]`.

# %%
alert_job = importlib.reload(alert_job)
STATE = OUT / "alert-state.json"
STATE.unlink(missing_ok=True)  # a fresh replay: verify matches the receiver's log to these four reports
DIY_URL = RX.url(alert_spec.DIY_PATH)
A, B, C, D = (WINDOWS[w] for w in "ABCD")
LINKS = {"traces": f"{UI}/project/{PROJECT_ID}/traces", "prompt": f"{UI}/project/{PROJECT_ID}/prompts/{PROMPT}"}


def tick(current):
    return alert_job.run_once(langfuse, A, current, SCOPE, state_path=STATE, url=DIY_URL, secret=DIY_SECRET,
                              links=LINKS)


def show(name, r):
    print(f"tick {name}: {len(r['alerts'])} alert(s), {len(r['sent'])} sent, {len(r['suppressed'])} suppressed")
    for s in r["sent"]:
        d = s["delivery"]
        print(f"   {s['payload']['title']} -> {'delivered' if d['ok'] else 'FAILED'} after {d['attempts']} attempt(s)")


TICKS = {"clean": tick(B)}
show("clean", TICKS["clean"])
RX.fail_next(alert_spec.DIY_PATH, 1)  # planted: the receiver answers 503 to the next POST
TICKS["planted"] = tick(C)
show("planted", TICKS["planted"])
cmd = [sys.executable, "-m", "langfuse_mastery.alert_job", "--tag", RUN_TAG, "--env", ENV,
       "--baseline", A[0].isoformat(), A[1].isoformat(), "--current", C[0].isoformat(), C[1].isoformat(),
       "--state", str(STATE), "--url", DIY_URL, "--secret-env", alert_spec.DIY_SECRET_ENV,
       "--report", str(OUT / "tick-repeat.json")]
r = subprocess.run(cmd, cwd=LAB_ROOT, capture_output=True, text=True, timeout=300)
print(f"tick repeat (a separate process, exit {r.returncode}):", (r.stdout or r.stderr)[-1500:])
TICKS["repeat"] = json.loads((OUT / "tick-repeat.json").read_text(encoding="utf-8"))
TICKS["recovered"] = tick(D)
show("recovered", TICKS["recovered"])
(OUT / "ticks.json").write_text(json.dumps(TICKS, indent=1, default=str), encoding="utf-8")
print("\nwhat the receiver saw:")
for row in RX.deliveries(alert_spec.DIY_PATH):
    body = json.loads(row["body"])
    print(f"  {row['received_at'][11:19]} {row['status']} {'(planted outage)' if row['planted_outage'] else ''}"
          f"{'(duplicate)' if row['duplicate'] else ''} signature_ok={row['signature_ok']} {body.get('title')}")

# %% [markdown]
# ## TODO 4: triage, from an alert to the failing traces
#
# "routing pass rate 1 → 0.5 on prompt-v2" is where on-call **starts**. Write `triage(envelope) -> list[dict]`: the
# failing requests behind an alert, for **our** payloads and **Langfuse's** monitor alerts alike:
# - `sc = alerting.alert_scope(envelope)` (your TODO 1) gives the window and the filters; `None` → `[]`.
# - The root observations of that window: `langfuse.api.observations.get_many(fields="core,basic,io",
#   filter=json.dumps([...]), limit=100, cursor=...)`, following `resp.meta.cursor` until it is None. The filter:
#   `startTime` `>=` from and `<` to (type `datetime`, ISO strings), `isRootObservation` `=` True (`boolean`),
#   `name` `=` `"support-request"` (`string`), the tags on column **`traceTags`** (`arrayOptions`, `any of`) and the
#   environment (`string`, `=`) when the scope has them.
# - Per root, its `SCORE` scores: `langfuse.api.scores.get_many(trace_id=..., name=SCORE, fields="details",
#   limit=50)`. The request failed when the **latest** one (by `timestamp`) is false.
# - Return one row per failing request, oldest first: `{"trace_id", "start_time"` (ISO), `"user_id", "input",
#   "verdict"` (the score's comment), `"url": f"{UI}/project/{PROJECT_ID}/traces/{trace_id}"}`.
#
# Expected: the login requests of window C (`expected account_security, got billing_specialist`; with the bad
# version the model sometimes hands off to nobody instead), and nothing from the other windows.

# %%
# TODO 4: your code here (triage)

# %%
FIRING = next(body for row in RX.deliveries(alert_spec.DIY_PATH) if row["status"] == 200 and not row["duplicate"]
              for body in [json.loads(row["body"])]
              if body.get("status") == "firing" and body["id"] in {s["payload"]["id"] for s in TICKS["planted"]["sent"]})
TRIAGE = triage(FIRING)
print("alert:", FIRING["title"])
for t in TRIAGE:
    print(f"  {t['start_time'][11:19]} {t['user_id']}  {t['verdict']:<50} {t['url']}")
# The same function on a Langfuse monitor alert: the real one if your monitor delivered it, else a stand-in with the
# fields Langfuse sends (the monitor's filters and evaluated window). A monitor watches the STABLE tag, not your run:
# any earlier run inside its 5-minute window shows up too.
native = [json.loads(r["body"]) for r in RX.deliveries(alert_spec.MONITOR_PATH) if r["status"] == 200]
MONITOR_ALERT = native[-1] if native else {"type": "monitor-alert", "payload": {
    "monitorName": NAMES["monitor.routing"] + " (stand-in)", "severity": "ALERT",
    "fromTimestamp": (C[1] - timedelta(minutes=5)).isoformat(), "toTimestamp": C[1].isoformat(),
    "filters": [{"column": "name", "operator": "=", "value": SCORE, "type": "string"},
                {"column": "tags", "operator": "any of", "value": [TAG], "type": "arrayOptions"}]}}
print(f"\n{MONITOR_ALERT['payload']['monitorName']}:", [t["trace_id"][:8] for t in triage(MONITOR_ALERT)])

# %% [markdown]
# ## Native deliveries (when the stack can reach this machine)
# If TODO 2 is live, this cell waits up to ~2.5 minutes for the routing monitor's ALERT and shows every native
# delivery: compare a Langfuse `monitor-alert` envelope with your `agent-alert` one. On the blocked stack it says so
# and moves on.

# %%
def native_rows():
    return [(r, json.loads(r["body"])) for r in RX.deliveries() if r["path"] in NATIVE_PATHS.values()]


if READY and all(NATIVE_SECRETS.values()):
    deadline = time.time() + 150
    while time.time() < deadline and not any(b.get("type") == "monitor-alert" and b["payload"].get("severity") == "ALERT"
                                             for _, b in native_rows()):
        time.sleep(10)
    for r, b in native_rows():
        what = (f"{b['payload']['monitorName']} {b['payload']['severity']} {b['payload'].get('metricValue')}"
                if b.get("type") == "monitor-alert" else
                f"{b['prompt']['name']} v{b['prompt']['version']} {b.get('action')} labels={b['prompt']['labels']}")
        print(f"{r['received_at'][11:19]} {r['path']:<13} {r['status']} signature_ok={r['signature_ok']} {what}")
else:
    print("skipped:", "native webhooks are blocked on this stack (TODO 2)" if not READY else
          "the signing secrets are not in .env yet (TODO 2)")

# %% [markdown]
# Verify reads what Langfuse stored, your four tick reports and the receiver's log:
# - the traffic: 4 requests in each window, A/B/D on the good version, C on the bad one, `live` back on the good one;
# - your job's numbers (requests, pass rate, cost per request, request p95, version) match the stored data;
# - the clean windows stay quiet;
# - **planted:** exactly one accepted, correctly signed firing delivery of `routing-quality-drop` whose baseline,
#   current and version are the stored ones (any other rule that fired must agree with the stored data too);
# - it got through the planted outage (a 503 first, then the same id accepted);
# - the repeat tick sent nothing (cooldown), the recovered tick sent one `resolved`;
# - your triage returns exactly the stored failing traces of window C;
# - the native monitor and prompt webhooks (⚠️ while the stack can't deliver), Langfuse Cloud spend alerts (⚠️
#   Cloud only).

# %%
verify.check_module_12(RUN_TAG, ticks=TICKS, deliveries=RX.log_path, diy_secret=DIY_SECRET, triage=TRIAGE,
                       prefix=PREFIX, environment=ENV, native_secrets=NATIVE_SECRETS)

# %% [markdown]
# ## UI tour
# - [ ] **Prompts → `support-agent-system`**: the `live` label sits on the good version again. Open the bad version:
#   its *linked generations* are exactly window C (the traces carried the prompt). That's the deploy in the data.
# - [ ] **Traces**: filter `tags = <your run tag>`, add the *Version* and *Scores* columns: C is `prompt-v<bad>` with
#   `handoff_correct = false` on the login requests. Open one from your triage list: the `create_handoff` tool call
#   says `billing_specialist`.
# - [ ] **Alerts** (left nav) → *New alert*: build the routing monitor's query and look at the preview chart over the
#   last hour: the dip during window C is what a fixed threshold sees. If the webhooks are enabled, open your
#   `routing-pass-rate` monitor and its history (OK → ALERT → OK).
# - [ ] **Automations**: your prompt automation's *executions* (each label move, with the HTTP status). Monitor
#   alerts don't write execution rows on this version.
# - [ ] Open `alerts_out/<run id>/deliveries.jsonl`: the 503, the retry with the same id, the resolved message; and,
#   if enabled, a native `monitor-alert` next to your `agent-alert`. Which one would you rather be woken up by?
# - [ ] Module 11's **`agent-health`** dashboard (if you built it): the pass-rate widget shows the dip and the version.
#
# ## Production notes
# - **Two kinds of alerts.** Native monitors are fixed thresholds on one Metrics query, evaluated every minute: right
#   for guard rails (p95 latency, spend per window, error rate). Regressions ("worse than last week", "worse on the
#   new version") need a **baseline**: a scheduled job like yours, or an experiment gate before the deploy (module 10).
# - **Page on symptoms users feel**, per version: quality (pass rates, judge scores), p95 latency, cost per request.
#   Require a **minimum sample** before paging on a rate (4 requests is anecdote); here the windows are tiny so the
#   lab runs in minutes.
# - **One incident, one page.** Fingerprint on rule + metric + scope (never the values); a cooldown or re-notify
#   interval for long incidents; always send **resolved**. Alert fatigue kills on-call faster than missed alerts.
# - **Make every alert actionable:** metric, baseline vs now, the version, the window, links, and a runbook line.
#   Triage (TODO 4) belongs in the alert bot: "these 2 conversations, opened in Langfuse".
# - **Webhooks are at-least-once.** Verify the signature (HMAC over `t.body`) and the timestamp (replays), dedupe by
#   the envelope id, answer fast (Langfuse times out and retries 4 times), and watch the automation: **5 consecutive
#   failures disable it**, so a broken receiver silently ends your alerting. Monitor the monitor.
# - **No data is not OK.** A pass rate over an empty window must be `SHOW_NO_DATA` (or `NOTIFY_NO_DATA` if silence
#   itself is an incident); `SUBSTITUTE_ZERO` is right only for sums like spend.
# - **Deploys are events.** The prompt automation turns every label move into a message (chat, CI via GitHub
#   dispatch, a deploy log): when an alert fires, the first question ("what changed?") is already answered, and the
#   rollback is one label move. Production clients cache prompts (SDK default 60 s, stale-while-revalidate): a deploy
#   or a rollback reaches every process within the TTL, not instantly.
# - **SSRF whitelisting is a security decision.** Whitelist only your alert router's host
#   (`LANGFUSE_WEBHOOK_WHITELISTED_HOST`), never a whole private range. Langfuse Cloud adds organization **spend
#   alerts** (billing); self-hosted, use a spend monitor.
# - **Why it matters (leadership):** a bad prompt deploy that silently misroutes customers is caught within minutes by
#   one alert naming the version, with the failing conversations a click away, and the fix is a one-line rollback:
#   minutes of exposure instead of a customer escalation days later.

# %% [markdown]
# ## When you're done: stop the receiver (frees port 80)
# Keep it running while you explore the native deliveries; it also stops when the kernel does.

# %%
RX.stop()
print("receiver stopped")
