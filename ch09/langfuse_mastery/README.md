# Langfuse mastery

Hands-on modules that instrument and evaluate the `langfuse_lab` LangGraph support agent with a self-hosted Langfuse.
Each module is a notebook with TODO cells for you to complete, followed by a `verify` cell.

## Prerequisites (one-time; run from the repo root)

1. **Start the Langfuse stack**
   `docker compose --env-file .env -f ch09/docker-compose.langfuse.yml up -d`
   Then open http://localhost:3000.
2. **Create the venv**
   `uv venv ch09/.venv-langfuse --python 3.13`
   `uv pip install --python ch09/.venv-langfuse/Scripts/python.exe -r ch09/langfuse_lab/requirements.txt`
3. **Register the kernel**
   `ch09/.venv-langfuse/Scripts/python.exe -m ipykernel install --user --name ch09-langfuse-v5 --display-name "Python (ch09 Langfuse v5)"`
4. **Fill in the repo-root `.env`** with these keys:
   `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`,
   `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`,
   `AZURE_OPENAI_DEPLOYMENT`, `EVAL_JUDGE_MODEL`

## How to work through a module

1. Open `NN_*.ipynb` and pick the kernel **Python (ch09 Langfuse v5)**.
2. Run the cells in order, writing your code in each `# TODO` cell. The hints tell you which APIs to look up.
3. Run the `verify` cell. Each requirement prints ✅/❌/⚠️ and the module ends with `MODULE PASSED`.
   Ingestion is asynchronous, so verify retries for a while before it reports a failure.
4. Do the **UI tour** checklist in Langfuse.

Some modules also have pure functions for you to implement in a `.py` file, with tests: module 03 `cost.py`
(`tests/test_cost.py`), module 04 `prompting.py` (`tests/test_prompting.py`), module 05 `masking.py`
(`tests/test_masking.py`), module 06 `scoring.py` (`tests/test_scoring.py`), module 07 `judging.py` (`tests/test_judging.py`), module 08 `review.py` (`tests/test_review.py`), module 09 `experiments.py` (`tests/test_experiments.py`), module 10 `ci_gate.py` (`tests/test_ci_gate.py`), module 11 `analytics.py` (`tests/test_analytics.py`), module 12 `alerting.py` (`tests/test_alerting.py`), module 13 `ops.py` (`tests/test_ops.py`) and the plan `ops_plan.py` (`tests/test_ops_plan.py`), module 14 `loop.py` (`tests/test_loop.py`). Each notebook runs only its own test file.
Run them from `ch09/`: `.venv-langfuse/Scripts/python.exe -m pytest langfuse_mastery/tests/test_cost.py -q`
(the whole `langfuse_mastery/tests` folder shows failures for modules you haven't done yet).

Module 03 creates project-wide **model definitions** (Settings → Models). Its notebook defines them **before**
sending traffic, because Langfuse prices a generation once, at ingestion.

Module 05 builds its **masked** Langfuse client in TODO 2, and it must be the first client in the kernel: the SDK keeps
one client per process and silently ignores `mask=` / `sample_rate=` on a later `Langfuse(...)`. If its guard fires,
restart the kernel and run the cells in order. Its sampling exercise runs `langfuse_mastery/_sampling_worker.py`
(TODO 4a is in that file) as a separate process.

Module 04 creates the project-wide prompt **`support-agent-system`** (Prompts). It ends with `production` on the good
version and `staging` on the bad candidate; module 09 compares those two labels, so leave them there.

Module 07 creates project-wide **evaluators**, an **evaluation rule** and an **LLM connection** (`azure-openai`).
They keep running after the notebook: every new trace tagged `live-eval` is judged by the worker, which costs Azure
calls. Later modules opt traffic in with that tag. To pause judging, disable the rule (Evaluation → rules, or
`api.evaluation_rules.update(id, enabled=False)`). Code evaluators are disabled on this deployment (⚠️): the worker
needs `LANGFUSE_CODE_EVAL_DISPATCHER`.

Module 08 creates a project-wide **annotation queue** (`routing-review`) and two score configs (`human_routing_ok`,
`review_error_type`). A queue **cannot be edited or deleted through the API**, so get its configs right the first
time. If your module 07 rule is enabled, module 08 tags its traffic `live-eval` and uses the real judge (≈10 worker
judge calls); otherwise a given, labelled **stand-in keyword judge** writes the judge scores. Review at least one
item in the UI (Annotation Queues → `routing-review` → Process queue); TODO 4 scripts the rest.

Module 09 creates a project-wide **dataset** (`support-regression`); datasets **cannot be deleted through the API**
(items can be archived). If module 04's prompt or module 06's `handoff_correct` config don't exist yet, a given cell
creates them (get-or-create by content / by name), so you can run 09 first. It runs about 18 agent turns (≈50 Azure
calls): 4 labelled production requests, then the 6-item dataset under `production` and `staging`, then a 2-item
remote-triggered run. Run each experiment cell once per `RUN_TAG`: the same run name appends to the same experiment.

Module 10 is a **release gate** you run like CI: `pytest langfuse_mastery/ci` with `CANDIDATE_LABEL` set (it is
skipped otherwise, so a plain `pytest` never spends LLM calls). Its TODOs 2-5 are files in `langfuse_mastery/ci/`:
`release_gate.py`, `test_release_gate.py` and the GitHub Actions workflow `release-gate.yml`. The workflow is
committed but **not active** (GitHub only runs `.github/workflows/`, and a runner can't reach `localhost:3000`).
A given bootstrap cell adds a third prompt label, **`candidate`** (a clean version), to `support-agent-system`, and
seeds `support-regression` only if module 09 hasn't built it yet (same items and ids). Set `GATE_DRY_RUN=1` for an
oracle task with no Azure calls. The real gate (staging must exit 1, candidate exit 0) costs about 50 Azure calls;
artifacts land in `langfuse_mastery/ci/artifacts/` (git-ignored). Run it by hand from `ch09/`:
`CANDIDATE_LABEL=staging GATE_RUN_ID=my-run-1 .venv-langfuse/Scripts/python.exe -m pytest langfuse_mastery/ci -q`

Module 11 builds an **analytics** layer over everything modules 06-10 write. It sends the same 7 requests twice
(about 28 Azure calls): a "previous week" on agent version v1 and a "current week" on v2, which ships the planted
`slow_tool`. TODOs 2-3 live in `langfuse_mastery/weekly_pull.py`, a script a cron job could run (it only reads Langfuse):
`.venv-langfuse/Scripts/python.exe -m langfuse_mastery.weekly_pull --env production --out reports/` reports on the
last two ISO weeks; the notebook passes its two windows with `--prev FROM TO --curr FROM TO`. TODO 4 creates a
project-wide **dashboard** `agent-health` with 7 widgets (`agent-health · …`, spec in `langfuse_lab/dashboard_spec.py`)
through `api.unstable.dashboards` / `dashboard_widgets`. A given cell get-or-creates module 08's `human_routing_ok`
score config. Reports and exports land in `langfuse_mastery/analytics_out/` (git-ignored). Set
`ANALYTICS_DRY_RUN=1` to rehearse with an oracle LLM (no Azure calls); verify refuses dry traffic. Optional: the blob
storage integration (TODO 5b) needs an **organization** API key from the UI in `.env` as
`LANGFUSE_ORG_PUBLIC_KEY` / `LANGFUSE_ORG_SECRET_KEY`; UI batch export needs `LANGFUSE_S3_BATCH_EXPORT_ENABLED: "true"`
in `ch09/docker-compose.langfuse.yml` (then `docker compose ... up -d`; data is kept).

Module 12 makes the numbers **page someone**. It sends the same 4 requests in four windows (about 44 Azure calls):
a baseline, a clean window, a window after a planted **deploy** (the prompt label **`live`** moves to the bad version of
`support-agent-system`), and one after the rollback. A given cell get-or-creates the prompt's good and bad versions by
content and sets `production` / `staging` only when no version has them; the module moves only `live`. It needs
module 11 TODOs 1-2 (the alert job reuses `analytics.py` and `weekly_pull.health_metrics`). TODO 3 is in
`langfuse_mastery/alert_job.py`, a script a scheduler could run (it only reads Langfuse):
`.venv-langfuse/Scripts/python.exe -m langfuse_mastery.alert_job --env production --state alert-state.json --url
https://… --secret-env ALERT_WEBHOOK_SECRET` compares the last 15 minutes with the same minutes a week ago. A local
**webhook receiver** runs in the kernel on `127.0.0.1:80` (port 80 must be free; the last cell, or stopping the
kernel, frees it); its log, the job's state and the tick reports land in `langfuse_mastery/alerts_out/` (git-ignored).
Native **alerts (monitors)** and **automations** have no public API: you configure them in the UI (TODO 2). They can
only reach the receiver after adding `LANGFUSE_WEBHOOK_WHITELISTED_HOST: host.docker.internal` to the
`&langfuse-worker-env` block of `ch09/docker-compose.langfuse.yml` (langfuse-web inherits it) and running
`docker compose --env-file .env -f ch09/docker-compose.langfuse.yml up -d` (data is kept); until then verify shows ⚠️
for them. Put the automations' signing secrets in `.env` as `M12_MONITOR_WEBHOOK_SECRET` /
`M12_PROMPT_WEBHOOK_SECRET`. Set `ALERTS_DRY_RUN=1` to rehearse with the oracle LLM (no Azure calls); verify refuses
dry traffic.

Module 13 is **production ops** on this stack, which runs the open-source edition: everything that needs an
Enterprise license (organization API keys and the organization management API, SCIM, project-level roles, native data
retention, audit logs, protected prompt labels) is explained, worked around in your plan, and shown as ⚠️ by verify.
TODO 2 is the prod/staging **separation plan** as data (`langfuse_mastery/ops_plan.py`, checked by `ops.lint_plan`).
It sends the same 3 requests as two deployments of one project (about 16 Azure calls), then runs 7 cheap deployments
(no LLM) as separate processes through a local **OTLP tap** (`langfuse_lab/otlp_tap.py`, 127.0.0.1 on a free port) to
measure batching, a planted 6 s Langfuse outage and a killed job; TODO 3 (`make_client`, `finish`) is in
`langfuse_mastery/_ops_worker.py`. TODO 4 erases one run-scoped customer (GDPR) with real trace deletions: the
worker deletes every ~2 minutes, so that cell takes 3-5 minutes. The erasure only ever touches this run's own customer
(`cust-<run id>-02`), and its delete refuses any trace without the run tag. The tap's log lands in
`langfuse_mastery/ops_out/` (git-ignored). Optional TODO 5 (UI): a non-production organization + project and an
**Ingestion** key in `.env` as `M13_STAGING_PUBLIC_KEY` / `M13_STAGING_SECRET_KEY`. Set `OPS_DRY_RUN=1` to rehearse
with the oracle LLM (no Azure calls); verify refuses dry traffic.

Module 14 is the **capstone**: one planted regression flows through every layer, and it runs YOUR code from modules 04 and 06-12 (its prerequisites cell lists what to finish first). The agent serves the prompt label **`live`**; `production` is the version the gate approved. A planted deploy moves `live` to module 04's staging candidate (login → billing); the judge (your module 07 rule if it is enabled, else module 08's stand-in) is the only quality signal; your module 12 alert job pages on the judge pass rate; TODO 2 fills a new annotation queue **`incident-review`** (configs `human_routing_ok`, `review_error_type` and a new `expected_handoff`; queues can't be edited or deleted) with the flagged requests plus a control; you label them in the UI (or set `SCRIPTED_REVIEW = True`); TODO 3 turns the confirmed failures into items of `support-regression` (a given cell first applies module 09's invoice label fix if your dataset still has module 10's seed label: a wrong label makes the gate block correct fixes); TODO 4 runs two fixes (label **`hotfix`**) through module 10's gate as a subprocess: the quick fix must be blocked, the fix shipped and promoted (`production` + `live`). Every label move also writes a **comment on the prompt version** (OSS has no audit log). TODO 5 builds the evidence trail; the report lands in `langfuse_mastery/loop_out/<run id>/` (git-ignored: `incident-report.html`, `.md`, `evidence.json`, the receiver log, the gate artifacts). The receiver listens on `127.0.0.1:80` (port 80 must be free). Note that the module moves your `production` and `live` labels of `support-agent-system` (it starts by putting both on the approved version, and ends with them on the promoted fix). About 9 agent turns plus two gate runs on a ~9-item dataset (≈95 Azure calls; with your module 07 rule, ≈18 more judge calls by the worker). Set `LOOP_DRY_RUN=1` to rehearse with the oracle LLM and a dry gate (no Azure calls); verify refuses dry traffic.

## Rebuilding notebooks

The notebooks are generated from percent-format sources in `src/`. To rebuild them, run this from `ch09/`:

`.venv-langfuse/Scripts/python.exe -m langfuse_mastery._nbbuild`   (add `01` to build just one module)

Edit guard: a notebook that has outputs, or that you have edited since it was built, is skipped
(`skip … (edited by you; use --force to overwrite)`). `--force` overwrites it and discards your work.

## Reset (wipes all Langfuse data)

Run from `ch09/`: `.venv-langfuse/Scripts/python.exe -m langfuse_lab.reset --yes`
This does `docker compose down -v`, then `up -d`, then waits until the stack is healthy.

## Modules

| # | Topic | Status |
|---|---|---|
| 00 | Setup & data model | available |
| 01 | Instrumentation (CallbackHandler + manual typed spans, levels, trace URLs) | available |
| 02 | Trace context (sessions, users, tags, metadata, environments, distributed tracing) | available |
| 03 | Cost & tokens (model definitions for Azure, usage + cached tokens, cost per session/user/release, Metrics API) | available. TODO 1 is `cost.py` + pytest |
| 04 | Prompt management (versions, labels, caching + fallback, prompt↔generation links, config, rollback) | available. TODO 1 is `prompting.py` + pytest |
| 05 | Data protection & volume (client-side PII masking, the singleton trap, per-trace sampling, `LangfuseMedia` attachments) | available. TODO 1 is `masking.py` + pytest; TODO 4a is in `_sampling_worker.py` |
| 06 | Scores (configs; trace/observation/session scores; feedback, corrections) | available. TODO 2 is `scoring.py` + pytest |
| 07 | Live evaluation (Azure LLM connection, LLM-as-a-judge evaluators + versions, evaluation rules: filters, sampling, variable mapping; judge calibration; code evaluators ⚠️) | available. TODO 1 is `judging.py` + pytest; needs module 06 TODO 2 (`scoring.py`) |
| 08 | Human review (annotation queue filled by low judge scores + controls, UI and API review with ANNOTATION scores, error taxonomy, comments, human-vs-judge kappa; assignments ⚠️ not readable) | available. TODO 1 is `review.py` + pytest; works without module 07 (stand-in judge) |
| 09 | Datasets & experiments (dataset from labelled traces, item versions + archive + pinned version, item/composite/run evaluators, production vs staging prompt experiment, API compare + gate, signed remote trigger; UI remote config ⚠️, UI prompt experiment optional) | available. TODO 1 is `experiments.py` + pytest; works without modules 04/06 (given get-or-create) |
| 10 | CI/CD gate (pytest release gate on Langfuse experiments: CI metadata, pinned dataset, cached production baseline, re-run suspects + majority vote, `ci_gate` run-level score, JUnit/Markdown artifacts, exit codes; GitHub Actions workflow committed but inactive) | available. TODO 1 is `ci_gate.py` + pytest; TODOs 2-5 are in `ci/`; needs module 09 TODO 1 (`experiments.py`) |
| 11 | Analytics (Metrics API for quality + operations: per-tool latency percentiles, error/level rates, pass rate per score × source, judge vs human, cost per resolved request, version/release/environment; top-N vs client-side aggregation; `agent-health` dashboard as code via the unstable dashboards API; JSONL export; blob storage → MinIO ⚠️ needs an org key (Cloud/Enterprise only, OSS cannot issue one); UI batch export ⚠️ off in compose; weekly pull with planted `slow_tool`) | available. TODO 1 is `analytics.py` + pytest; TODOs 2-3 are in `weekly_pull.py` |
| 12 | Alerts & automations (native monitors: score / latency / spend over a sliding window, no-data modes; webhook + prompt automations ⚠️ need the compose SSRF whitelist; DIY alert job on module 11's metrics: baseline vs current per version, fingerprint, dedup, cooldown, resolve, signed webhook with retries; triage from an alert to the failing traces; planted bad prompt deploy; Cloud spend alerts ⚠️) | available. TODO 1 is `alerting.py` + pytest; TODO 3 is in `alert_job.py`; needs module 11 TODOs 1-2 |
| 13 | Production ops (environments vs projects vs organizations, roles and least-privilege API keys, the separation plan as data + linter, retention per environment as a DIY job (native retention ⚠️ Enterprise), GDPR erasure with async deletion and a planted straggler, SDK batching/timeout/flush measured through an OTLP tap with a planted outage and a killed job, scale-out; org API/SCIM/project roles/audit logs ⚠️ Enterprise) | available. TODO 1 is `ops.py` + pytest; TODO 2 is `ops_plan.py`; TODO 3 is in `_ops_worker.py` |
| 14 | Capstone: the production loop (one planted bad deploy through every layer: live judge → DIY alert → annotation queue + human labels → incident items in the regression dataset → prompt fix → experiment vs production → CI gate (a quick fix that breaks billing is BLOCKED) → audited promotion → recovered traffic + resolved alert; an evidence-trail incident report for leadership; native prompt webhooks ⚠️ until the SSRF whitelist) | available. TODO 1 is `loop.py` + pytest; needs your modules 04 and 06–12 (its prerequisites cell checks them) |
