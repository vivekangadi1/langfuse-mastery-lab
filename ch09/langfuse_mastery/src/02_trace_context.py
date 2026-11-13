# %% [markdown]
# # Module 02 — Trace context: sessions, users, environments, distributed tracing
#
# **Goal:** every trace carries the context you need to *find* it later, and a handoff that runs in a
# **separate OS process** still lands inside the same trace.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **Trace** | One request → response | One customer turn |
# | **Session** (`session_id`) | Traces grouped into a conversation | A multi-turn chat; replayable in the Sessions view |
# | **User** (`user_id`) | Traces grouped by who sent them | `cust_00` … per-customer history, cost, scores |
# | **Tags** | Short, low-cardinality labels you *filter* by | `billing`, `multi_turn`, `env:staging`, your `RUN_TAG` |
# | **Metadata** | Structured, high-cardinality *context* | `intent`, `conversation_id`, `pid`, `ticket_id` |
# | **Environment** | Data isolation: `dev` / `staging` / `production` | `staging` (a filter at the top of every Langfuse view) |
# | **Version / release** | *Which code* produced the trace | `v1` — compare v1 vs v2 later (module 09) |
#
# **In-process vs cross-process context.** Inside one Python process, the SDK keeps "the current span" in a
# `contextvar`: anything you start inside `@observe` nests under it, and `propagate_attributes(...)` stamps
# user/session/tags/metadata/environment/version onto the current span *and every span started inside it*.
# A **separate process has none of that** — contextvars do not cross process boundaries. To continue a trace there,
# you pass the identifiers explicitly (trace id + parent span id) and the child re-applies the context itself.
#
# The target for this module:
#
# ```
# Session  <conversation_id>-s_refund_followup   (user cust_00, environment staging, version v1)
# ├── trace 1: support-request (agent)   "I was charged twice for order 8129."
# │   ├── LangGraph (chain) … AzureChatOpenAI, lookup_order, create_handoff
# │   └── specialist-billing_specialist (agent)   ← started in ANOTHER process (metadata pid ≠ root pid)
# └── trace 2: support-request (agent)   "How long until I see the refund?"
# ```
#
# Docs: https://langfuse.com/docs/observability/features/sessions ·
# https://langfuse.com/docs/observability/features/users ·
# https://langfuse.com/docs/observability/features/tags ·
# https://langfuse.com/docs/observability/features/metadata ·
# https://langfuse.com/docs/observability/features/environments ·
# https://langfuse.com/docs/observability/features/releases-and-versioning ·
# https://langfuse.com/docs/observability/sdk/instrumentation (distributed tracing / trace context)

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` importable whatever the kernel's cwd

import os
import uuid
from langfuse_lab import verify, traffic, specialist
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent

settings = Settings.from_env()  # loads the repo-root .env into os.environ (Langfuse keys + Azure settings)
RUN_TAG = f"run:m02-{uuid.uuid4().hex[:6]}"
ENV = "staging"   # environment: lowercase letters, digits, '-' or '_', must not start with "langfuse"
RELEASE = "v1"    # the code version that produced these traces
print("run tag:", RUN_TAG, "| environment:", ENV, "| version:", RELEASE)

# %% [markdown]
# ## Demo — the conversations you will trace (no LLM calls)
# Two multi-turn sessions (each should end in a handoff) and one single-turn question, from three customers.
# Ids are scoped by your run, so re-running the module never mixes sessions.

# %%
demo = traffic.demo_conversations(run_id=RUN_TAG.split(":")[1], env=ENV, release=RELEASE)
print(f"{'session_id':<38} {'user':<8} {'turns':>5}  tags")
for spec in demo:
    print(f"{spec.session_id:<38} {spec.user_id:<8} {len(spec.turns):>5}  {', '.join(spec.tags)}")
for spec in demo:
    for i, turn in enumerate(spec.turns, 1):
        print(f"  {spec.conversation_id} turn {i}: {turn}")

# %% [markdown]
# ## TODO 1 — a per-turn root that carries the context
#
# Recap from module 01: `langfuse = get_client()`; `@observe(name=..., as_type="agent", capture_input=False, capture_output=False)`
# for the root; run the agent with `config={"callbacks": [CallbackHandler()]}` so LangGraph nests under it;
# `langfuse.update_current_span(input=..., output=...)` for the root's input/output.
#
# Write `handle_turn(spec, message, history, agent) -> AgentRun`:
# - decorated as the `support-request` root (type **agent**, `capture_input=False`)
# - inside it, **one** `propagate_attributes(...)` block that sets, for the root and every span below it:
#   `user_id=spec.user_id`, `session_id=spec.session_id`, `tags=[RUN_TAG, *spec.tags]`,
#   `metadata={"intent": spec.intent, "conversation_id": spec.conversation_id, "pid": str(os.getpid())}`,
#   `environment=ENV`, `version=RELEASE`
#   (metadata values must be **strings** ≤ 200 chars)
# - runs `agent.invoke(message, history=history, config=...)`, sets input = message, output = the turn's final answer
# - returns the `AgentRun`
#
# And `run_conversation(spec, agent)`: loop over `spec.turns`, calling `handle_turn` once per turn and passing
# `history=run.messages` from the previous turn (start with `[]`). One trace per turn; the shared `session_id`
# is what turns them into a conversation.
#
# Hints: `from langfuse import get_client, observe, propagate_attributes`; `from langfuse.langchain import CallbackHandler`.
# Do **not** set `LANGFUSE_TRACING_ENVIRONMENT` — the environment comes from `propagate_attributes`, per request.
#
# Expected (after the run cell): the Sessions view shows 2 two-turn sessions.

# %%
# TODO 1: your code here


# %% [markdown]
# ## TODO 2 — the specialist worker (a separate process)
#
# The cell below writes `_specialist_worker.py`. `specialist.run_worker(script, payload)` runs it with this
# venv's Python, sends `payload` as JSON on stdin and returns the JSON the worker prints on its **last** stdout line.
# The child inherits `LANGFUSE_*` from this kernel's environment, and `ch09/` is on its `PYTHONPATH`.
#
# The plumbing is given; complete the Langfuse parts in `handle(payload)`:
# - create a client (`get_client()`)
# - **continue the parent's trace**: `langfuse.start_as_current_observation(trace_context={"trace_id": ...,
#   "parent_span_id": ...}, name=f"specialist-{target}", as_type="agent")`
# - **re-apply the context** inside it: `propagate_attributes(user_id=, session_id=, environment=, version=)` from the
#   payload — nothing from the parent's `propagate_attributes` reaches this process
# - call `resolve_handoff(target, reason, conversation)`, set the span's input/output and
#   `metadata={"pid": str(os.getpid())}` (this is how verify proves the span ran in another process)
# - **flush before the process exits**, or the span never reaches Langfuse
#
# `%%writefile` overwrites the file each time you run the cell — edit here, not in the file.

# %%
%%writefile _specialist_worker.py
"""Specialist worker: runs in its OWN process. JSON payload on stdin -> JSON result as the last stdout line."""
import json
import os
import sys

from langfuse_lab.specialist import resolve_handoff  # importable: run_worker puts ch09/ on PYTHONPATH

# payload keys (sent by your TODO 3): trace_id, parent_span_id, target, reason, conversation,
#                                     user_id, session_id, environment, version


def handle(payload: dict) -> dict:
    # TODO 2a: langfuse = get_client()
    # TODO 2b: continue the parent's trace with start_as_current_observation(trace_context=..., name=..., as_type=...)
    # TODO 2c: inside it, propagate_attributes(user_id=, session_id=, environment=, version=) from the payload
    # TODO 2d: resolve the handoff, then update the span: input, output, metadata={"pid": str(os.getpid())}
    return resolve_handoff(payload["target"], payload["reason"], payload["conversation"])  # replace with your version


if __name__ == "__main__":
    result = handle(json.loads(sys.stdin.read()))
    # TODO 2e: flush the Langfuse client before exiting
    print(json.dumps(result))  # must stay the LAST stdout line

# %% [markdown]
# ## TODO 3 — dispatch the handoff from the parent
#
# Redefine `handle_turn` (start from your TODO 1 code). After `agent.invoke(...)`, **while the root is still open**:
# when `run.summary["handoff_target"] != "none"`, call
# `specialist.run_worker(Path("_specialist_worker.py"), payload)` with a payload that has
# - `trace_id=langfuse.get_current_trace_id()` and `parent_span_id=langfuse.get_current_observation_id()`
#   (the current observation is your `support-request` root)
# - `target` = the handoff target; `reason` = the `create_handoff` call's reason
#   (`run.summary["tool_calls"]` → the entry named `create_handoff` → `["input"]["reason"]`)
# - `conversation` = the customer's messages so far (e.g. `[m.content for m in run.messages if m.type == "human"]`)
# - `user_id`, `session_id`, `environment`, `version` — the worker needs them because context does not cross processes
#
# Attach the ticket to the root: e.g. output `{"answer": ..., "ticket": ticket}` and `metadata={"ticket_id": ...}`
# via `update_current_span`.
#
# The worker's stderr is forwarded here prefixed `[worker stderr]` — SDK warnings there may show in red; they are
# harmless as long as the worker returns a ticket. A failing worker raises `RuntimeError` with its stderr tail.
#
# Expected: the `support-request` trace of each handoff turn contains a `specialist-*` span.

# %%
# TODO 3: your code here


# %% [markdown]
# ## Run it
# Three conversations, five turns (≈ 15 Azure calls). Watch for the printed tickets.
# The run cell calls `run_conversation` by this exact name.

# %%
agent = SupportAgent()
for spec in traffic.demo_conversations(run_id=RUN_TAG.split(":")[1], env=ENV, release=RELEASE):
    run_conversation(spec, agent)

from langfuse import get_client
get_client().flush()

# %% [markdown]
# `expected_traces=5` = one root trace per turn (2 + 2 + 1); verify waits until all 5 are queryable before judging.
# The specialist span is *inside* those traces, so it does not add a trace.

# %%
verify.check_module_02(RUN_TAG, expected_traces=5, environment=ENV, version=RELEASE)

# %% [markdown]
# ## UI tour
# - [ ] **Sessions** view: open the `…-s_refund_followup` session and replay the conversation turn by turn
# - [ ] **Users** view: open `cust_02` — every trace that customer produced, with counts and cost
# - [ ] Traces list: filter by **environment** `staging` (top bar), by **version** `v1`, and by tag `multi_turn`
# - [ ] Open a handoff trace: the metadata panel shows `intent`, `conversation_id`, `pid`, `ticket_id`
# - [ ] Find the `specialist-*` span inside the parent trace: its `pid` differs from the root's — another process
# - [ ] Toggle the **agent graph** view on the handoff trace
#
# ## Production notes
# - Set context **once, at the request boundary** (your root), with `propagate_attributes` — not on every span by hand.
# - **Tags = low-cardinality filters** (intent, feature, tier); **metadata = high-cardinality context** (ids, pids, ticket ids).
#   A tag per conversation id makes the tag filter useless.
# - **Never put secrets or PII in tags or metadata** — they are indexed and shown everywhere (masking is module 05).
# - Separate data with **environments** (`dev`/`staging`/`production`) in one project rather than separate projects,
#   so prompts, datasets and evaluators are shared while the data stays isolated.
# - Across HTTP services, prefer **W3C `traceparent` header propagation** (OpenTelemetry) — the same idea as the
#   trace id + parent span id you passed on stdin here; the receiving service re-applies user/session itself.
# - **Why it matters (leadership):** one link shows a customer's whole conversation across every service that touched it.
