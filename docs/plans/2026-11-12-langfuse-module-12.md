# Langfuse Mastery: Module 12 (Alerts & automations) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 06–11 plans.

**Goal:** module 11 produces numbers and a weekly report. Module 12 makes them **page someone**: an alert fires when
quality, latency or cost goes wrong, reaches a webhook the team owns, carries enough context to act on, fires **once**
(not every minute), says when it is over, and points at the evidence. The learner:
- configures Langfuse's **native alerts ("monitors")** and **automations** in the UI from a printed spec: a score
  monitor (routing pass rate), latency and spend guard rails, a webhook automation for alert events, and a **prompt
  automation** that announces label moves (deploys);
- builds the **DIY alert job** for what a fixed-threshold monitor can't express: "worse than the baseline window",
  per version, with dedup, cooldown and resolve, an HMAC-signed webhook with retries (`alerting.py`, pytest;
  `alert_job.run_once`, on top of module 11's `weekly_pull.health_metrics` + `analytics.compare/flag`);
- writes **triage**: from an alert payload (DIY or native) to the failing traces in its window.

Scenario (design row 12): "Alert firing on a planted drop, calling a local webhook". **Planted failure:** a deploy
moves the prompt label `live` to module 04's bad version (login → billing). The DIY job must fire **exactly once**
(signed, naming the metric, baseline vs current and the version), not on the clean windows, survive a planted
receiver outage (retry, idempotent), stay quiet on the repeat tick (cooldown) and resolve after the rollback. The
native score monitor must ALERT once on the same traffic (when the stack can deliver to the host, see below).

**Prerequisite:** module 11 TODOs 1–2 (`analytics.py`, `weekly_pull.scope_filters` / `health_metrics`); the first
cell checks them. Module 04's prompt need not exist: a GIVEN cell get-or-creates the two versions by content
(labels `production` / `staging` only set when missing) and moves only this module's own label `live`.

**Conventions:** as in modules 07–11. Project-global names are `PREFIX + base`; the learner uses `PREFIX = ""`,
validation `zz_ref_m12_`.

| object | name (`PREFIX=""`) |
|---|---|
| managed prompt (module 04) | `support-agent-system`; this module moves label **`live`** only |
| stable traffic tag (native monitors filter on it) | `alerts-lab` |
| native monitors (UI) | `routing-pass-rate`, `request-latency-p95`, `spend-5m` |
| native automations (UI) | `alerts → local webhook` (monitor events), `prompt deploys → local webhook` (prompt events) |
| webhook receiver paths | `/m12/monitor`, `/m12/prompt` (native), `/m12/diy` (DIY job) |
| secrets (.env) | `M12_MONITOR_WEBHOOK_SECRET`, `M12_PROMPT_WEBHOOK_SECRET` (shown once by the UI) |

**Verified live (probe, 2026-11-12, server 4.77 / SDK 5.2; server code read inside the containers, read-only):**
- **Native alerts exist on OSS v4.77** ("Alerts" page, `/project/<id>/alerts`): a *monitor* is one Metrics-API-v2
  query (view `observations` / `scores-numeric` / `scores-boolean` / `scores-categorical`, one metric =
  measure + aggregation (no histogram), filters) over a sliding **window** (5m, 10m, 15m, 30m, 1h, 2h, 4h, 1d, 2d,
  1w), a **threshold operator** (GT, GTE, LT, LTE, EQ, NEQ), an **alert threshold** and an optional **warning
  threshold**, a **no-data mode** (SUBSTITUTE_ZERO default, LAST_SEVERITY, SHOW_NO_DATA, NOTIFY_NO_DATA + interval),
  **renotify** (OFF or EVERY n minutes) and **≥1 automation** ("At least one automation is required").
  - The worker's MonitorRunner ticks every 30 s; cadence is **1 min** for windows < 1 day (30 min ≥ 1d, 48 h for
    1w); the evaluated window is `[runAt − 30 s − window, runAt − 30 s]` (`monitorEvaluationOffsetMs` = 30 s).
  - Severity state machine UNKNOWN / OK / WARNING / ALERT / NO_DATA (+ PAUSED, ERROR_BAD_QUERY): an alert is
    emitted only on a **change** (cold start UNKNOWN → OK is silent; OK ↔ WARNING ↔ ALERT always emits, recovery
    included; ALERT → ALERT only with renotify EVERY). No data counts 0 under SUBSTITUTE_ZERO: a pass-rate monitor
    must use SHOW_NO_DATA, or silence pages you.
  - Entitlements: plan `oss` has `monitor-count` unlimited (`false`); **Cloud spend alerts** (`cloud-spend-alerts`)
    are Cloud-only (billing).
  - **No public API and no SDK client** for monitors or automations (tRPC `monitors.*`, `automations.*` only, scopes
    `alerts:CUD`, `automations:CUD`): configured in the UI. Verify checks the **deliveries** instead.
- **Automations** = trigger (event source `prompt` | `monitor` | `project-notification` | `skill`, actions
  created/updated/deleted, filter) + action (`WEBHOOK`, `SLACK`, `GITHUB_DISPATCH`, `PAGERDUTY`). Slack needs the
  Slack app (`SLACK_CLIENT_ID`…, not configured here); GitHub/PagerDuty are external: notes only.
- **Webhook delivery** (worker `queues/webhooks.js`): POST JSON, headers `content-type: application/json`,
  `user-agent: Langfuse/1.0`, `x-langfuse-signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "t.body")>` (secret
  `lf-whsec_…`, the module 09 scheme), signed once per job; 4 attempts with exponential backoff, timeout
  `LANGFUSE_WEBHOOK_TIMEOUT_MS`; 5 consecutive failures **disable** the trigger. Monitor alerts don't write
  execution rows (a Redis counter); prompt deliveries do (UI: automation → executions).
- **Payloads** (generated with the server's own zod schemas inside the worker, used as test fakes):
  - monitor: `{"id", "timestamp", "type": "monitor-alert", "apiVersion": "v1", "payload": {monitorId, projectId,
    permalink, dataPermalink, message: {title: "[ALERT] <name>", body}, severity, timestamp, fromTimestamp,
    toTimestamp, view, filters, window, monitorName, metric: {measure, aggregation}, metricValue,
    thresholdOperator, alertThreshold, warningThreshold, alertedAt}}`;
  - prompt: `{"id", "timestamp", "type": "prompt-version", "apiVersion": "v1", "action", "prompt": {id, name,
    version, projectId, createdBy, labels, prompt, type, config, commitMessage, tags, createdAt, updatedAt}}`
    (optional `user` before `prompt`). A label move emits `updated` for **every touched version** (the one that
    gains the label and the one that loses it): filter the trigger on `labels any of [live]`.
- **SSRF (the blocker):** `validateWebhookURL` runs when the automation is **saved** (web, tRPC → "Invalid webhook
  URL: …") and on **every delivery** (worker). Only ports 80/443; hostnames `localhost`, `*.internal`,
  `host.docker.internal`, `gateway.docker.internal` are blocked and every resolved IP in 10/8, 127/8, 172.16/12,
  192.168/16, … too, unless the host is in `LANGFUSE_WEBHOOK_WHITELISTED_HOST` (or `LANGFUSE_WEBHOOK_ALLOWED_HOSTS`).
  Ran the server's validator inside the worker: `http://host.docker.internal/x` → `blocked-hostname`,
  `http://192.168.65.254/x` and `http://127.0.0.1/x` → `blocked-ip`, `:8080` → `port-not-allowed`; the env
  whitelist is empty. With `{hosts: ["host.docker.internal"]}` passed in, `http://host.docker.internal/m12/...`
  is allowed. **The compose file has no whitelist, so native webhooks cannot reach this machine** (and a monitor
  cannot be saved without an automation). Not changed (module 11 rule): verify raises `FeatureUnavailable` with the
  exact lines:
  `LANGFUSE_WEBHOOK_WHITELISTED_HOST: host.docker.internal` in the `&langfuse-worker-env` block (langfuse-web
  inherits it), then `docker compose --env-file .env -f ch09/docker-compose.langfuse.yml up -d`.
- **Network path:** a receiver on the Windows host bound to **127.0.0.1:80** is reachable from the worker container
  as `http://host.docker.internal:80/...` (192.168.65.254; arrives as client 127.0.0.1). Port 80 is free here.
- **Prompt labels:** `update_prompt(name, version, new_labels=["live"])` **adds** the label to that version and
  removes it from the version that had it (`latest` is automatic on the newest version).

---

### Task 1: lab (non-Langfuse)
- `traffic.alert_cases(*, run_id)`: 4 requests × 4 windows: `A` baseline, `B` clean, `C` deploy (served by the bad
  version: tag `wrong_handoff`), `D` after the rollback. Requests: login cust_02 + locked-out cust_03
  (account_security), double charge 8129 (billing), shipping 8103 (none). ≈11 Azure calls per window.
- `oracle_llm`: the oracle reads the **system prompt's login rule**, so a dry run of the bad version misroutes login
  like the real agent (module 11's behaviour is unchanged: its prompt is the good one).
- `webhook_receiver.Receiver`: a threaded local receiver (127.0.0.1:80, fallback port), JSONL log of every attempt
  (raw body, lower-cased headers, status, envelope id/type, signature check, duplicate flag), per-path secrets,
  `fail_next(path, n)` (a planted outage: 503), dedupe by envelope id (ack duplicates), signature tolerance 300 s.
- `alert_spec.py`: the native spec as data (`MONITORS`, `AUTOMATIONS`, `render(prefix, …)`), `native_webhooks_ready()`
  (reads the compose file for the whitelist), `NATIVE_HINT` (the exact lines), `signature_ok(...)`.
- Tests. Commit: "lab: module 12 alert cases, oracle reads the prompt's login rule, webhook receiver, native alert spec".

### Task 2: verify: module 12 checks
`module_12_checks(run_tag, client, *, ticks, deliveries, diy_secret, triage=None, prefix="", environment="dev",
allow_dry=False, native_secrets=None, expected_per_window=4)`; one snapshot: all run observations (module 11's
`run_observations`), scores per trace, the `live` label's version, the receiver log.
1. traffic: 4 × 4 roots, A/B/D on one version, C on another (tag `wrong_handoff`), `live` back on A's version.
2. the job's numbers (each tick's baseline/current requests, pass rate, cost per request, request p95) = stored data.
3. clean windows stay quiet (tick `clean`: no notifications; no firing delivery on B or D).
4. **planted:** exactly one accepted firing delivery of `routing-quality-drop`, signed with the DIY secret,
   metric/baseline/current/version = stored data; other rules only if the stored data agrees.
5. retried through the planted outage (≥1 failed attempt, same id, accepted once).
6. dedup + cooldown (tick `repeat`: nothing sent, the fingerprint suppressed).
7. resolve (tick `recovered`: one resolved delivery after the firing one).
8. triage = the stored failing traces of window C.
9. native score monitor → webhook (⚠️ `FeatureUnavailable` + exact compose lines while the stack can't deliver).
10. native prompt webhook on the deploy and the rollback (⚠️ likewise).
11. Langfuse Cloud spend alerts (⚠️ Cloud only).
Commit: "lab: module 12 checks (...)".

### Task 3: learner `langfuse_mastery/alerting.py` + tests
Given `ALERT_RULES`; pure `to_alerts`, `fingerprint`, `decide` (dedup / cooldown / renotify / resolve), `alert_scope`
(DIY + native envelopes), `build_payload`, `deliver` (signed POST, retries, injectable `post`/`sleep`). Commit:
"mastery: module 12 alerting stubs + tests (learner implements)".

### Task 4: `alert_job.py`, notebook `src/12_alerts_automations.py`, validation
`alert_job.py`: given `snapshot`, state I/O, CLI; learner TODO 3 `run_once`. Notebook: prerequisite + TODO 1; given
receiver, instrumentation, prompt + `deploy`; TODO 2 native config in the UI (spec printed; skipped with the compose
hint while blocked); traffic A, B, deploy, C, rollback, D; TODO 3 four ticks (tick 3 as a cron-style subprocess, tick
2 through a planted receiver outage); TODO 4 triage; native deliveries (when enabled); verify; UI tour; notes.
Budget: 16 agent turns (≈44 Azure calls); dry rehearsal first (`ALERTS_DRY_RUN=1`, 0 Azure), then one live run.

---

### As implemented (2026-11-12)

- **Files:** lab `alert_spec.py` (+ `VERSION_PREFIX`, `agent_version` / `prompt_version_of`, `DIY_SECRET_ENV`),
  `webhook_receiver.py`, `oracle_llm` login rule, `traffic.alert_cases` (+ `tests/test_alerts_lab.py`);
  `verify.module_12_checks` / `check_module_12` (+ `tests/test_verify_m12.py`, 25 tests, a real `Receiver` on a free
  port writes the log verify reads); learner `alerting.py` (TODO 1: 6 functions, 20 tests) and `alert_job.py`
  (TODO 3 `run_once`, 6 tests that fake module 11's metrics); notebook TODO 2 (native monitors + automations in the
  UI, spec printed) and TODO 4 (`triage`).
- **Version contract:** a trace's `version` is `prompt-v<n>`, the prompt version `live` served it (that is what the
  alerts name); the generations are linked to the prompt (`propagate_attributes(prompt=)`).
- **Rehearsal:** the whole notebook ran first with `DRY_RUN = True` (oracle LLM, 0 Azure calls), `run:m12-78dfe1`.
  It found a real design bug: Langfuse stores start times **truncated to milliseconds**, the windows came from
  `datetime.now()` (µs), and the oracle is fast enough that each window's first trace started inside the window's
  first millisecond (window C opened at 13.300512, its first root is stored as 13.300). The Metrics API and the
  observations filter counted it (they compare at ms precision), verify didn't. Fix: verify floors window bounds to the
  millisecond like the server (regression test added), and the notebook opens each window on a whole millisecond.
  Re-verified the stored dry data with the fix: every check passed except the two signature checks, which need the
  run's random DIY secret (not kept; the receiver had logged `signature_ok=True` at receipt).
- **Validation:** one live run `run:m12-19430a` (`PREFIX="zz_ref_m12_"`, module 11 + 12 references swapped in,
  `M12_DIY_SECRET` pinned) printed **MODULE PASSED** on the first try: 8 ✅, 3 ⚠️ (native monitor, native prompt
  webhook: compose has no SSRF whitelist; Cloud spend alerts). Real behaviour: every request routed correctly in A, B
  and D on v1; on v2 both login requests went to `billing_specialist` (pass rate 1 → 0.5), double charge and shipping
  stayed right; cost per request $0.00046–0.00055 and request p95 4.3–5.5 s in every window (no other rule fired).
  Tick 2 fired once after one planted 503 (attempt 2, same id and body), the cron-style tick 3 suppressed it
  (cooldown), tick 4 sent one `resolved`; triage returned exactly the two misrouted login traces.
- **Azure:** ≈44 agent LLM calls (16 turns) in the live run; 0 in the rehearsal and the read-only probes.
- **Leftovers (deletes blocked):** prompt `zz_ref_m12_support-agent-system` (v1 good: production + live, v2 bad:
  staging + latest); probe prompt `zz_probe_m12_prompt` (v1 live + production, v2 staging + latest) from the probe
  phase; the traces of `run:m12-78dfe1` (dry) and `run:m12-19430a`. No score configs, datasets, monitors or
  automations were created (the automation form refuses `host.docker.internal` on this stack).
