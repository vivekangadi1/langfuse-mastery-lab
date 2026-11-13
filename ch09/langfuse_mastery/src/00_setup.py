# %% [markdown]
# # Module 00 — Setup & the Langfuse data model
#
# **Goal:** confirm the stack works and build the mental model every later module uses.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **Trace** | One end-to-end request | One customer message → reply |
# | **Observation** | A step inside a trace: `span`, `generation`, `tool`, `retriever`, `guardrail`, `agent`, `event`, … | LLM call, `lookup_order`, KB search, guardrail check |
# | **Session** | Traces grouped into a conversation | A multi-turn chat (module 02) |
# | **Score** | An evaluation attached to a trace/observation/session | "handoff correct = true" (module 06) |
#
# In SDK v5 a trace *is* its root observation: the trace's input/output come from the root span.
#
# Docs: https://langfuse.com/docs/observability/data-model

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` importable whatever the kernel's cwd

import uuid
from langfuse_lab import verify
from langfuse_lab.config import Settings

settings = Settings.from_env()
RUN_TAG = f"run:m00-{uuid.uuid4().hex[:6]}"
print("Langfuse:", settings.langfuse_base_url, "| agent deployment:", settings.agent_deployment, "| run tag:", RUN_TAG)

# %% [markdown]
# ## Which features does this self-hosted server expose?
# Langfuse OSS v4 includes the evaluation and review features we need. This probe proves it for *this* server.

# %%
verify.oss_feature_report()

# %% [markdown]
# ## TODO 1 — create your first trace by hand
#
# Write a trace **named** `hello-langfuse`, **tagged** with `RUN_TAG`, whose root observation has an
# **input** and an **output**. Then flush so it is sent before the verify cell runs.
#
# Hints (look these up rather than guessing):
# - `from langfuse import get_client, propagate_attributes`
# - `langfuse.start_as_current_observation(name=..., as_type="span")` is a context manager
# - inside it, `span.update(input=..., output=...)`
# - `propagate_attributes(trace_name=..., tags=[...])` sets trace-level attributes for everything inside it
# - `langfuse.flush()`
#
# Expected: the verify cell below prints ✅ for all three checks.

# %%
# TODO 1: your code here


# %%
verify.check_module_00(RUN_TAG)

# %% [markdown]
# ## UI tour
# Open http://localhost:3000 → **Tracing → Traces**, filter by tag = your `RUN_TAG`:
# - [ ] Open `hello-langfuse`; find the input/output you set on the root span
# - [ ] Note the trace ID in the URL — you'll use `langfuse.get_trace_url()` in module 01
# - [ ] Look at the left nav: Sessions, Users, Scores, Datasets, Prompts, Dashboards — each is a later module
#
# ## Production notes
# - The SDK sends data **asynchronously in batches**; short scripts must `flush()` (or `shutdown()`) before exit.
# - Ingestion is async on the server too: a trace may take a few seconds to be queryable — `verify` retries.
# - **Why it matters (leadership):** without a trace, an agent failure is a support ticket with no evidence. With one, it is a link anyone can open.
