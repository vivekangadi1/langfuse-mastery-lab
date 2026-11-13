# Langfuse production mastery: design

**Date:** 2026-11-10
**Goal:** Master every Langfuse capability needed in production (observability, evaluation, prompt management, operations), learning by building. A leadership showcase comes later, built from this work.
**Priority:** The user's mastery comes first. The capstone module (14) is designed so it can later become the leadership demo.

## Why

The existing `ch09_example_external_evaluation_pipelines_Langfuse.ipynb` covers about a quarter of Langfuse's observability and evaluation surface: the external scoring pipeline, typed scores, datasets, SDK experiments, run-level evaluators, and batch evaluation. Its traces are synthetic, a single flat span with a JSON payload. That means it can't teach span trees, sessions, cost, live evaluators, human review, dashboards, alerts, or prompt management.

## Decisions

| Decision | Choice |
|---|---|
| Trace source | A real ch09 support agent. The existing cases, datasets, and evaluators carry over. |
| Agent framework | **LangGraph** (`StateGraph`, `AzureChatOpenAI`, `ToolNode`), which the user already knows |
| Learning format | Hybrid. I scaffold everything that isn't Langfuse code; the user writes the Langfuse code from TODOs; I guide. |
| Structure | A module ladder: a shared package plus about 15 numbered notebooks |
| Scope | Observability, evaluation, prompt management, and production ops. Nothing excluded. |
| Environment | Self-hosted Langfuse **v4.77** (`ch09/docker-compose.langfuse.yml`, :3000), Python SDK **v5.2** in `ch09/.venv-langfuse` (kernel `ch09-langfuse-v5`), Azure deployments `gpt-4.1-mini` (agent) and `gpt-5.2-chat` (judge, expensive path) |

## Layout

```
ch09/langfuse_lab/          scaffold I write (not Langfuse skills)
  agent.py      LangGraph support agent, with NO Langfuse code; the user adds instrumentation from the notebooks
  tools.py      lookup_order, lookup_shipment, reset_password, cancel_subscription, search_kb, create_handoff
  data/         ~30 orders/customers, ~20 KB articles, the 8 existing cases, ~6 multi-turn sessions
  traffic.py    synthetic traffic: --n, --env, --release, --fail {wrong_handoff,slow_tool,cost_spike,pii}, --dry-run
  verify.py     check_module_N(): queries Langfuse, prints ✅/❌/⚠️(not in OSS) per requirement, retries ~60s
  reset.py      empties the Langfuse project for a clean start
ch09/langfuse_mastery/
  00_setup.ipynb … 14_capstone.ipynb
  tests/        pytest tests of the user's pure functions (no server needed)
```

The existing ch09 notebooks stay as they are, as reference. Module 06 links to them.

## Agent

```
START → guardrail → agent (AzureChatOpenAI + bound tools) ⇄ tools (ToolNode) → respond → END
                                    └─► handoff → specialist subgraph (separate process in module 02)
```

- **State:** messages, `customer_id`, `retry_count`, `handoff_target`, `steps_completed`. These are the fields the existing evaluators read.
- **Observation types covered:** agent, tool, retriever (`search_kb`), embedding, guardrail, generation, event, span.
- **Instrumentation is taught in two layers:**
  1. The `langfuse.langchain.CallbackHandler` for automatic node, LLM, and tool traces.
  2. Manual `@observe(as_type=...)`, metadata, root trace input/output, and error levels, to fill the gaps the handler leaves.

## Modules

| # | Module | User builds |
|---|---|---|
| 00 | Setup & data model | Verify cell for the server and SDK; checklist of features available in OSS |
| 01 | Instrumentation (callback handler + manual spans, types, levels, trace URLs, flush) | A correct span tree for one request |
| 02 | Trace context (sessions, users, tags, metadata, environments, releases, distributed tracing, agent graphs) | Session and user propagation; handoff across processes continuing the same trace |
| 03 | Cost & tokens (custom model definitions for Azure, usage details) | Model definitions; cost-per-session query |
| 04 | Prompt management (versions, labels, caching, prompt↔generation linking, playground) | System prompt served from Langfuse; promote a version by label |
| 05 | Data protection & volume (masking, sampling, attachments) | Masking function (pytest); sampling configuration |
| 06 | Scores (score configs for all types, trace/observation/session scores, user feedback, corrections) | Score configs; feedback hook |
| 07 | Live evaluation (built-in LLM-as-a-judge, code evaluators, Azure LLM connection, filters, variable mapping) | Evaluators scoring live traffic with no notebook code |
| 08 | Human review (annotation queues, UI scoring, comments, human-vs-AI agreement) | Queue filled by low judge scores |
| 09 | Datasets & experiments (from traces, versioning, item/run/composite evaluators, UI prompt experiments, compare, remote trigger) | Regression dataset; prompt v1 vs v2 experiment |
| 10 | CI/CD gate | pytest / GitHub Actions gate that fails on a planted regression |
| 11 | Analytics (dashboards, custom dashboards, Metrics API, export to UI and MinIO, public API) | Agent health dashboard; weekly metrics pull |
| 12 | Alerts & automations (score, cost, and latency alerts; webhooks; prompt webhooks) | Alert firing on a planted drop, calling a local webhook |
| 13 | Production ops (RBAC, projects, API keys, retention, deletion, env separation, SDK batching, scale-out) | Prod/staging separation plan; retention settings |
| 14 | Capstone: production loop | traffic → judge → alert → review → dataset → prompt fix → experiment → CI → promote, on a planted regression |

Enterprise-only and Cloud-only features (audit logs, SCIM, spend alerts) are covered on paper in module 13. `verify` reports them as ⚠️.

## Data flow

```
traffic.py ─► agent.py (user's instrumentation) ─► Langfuse v4.77
                                                   │
     ┌──────────── online judges (07) ◄────────────┤
     ▼                                             │
 low scores ─► annotation queue (08) ─► dataset (09) ─► experiment ─► CI gate (10)
     │                                                                     │
     └─► alert + webhook (12)              prompt v2 promoted by label (04) ◄┘
```

## Module template

Each notebook has these parts, in order:

1. Concept, with doc links
2. Demo
3. TODO cells (spec + expected result, no solution)
4. Run
5. `verify.check_module_N()`
6. UI tour checklist
7. Production notes, including the "why it matters" line for leadership

## Verification

| Layer | Tool | Requires server |
|---|---|---|
| Unit | `pytest ch09/langfuse_mastery/tests` (masking, mappers, code evaluators, cost math) | No |
| Module | `verify.check_module_N()` (span shapes, sessions, configs, alerts) | Yes |
| Planted failure | `traffic.py --fail X` then verify: the feature must **catch** its failure | Yes |

A module is done only when its verify cell and its planted-failure check both pass.

## Robustness

- **Ingestion lag:** verify retries for up to about 60s.
- **Clean starts:** every module begins with `reset.py` or a fresh run ID, so leftover data can't hide bugs.
- **Azure cost:** `--dry-run` estimates calls; modules default to a small number of conversations (about 20–50 per module, roughly a few dollars).
- **SDK version:** v5 APIs only (`observations.get_many`, `experiment_url`, typed scores). No removed v4 calls.

## Build order

1. Environment check: `langgraph`, `langchain-openai`, and `langchain-core>=1` installed alongside `langfuse` 5.2 in `ch09/.venv-langfuse`, with no conflicts.
2. The `langfuse_lab` scaffold (agent, tools, data, traffic, verify, reset), unit-tested without Langfuse.
3. Modules 00–14 in order. Each is scaffolded just before the user starts it, so later modules can adapt.

## Out of scope (for now)

- Leadership slides and the showcase narrative. They will be derived from module 14 and the per-module production notes.
- Wrapping the agent as a service (FastAPI). It's possible later without rework.
