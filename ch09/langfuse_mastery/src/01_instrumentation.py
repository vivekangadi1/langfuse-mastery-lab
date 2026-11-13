# %% [markdown]
# # Module 01 — Instrumentation: automatic + manual
#
# **Goal:** one customer request produces a correct, typed span tree:
#
# ```
# support-request (agent)                 ← your root (@observe), with input/output
# └── LangGraph (chain)                   ← automatic (CallbackHandler) from here down …
#     ├── guardrail (chain)               ← the graph node
#     │   └── guardrail (guardrail)       ← … except your manual @observe spans
#     ├── agent (agent)
#     │   └── AzureChatOpenAI (generation)
#     └── tools (chain)
#         ├── lookup_order (tool)
#         └── search_kb (tool)
#             └── kb-search (retriever)   ← manual @observe, nested under the tool that called it
# ```
#
# Production LangGraph apps use **both** layers: the `CallbackHandler` traces the graph for free,
# and `@observe` fills what it cannot know (your guardrail, your retriever, business-level root).
#
# Docs: https://langfuse.com/docs/observability/sdk/instrumentation ·
# https://langfuse.com/integrations/frameworks/langchain ·
# https://langfuse.com/docs/observability/features/observation-types

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` importable whatever the kernel's cwd

import uuid
from langfuse_lab import verify
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.failures import FailureConfig
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_lab import kb

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings), same as module 00
RUN_TAG = f"run:m01-{uuid.uuid4().hex[:6]}"
fixtures = load_fixtures()
print("run tag:", RUN_TAG)

# %% [markdown]
# ## Demo — the agent with zero instrumentation
# It works, but nothing reaches Langfuse. This is every production agent before observability.

# %%
plain = SupportAgent()
run = plain.invoke("I was charged twice for order 8129. Please fix it.")
print(run.summary["steps_completed"], "->", run.summary["handoff_target"])
print(run.summary["final_answer"])

# %% [markdown]
# ## TODO 1 — automatic tracing with the LangChain `CallbackHandler`
#
# Create a `langfuse.langchain.CallbackHandler` and run the same request with
# `plain.invoke(..., config={"callbacks": [handler]})`. Flush, then open the trace in the UI.
#
# Hints:
# - create the client: `from langfuse import get_client`; `langfuse = get_client()` — later cells use the name `langfuse`
# - `from langfuse.langchain import CallbackHandler`
#
# Write down (markdown cell or comment): which observation **types** did it create? What is **missing**
# (hint: is the guardrail typed as a guardrail? is the KB search a retriever? is there a meaningful root?).

# %%
# TODO 1: your code here


# %% [markdown]
# ## TODO 2 — manual observations for what the handler can't know
#
# Wrap the scaffold's guardrail and retriever with `@observe` using the right `as_type`:
# - `observed_guardrail(text)` → calls `check_input(text)`, type **guardrail**, span named `guardrail`
# - `observed_search(query, k)` → calls `kb.search(query, fixtures.kb, k=k)`, type **retriever**, span named `kb-search`
#
# Then build an agent that uses them: `SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)`.
#
# Expected: after the run cell below, `kb-search` and `guardrail` spans appear typed (retriever / guardrail) in the UI.

# %%
# TODO 2: your code here


# %% [markdown]
# ## TODO 3 — a business-level root span
#
# Write `handle_request(message: str, agent) -> str`:
# - decorated with `@observe(name="support-request", as_type="agent")`
# - all traces tagged with `RUN_TAG` (`propagate_attributes(tags=[...])`)
# - runs the agent **with** the `CallbackHandler` so LangGraph spans nest under your root
# - sets the root's **input** = the message and **output** = the final answer (hint: `langfuse.update_current_span`)
# - if any tool call errored (`run.summary["retry_count"] > 0`), set the root `level="WARNING"` with a `status_message`
# - prints `langfuse.get_trace_url()` so you can click straight into the trace
# - returns the final answer
#
# Production hint: `@observe` captures the function's arguments as the span input — here that includes the
# `agent` object. Pass `capture_input=False` to the decorator and set the input explicitly with
# `update_current_span(input=message, ...)` instead.
#
# Expected: the printed URL opens a `support-request` trace whose input is your message.

# %%
# TODO 3: your code here


# %% [markdown]
# ## Run it — including a planted failure
# Four requests: a billing request (order lookup + handoff), a KB question (exercises the retriever),
# a shipping request with **`slow_tool`** planted (every tool call sleeps 3 s), and a request for an order
# that does not exist (`0000` → the tool returns `order_not_found` → `retry_count > 0` → your root goes `WARNING`).
# Module 01's planted failure is caught if the slow tool span is visible with its latency.
#
# The run cell calls `observed_guardrail`, `observed_search` and `handle_request` by these exact names.

# %%
instrumented = SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)
slow = SupportAgent(failures=FailureConfig.from_names(["slow_tool"]),
                    guardrail_fn=observed_guardrail, search_fn=observed_search)

for message, agent in [
    ("I was charged twice for order 8129. Please fix it.", instrumented),
    ("What is your refund policy?", instrumented),
    ("Where is my order 9912 and when will it arrive?", slow),
    ("Where is my order 0000 and when will it arrive?", instrumented),
]:
    print(handle_request(message, agent))

from langfuse import get_client
get_client().flush()

# %% [markdown]
# Ingestion is async, so `expected_traces=4` makes verify wait until all 4 requests above are queryable before judging them.
# `expect_warning=True` checks that the unknown-order request's root was marked `WARNING` by your TODO 3.

# %%
verify.check_module_01(RUN_TAG, expect_slow_tool=True, expect_warning=True, expected_traces=4)

# %% [markdown]
# ## UI tour
# - [ ] Open the `support-request` trace: is the tree typed (agent → guardrail / generation / tool / retriever)?
# - [ ] Click the generation: model, tokens, latency are captured automatically
# - [ ] Find the slow trace: which span owns the 3 seconds? (this is how you debug latency in production)
# - [ ] Toggle the **agent graph** view on a trace
#
# ## Production notes
# - Pass the `CallbackHandler` per invocation (`config={"callbacks": [...]}`), not globally, so traces nest under your root.
# - Keep the root span business-meaningful (`support-request`) — dashboards and evaluators key off it later.
# - Use levels (`WARNING`/`ERROR`) + `status_message` so failures are filterable without reading payloads.
# - **Why it matters (leadership):** the slow-tool trace shows *which* dependency cost 3 seconds — minutes to diagnose instead of days.
