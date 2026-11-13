# %% [markdown]
# # Module 05: Data protection & volume (masking, sampling, attachments)
#
# **Goal:** trace production traffic **without a data-protection incident**, and without paying to store every request.
# After this module:
# - sensitive data (card numbers, emails, phone numbers) is masked **inside our process**, before anything is sent,
#   and you can prove that no card number reached Langfuse in **any** field;
# - a high-volume service sends a **sample** of its traces, whole traces only, and you can predict which ones;
# - a customer's uploaded photo and PDF receipt are attached to the trace and stored in the stack's MinIO bucket.
#
# | Concept | What it is | In our support agent |
# |---|---|---|
# | **Guardrail** (module 01) | app logic that **decides** on the raw input: block, flag, route | `check_input` flags `pii_card_number`. The request still runs, and without masking the card is in the trace |
# | **`mask=` callback** | `Langfuse(mask=fn)`: `fn(data=...)` runs on the input, output and metadata of **every** observation, in-process, **before export** | your `masking.mask_pii`: cards (Luhn), emails, phones → placeholders |
# | **Not masked** | values given to `propagate_attributes(...)`: metadata, tags, user / session ids. Also score comments and media bytes | never put free text there |
# | **Sampling** | `Langfuse(sample_rate=0.25)` or `LANGFUSE_SAMPLE_RATE`: keep a fraction of **traces** | a separate high-volume worker process |
# | **Attachments** | `LangfuseMedia(content_bytes=..., content_type=...)` inside input/output/metadata: uploaded to object storage, replaced by a reference | the damage photo + PDF receipt of a broken-item ticket |
#
# ```
#  customer text ──► guardrail (sees RAW text: flags pii_card_number)        ── decides, in-app
#       │
#       ▼  agent, tools, LLM calls ... every observation's input / output / metadata
#  ┌──────────── your process ─────────────────────────────────────────────────┐
#  │ LangfuseMedia → upload to MinIO, replaced by "@@@langfuseMedia:…@@@"      │
#  │ mask(data=…) → "My card number is [REDACTED_CARD]."                       │  ← the only copy that leaves
#  │ sampler: keep trace iff trace_id says so (whole trace, every span)        │
#  └──────────────────────────────────┬────────────────────────────────────────┘
#                                     ▼  OTLP export
#                               Langfuse (ClickHouse) · media in MinIO
# ```
#
# **Guardrail vs mask: two layers, both needed.** The guardrail is a **decision** about the request; it has to see the
# real text to do its job, and it can't stop the trace from recording what the customer typed. The mask is a **storage
# and transport** control: whatever the app does, the copy that leaves the process is redacted. In this module the
# guardrail still flags `pii_card_number` (it ran on the raw text), while the stored guardrail input shows
# `[REDACTED_CARD]`. Verify checks both.
#
# **The planted failure, `pii`:** customers paste card numbers into the chat (`traffic.PII_SUFFIX = " My card number
# is 4111 1111 1111 1111."`). Verify scans **every field of every observation** of those traces (LLM messages, tool
# inputs and outputs, metadata fully expanded) and fails on a single card number. It also requires the placeholder
# where the card was, so it knows your mask **ran**, rather than the data simply being absent.
#
# Docs: https://langfuse.com/docs/observability/features/masking ·
# https://langfuse.com/docs/observability/features/sampling ·
# https://langfuse.com/docs/observability/features/multi-modality ·
# https://langfuse.com/docs/observability/sdk/python/advanced-usage

# %%
import sys
from pathlib import Path
LAB_ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "langfuse_lab").is_dir())
sys.path.insert(0, str(LAB_ROOT))  # make `langfuse_lab` / `langfuse_mastery` importable whatever the kernel's cwd

import hashlib
import importlib
import json
import re
import subprocess
import time
import uuid
from langfuse_lab import verify, traffic, kb
from langfuse_lab.attachments import damage_photo_png, receipt_pdf
from langfuse_lab.config import Settings
from langfuse_lab.agent import SupportAgent
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input
from langfuse_lab.specialist import run_worker
from langfuse_mastery import masking

settings = Settings.from_env()  # loads the repo-root .env (Langfuse keys + Azure settings). No Langfuse client yet!
RUN_TAG = f"run:m05-{uuid.uuid4().hex[:6]}"
RUN_ID = RUN_TAG.split(":")[1]
SAMPLING_TAG = f"{RUN_TAG}:sampling"
ENV = "dev"
fixtures = load_fixtures()
CONVERSATIONS = traffic.mask_demo_conversations(run_id=RUN_ID, env=ENV)
UPLOAD = traffic.upload_demo_case(run_id=RUN_ID, env=ENV)
print("run tag:", RUN_TAG)
for spec in [*CONVERSATIONS, UPLOAD]:
    print(f"  {spec.intent:<20} {spec.user_id}  {','.join(spec.failures):<9} {spec.turns[0][:95]}")

# %% [markdown]
# ## Demo: the singleton trap (why the cell above created no client)
# The SDK keeps **one client per public key per process**. `Langfuse(...)` and `get_client()` both reuse it. So once
# *anything* in a process has created a client (a `get_client()` in an earlier cell, an imported helper, the
# `CallbackHandler`), a later `Langfuse(mask=...)` **silently returns a client without your mask**, with no warning
# (verified live). `sample_rate=` is ignored the same way.
#
# This given cell shows it in a **separate process**, so this kernel stays clean.

# %%
trap = """
from langfuse import Langfuse, get_client
get_client()                                   # e.g. a helper module called it at import time
client = Langfuse(mask=lambda *, data, **kw: "MASKED")
print("mask in effect:", client._mask is not None)
"""
print(subprocess.run([sys.executable, "-c", trap], capture_output=True, text=True, cwd=LAB_ROOT).stdout)

# %% [markdown]
# So in this notebook, **TODO 2 creates the first and only client**, and every later cell (and the `CallbackHandler`,
# and `@observe`) gets that one through `get_client()`. If you ever see the guard in TODO 2 fire: restart the kernel
# and run the cells in order. Other module notebooks run in their own kernels, so this masked client never leaks into
# them, and theirs never into this one.
#
# ## TODO 1: implement `langfuse_mastery/masking.py` (pure functions + pytest)
#
# Open `ch09/langfuse_mastery/masking.py` and implement `luhn_valid`, `redact_text` and `mask_pii`. The module
# docstring lists every shape the `mask` callback really receives. Three of them catch people out:
# - LLM messages as **lists of dicts**, and LangGraph state as **dicts of lists of dicts**: recursion.
# - **Tuples** (`@observe` captures arguments as `{"args": (...), "kwargs": {...}}`).
# - **Raw Python objects**: `@observe` captures your function's return value. Our `support-request` returns
#   `(AgentRun, trace_id)`, where `AgentRun` is a dataclass holding LangChain `HumanMessage` **objects**. A masker that
#   only walks dicts and lists passes those through, and the card reaches Langfuse in the root's output.
#
# Two rules: **fail closed** (unknown object → `str()` → redact) and **don't over-mask** (order `8129`, `cust_02`, uuids,
# dates and token counts must survive, or the traces become useless). The guardrail's `luhn_ok` in
# `langfuse_lab/guardrail.py` is a reference idea for the checksum; write your own.
#
# The cell below runs only this module's tests and reloads your module. Re-run it until it shows `48 passed`.

# %%
result = subprocess.run([sys.executable, "-m", "pytest", "langfuse_mastery/tests/test_masking.py", "-q",
                         "-p", "no:cacheprovider"], cwd=LAB_ROOT, capture_output=True, text=True)
print(result.stdout[-2500:] or result.stderr[-2500:])
masking = importlib.reload(masking)

# %% [markdown]
# ## TODO 2: wire the mask into the client (correctly)
#
# Write two things:
#
# **`pii_mask(*, data, **kwargs)`**, the callback. The SDK calls it as `mask(data=...)`, keyword-only. Return
# `masking.mask_pii(data)`. Look `mask_pii` up **through the module at call time** (`masking.mask_pii`, not a
# `from masking import mask_pii` captured once). Then a later `importlib.reload(masking)` takes effect in the live
# client without a kernel restart.
#
# **`build_masked_client() -> Langfuse`**: construct `Langfuse(mask=pii_mask)` and **prove the mask took effect**.
# - If a client already existed in this process, the SDK ignored your `mask`: the returned client's `_mask` is `None`
#   (a private attribute, but the only place this is visible). Raise a `RuntimeError` telling the reader to restart the
#   kernel and run the cells in order.
# - Return the client.
#
# Notes:
# - If `pii_mask` raises (for example, `mask_pii` is still a stub), the SDK logs an error and stores
#   `"<fully masked due to failed mask function>"` instead. It fails **closed**, which is the right default.
# - The keys and base URL come from the environment that `Settings.from_env()` loaded.
#
# The cell after yours checks that the client masks a card and that `get_client()` (what the `CallbackHandler` and
# `@observe` use) returns the **same** masked configuration.

# %%
from langfuse import Langfuse, LangfuseMedia, get_client, observe, propagate_attributes
from langfuse.langchain import CallbackHandler

# TODO 2: your code here

# %%
langfuse = build_masked_client()
sample = "order 8129, card 4111 1111 1111 1111, mail farah@example.com, call +1 415-555-0132"
print("masked:", langfuse._mask(data=sample))
assert langfuse._mask(data=sample) == masking.mask_pii(sample) != sample
assert get_client()._mask is langfuse._mask, "get_client() must hand out the same masked configuration"

# %% [markdown]
# ### Demo: what the mask does NOT cover
# This given cell sends one span. Its **input** goes through your mask. A `ticket_subject` placed in
# `propagate_attributes(metadata=...)` does **not** (verified live). Propagated attributes are copied onto every
# observation as-is, and they bypass `mask`. So never put customer free text in metadata you propagate, tags, user ids
# or session ids. (The trace is tagged `…:smoke`, outside the run that verify scans.)

# %%
with langfuse.start_as_current_observation(name="mask-smoke", as_type="span") as span:
    with propagate_attributes(tags=[f"{RUN_TAG}:smoke"], metadata={"ticket_subject": "Callback +1 415-555-0132"}):
        span.update(input={"message": sample}, output="ok")
        smoke_trace = langfuse.get_current_trace_id()
langfuse.flush()
for _ in range(20):
    rows = langfuse.api.observations.get_many(trace_id=smoke_trace, fields="core,io,metadata", limit=5).data
    if rows:
        break
    time.sleep(2)
print("input (masked):         ", rows[0].input)
print("propagated (NOT masked):", rows[0].metadata.get("ticket_subject"))

# %% [markdown]
# ## Given: the instrumentation you built in modules 01–02
# This is a recap, not a TODO, and it contains no masking code at all. Every observation below goes through the client
# from TODO 2, because `@observe` and `CallbackHandler()` call `get_client()`.
#
# Note that `traced_turn` **returns** `(run, trace_id)`. `@observe` records that return value as the root's output
# (the `AgentRun` with all the LangChain messages), so your mask sees raw objects there.

# %%
@observe(name="guardrail", as_type="guardrail")
def observed_guardrail(text):
    return check_input(text)


@observe(name="kb-search", as_type="retriever")
def observed_search(query, k):
    return kb.search(query, fixtures.kb, k=k)


AGENT = SupportAgent(guardrail_fn=observed_guardrail, search_fn=observed_search)


@observe(name="support-request", as_type="agent", capture_input=False, capture_output=False)
def traced_turn(message, spec, history=None):
    """One turn = one trace. Returns (AgentRun, trace_id)."""
    with propagate_attributes(session_id=spec.session_id, user_id=spec.user_id, tags=[RUN_TAG, *spec.tags],
                              environment=ENV):
        run = AGENT.invoke(message, history=history or [], config={"callbacks": [CallbackHandler()]})
        langfuse.update_current_span(input=message, output=run.summary["final_answer"])
        return run, langfuse.get_current_trace_id()


def run_conversation(spec) -> list[str]:
    """All turns of one conversation (one trace per turn, shared session). Returns the trace ids."""
    history, trace_ids = [], []
    for message in spec.turns:
        run, trace_id = traced_turn(message, spec, history=history)
        history = run.messages
        trace_ids.append(trace_id)
    return trace_ids


def wait_for_ingestion(trace_ids, *, timeout=120, settle=4):
    """Ingestion is async. Wait until every trace's root has arrived and no trace gains observations for `settle` s."""
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        seen = {}
        for t in trace_ids:
            rows = langfuse.api.observations.get_many(trace_id=t, fields="core", limit=200).data
            seen[t] = (any(o.parent_observation_id is None for o in rows), len(rows))
        if all(has_root for has_root, _ in seen.values()) and seen == last:
            print(f"ingested: {len(trace_ids)} traces, {sum(n for _, n in seen.values())} observations")
            return
        last = seen
        time.sleep(settle)
    print("warning: ingestion still settling (re-run this cell before verify)")

# %% [markdown]
# ## TODO 3: attach the customer's photo and receipt (`LangfuseMedia`)
#
# The broken-item customer (`UPLOAD`) sends a photo of the parcel and the PDF receipt along with their message. Write
# two functions.
#
# **`upload_turn(spec, photo: bytes, receipt: bytes) -> (run, trace_id)`**: the same shape as `traced_turn` (decorated
# the same way, with the same `propagate_attributes`), with one difference: the root's **input** is a dict:
# `{"message": <the text>, "photo": LangfuseMedia(...), "receipt": LangfuseMedia(...)}`.
# - `LangfuseMedia(content_bytes=photo, content_type="image/png")` and likewise `"application/pdf"` for the receipt.
# - The SDK starts uploading the bytes to object storage (MinIO on this stack) in a background thread, then passes
#   the input **with the `LangfuseMedia` objects still in it** to your mask. Each object becomes a reference string
#   `@@@langfuseMedia:type=image/png|id=…|source=bytes@@@` only later, when the span is serialized. So a fail-closed
#   masker that `str()`s unknown objects would store `"<langfuse.media.LangfuseMedia object at 0x…>"` and the trace
#   would lose its attachment (verified live: it happened while building this module). That's why `mask_pii` returns
#   `LangfuseMedia` objects untouched (the tests check it).
# - The agent itself only gets the text: `AGENT.invoke(spec.turns[0], config=...)`.
#
# **`fetch_attachments(trace_id) -> dict[str, bytes]`**: read the files back through the API: `{content_type: bytes}`.
# - Find the root observation of the trace (`parent_observation_id is None`) and its `input` (a JSON **string**).
# - Find the reference strings with a regex (`@@@langfuseMedia:.+?@@@`), then
#   `LangfuseMedia.parse_reference_string(ref)["media_id"]`.
# - `langfuse.api.media.get(media_id)` returns `.content_type`, `.content_length` and `.url`, a presigned MinIO URL
#   valid for 1 hour. Download it with `httpx.get(url).content`.
# - There is no "media of a trace" endpoint. The reference strings in the observations are the index.
#
# The run cell below calls `upload_turn`, then `fetch_attachments`, and checks that the bytes come back identical.

# %%
# TODO 3: your code here

# %% [markdown]
# ## Run it: PII traffic + the upload
# 3 conversations (4 turns, 2 of them carrying a pasted card number) and the upload turn: 5 traces, about 15 Azure
# calls. Then the cell reads back what Langfuse **stored**.

# %%
PHOTO, RECEIPT = damage_photo_png(), receipt_pdf("8115")
ATTACHMENT_SHA = {"image/png": hashlib.sha256(PHOTO).hexdigest(), "application/pdf": hashlib.sha256(RECEIPT).hexdigest()}
TRACES = {}
for spec in CONVERSATIONS:
    TRACES[spec.conversation_id] = run_conversation(spec)
    print(f"{spec.intent:<20} {','.join(spec.failures):<9} {len(TRACES[spec.conversation_id])} trace(s)")
run, UPLOAD_TRACE = upload_turn(UPLOAD, PHOTO, RECEIPT)
TRACES[UPLOAD.conversation_id] = [UPLOAD_TRACE]
print(f"{UPLOAD.intent:<20} {'':<9} 1 trace(s), handoff={run.summary['handoff_target']}")
langfuse.flush()
wait_for_ingestion([t for ids in TRACES.values() for t in ids])

files = fetch_attachments(UPLOAD_TRACE)
print({ct: f"{len(b)} bytes" for ct, b in files.items()})
assert files.get("image/png") == PHOTO and files.get("application/pdf") == RECEIPT, "attachments differ"

# %% [markdown]
# ### What Langfuse stored
# The root input and the user message the **LLM generation** recorded, per trace, plus the guardrail's flags. The
# guardrail flagged the card because it saw the raw text in-process. The stored copies show only the placeholder.

# %%
for conversation_id, trace_ids in TRACES.items():
    for t in trace_ids:
        rows = langfuse.api.observations.get_many(trace_id=t, fields="core,basic,io", limit=200).data
        root = next(o for o in rows if o.parent_observation_id is None)
        gen = next((o for o in rows if o.type == "GENERATION"), None)
        guard = next((o for o in rows if o.type == "GUARDRAIL"), None)
        user_msgs = [m["content"] for m in json.loads(gen.input) if m.get("role") == "user"] if gen else []
        print(f"{t}\n  root input : {str(root.input)[:150]}\n  LLM saw    : {user_msgs[-1][:150] if user_msgs else '-'}"
              f"\n  guardrail  : {guard.output if guard else '-'}")

# %% [markdown]
# ## TODO 4: sampling, for the high-volume path
#
# At scale you don't need every trace of a healthy, high-volume endpoint. Langfuse's SDK samples **per trace**: it
# installs OpenTelemetry's `TraceIdRatioBased(rate)` sampler, which decides from the **trace id** alone. So every span
# of a trace gets the same decision, and a trace is kept or dropped whole.
#
# **4a. In `langfuse_mastery/_sampling_worker.py`, implement `make_client(rate)`.** The worker is a separate
# process (its docstring explains why: the sampler is fixed by the **first** client of a process, and this kernel
# already has one). The rest of the worker is given. It sends `n` cheap traces (no LLM call, 3 observations each)
# with **seeded** trace ids, and prints them all, kept or not.
#
# **4b. Here, write `predict_kept(trace_ids, rate) -> set[str]`**: the ids the sampler keeps. The rule, as implemented
# by OpenTelemetry's `TraceIdRatioBased` (read its source: `opentelemetry/sdk/trace/sampling.py`):
# - `bound = round(rate * 2**64)`
# - keep iff `int(trace_id, 16) & (2**64 - 1) < bound`, the **lower 64 bits** of the 128-bit trace id.
#
# Deterministic sampling is what lets you reason about it. You can predict the kept set before sending. And with
# `trace_context` from another service (module 02), every process makes the **same** decision for the same trace.
#
# The cell after yours runs the worker with 40 traces at rate 0.25 and compares your prediction with what arrived.

# %%
# TODO 4b: your code here

# %%
SAMPLE_RATE, N_SAMPLED = 0.25, 40
out = run_worker(LAB_ROOT / "langfuse_mastery" / "_sampling_worker.py",
                 {"tag": SAMPLING_TAG, "run_id": RUN_ID, "n": N_SAMPLED, "rate": SAMPLE_RATE}, timeout=180)
SAMPLED_IDS = out["trace_ids"]
PREDICTED = predict_kept(SAMPLED_IDS, SAMPLE_RATE)
print(f"worker sent {len(SAMPLED_IDS)} traces at rate {SAMPLE_RATE}; you predict {len(PREDICTED)} kept "
      f"(expected about {N_SAMPLED * SAMPLE_RATE:.0f})")
flt = [{"type": "arrayOptions", "column": "traceTags", "operator": "any of", "value": [SAMPLING_TAG]}]
for _ in range(30):
    arrived = {o.trace_id for o in langfuse.api.observations.get_many(fields="core", filter=json.dumps(flt),
                                                                       limit=1000).data}
    if arrived >= PREDICTED and arrived:
        break
    time.sleep(2)
print(f"arrived: {len(arrived)}   arrived == predicted: {arrived == PREDICTED}")

# %% [markdown]
# Verify checks:
# - **planted `pii` caught:** no card number in **any** field of any observation of the pii traces. It scans inputs,
#   outputs, metadata (fully expanded: the API truncates metadata values at 200 characters unless you ask), LLM
#   messages and tool inputs.
# - **the mask ran:** `My card number is [REDACTED_CARD]` in the root input and in the LLM messages of every pii trace.
# - **two layers:** the guardrail flagged `pii_card_number` (raw text in-process), while its stored input is masked.
# - emails and phone numbers are masked everywhere, including the `reset_password` tool **output**.
# - **no over-masking:** order and customer ids survive in the root inputs and the `lookup_order` tool inputs.
# - the attachments are stored, resolve through the media API, and download byte-identical.
# - **sampling:** exactly the trace-id prediction arrived, every kept trace complete, the count within tolerance of
#   `n × rate`, and your `predict_kept` agrees.

# %%
verify.check_module_05(RUN_TAG, sampling_tag=SAMPLING_TAG, sampled_ids=SAMPLED_IDS, predicted_ids=PREDICTED,
                       sample_rate=SAMPLE_RATE, attachment_sha256=ATTACHMENT_SHA, expected_traces=5)

# %% [markdown]
# ## UI tour
# - [ ] **Traces**: filter on tag `pii`. Open one and click through the generation, the guardrail, the tools and the
#   root: `[REDACTED_CARD]` everywhere the card was. The guardrail's output still says `pii_card_number`.
# - [ ] Open the `password_reset` trace: the `reset_password` tool **output** shows `[REDACTED_EMAIL]`. Tool results
#   are customer data too.
# - [ ] Open the `damaged_item_photo` trace: the root input renders the **photo inline** and the PDF as an attachment.
#   The stored input holds only the `@@@langfuseMedia…@@@` references; the files live in MinIO (`langfuse` bucket,
#   `media/` prefix; console at http://localhost:9091).
# - [ ] Open the `…:smoke` trace: the propagated `ticket_subject` shows the phone number **unmasked** on every
#   observation. That's the lesson of the demo, in the UI.
# - [ ] Filter on tag `…:sampling`: about a quarter of the 40 traces, each one complete (root + guardrail + kb-search).
#   There are no orphan spans from the dropped ones.
#
# ## Production notes
# - **Mask in the process, not later.** Anything sent unmasked is in your trace store, its backups, exports (module
#   11) and every downstream copy. Masking is a client-side feature: the server stores what it receives.
# - **One client, built first, at startup.** Construct `Langfuse(mask=..., sample_rate=...)` before anything calls
#   `get_client()` (including imported libraries), then use `get_client()` everywhere. Add a startup assertion that the
#   mask is in effect. A silently ignored mask is the classic incident.
# - **Fail closed.** If the mask raises, the SDK stores a fully-masked placeholder. Keep it that way: never wrap your
#   masker in a "return data unchanged on error".
# - **Know what the mask doesn't see:** `propagate_attributes` values (metadata, tags, user / session ids), score
#   comments (module 06), media bytes (a screenshot of a card is not text: your masker must even pass
#   `LangfuseMedia` through untouched), and spans from other OpenTelemetry
#   instrumentations. For the last, the SDK has an export-stage hook, `mask_otel_spans`. Use pseudonymous user ids, not
#   emails.
# - **Regexes are a floor, not a ceiling.** Luhn-checked cards, emails and phones cover the common leaks. Names,
#   addresses and free-text secrets need an NER/PII service (Presidio, a cloud DLP). Measure over-masking too, or the
#   traces stop being useful for debugging.
# - **Sample by trace, deterministically.** `sample_rate` keeps whole traces. Because the decision is a function of the
#   trace id, a distributed trace (module 02) is kept or dropped consistently in every service. Sample high-volume,
#   healthy paths. Keep 100% of the things you must audit, in a separate process or service with its own rate.
#   The SDK applies the same decision to scores: a score on a sampled-out trace is not sent (session scores are).
# - **Attachments cost storage, not tokens:** media goes to S3/MinIO, deduplicated by content hash (the media id is
#   derived from the bytes). Set bucket lifecycle rules to match your trace retention (module 13).
# - **Why it matters (leadership):** full observability of the agent **without** a data-protection incident. Sensitive
#   data never leaves our process, and we can prove it with a check that runs on every release.
