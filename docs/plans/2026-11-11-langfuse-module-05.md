# Langfuse Mastery: Module 05 (Data protection & volume) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 02, 03, 04 and 06 plans.

**Goal:** Module 05 answers "can we trace production traffic without a data-protection incident, and without paying to
store every request?". It teaches:
- **Masking before data leaves the process:** the client `mask=` callback. The learner writes a recursive PII masker
  (cards with Luhn, emails, phone numbers) that must not over-mask ids, and wires it into the client correctly despite
  the `get_client()` singleton.
- **Where masking does NOT reach:** `propagate_attributes(...)` values (metadata, tags, user/session ids), score
  comments and media bytes bypass `mask`. Free text never goes there.
- **The guardrail is a different layer:** the guardrail *decides* in-app on the raw text (it still flags
  `pii_card_number`); the mask controls what is *exported and stored*. Both run on the same request.
- **Sampling:** `sample_rate` / `LANGFUSE_SAMPLE_RATE`, deterministic by trace id, whole traces kept or dropped.
- **Attachments:** `LangfuseMedia` (a customer's damage photo + PDF receipt) uploaded to the stack's MinIO bucket and
  read back through the media API.
- **Planted failure `pii`:** customers paste card numbers (`traffic.PII_SUFFIX`). Verify scans every field of every
  observation of those traces (metadata fully expanded) and proves no card number reached Langfuse, and that the
  placeholder is there (masking ran, the data isn't merely absent).

Module 05 builds on modules 01 and 02 (instrumentation, tags). It is independent of modules 03, 04 and 06.

**Conventions:** the same as the scaffold, module 02, 03, 04 and 06 plans:
- Work from `ch09/` and use `$PY = .venv-langfuse/Scripts/python.exe`.
- Lab tests run with `$PY -m pytest langfuse_lab/tests -q`.
- `git add` files explicitly.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Branch: `langfuse-mastery`.
- `langfuse_lab/` contains no Langfuse code except `verify.py`, which only reads data. The learner writes all the Langfuse code.

**Verified live (planner probe, 2026-11-11, server 4.77 / SDK 5.2):**
- **`mask` signature:** `Langfuse(mask=fn)`, called as `fn(data=...)` (keyword only). It is applied to `input`,
  `output` and `metadata` of every observation the SDK writes: `@observe` captures, `start_as_current_observation`,
  `update()` / `update_current_span()`, and **every observation of the LangChain `CallbackHandler`** (generations,
  chains, tools). If `fn` raises, the SDK logs an error and stores `"<fully masked due to failed mask function>"`
  (for dict metadata: that value per key). Fail-closed.
- **What the mask receives (recorded live from one agent turn):**
  - CallbackHandler chain inputs/outputs: `{"messages": [ {content, additional_kwargs, response_metadata, type, name, id}, ...], ...}`
    (LangChain messages already turned into dicts).
  - GENERATION input: a list of `{"role", "content"}` dicts (system + user + tool messages); GENERATION output:
    `{"role": "assistant", "content", "tool_calls": [{name, args, id, type}], ...}`.
  - TOOL input: the args dict (`{"order_id": "8129"}`); TOOL output: the ToolMessage as a dict (`content` is a JSON string).
  - CallbackHandler metadata: dicts with tuples (`langgraph_triggers`, `langgraph_path`).
  - `@observe` input: `{"args": (...), "kwargs": {...}}` (a **tuple** inside).
  - `@observe` output: **raw Python objects**: `GuardrailResult` (a dataclass) for the guardrail, and for a
    `support-request` that returns `(AgentRun, trace_id)`: a tuple holding a dataclass whose `messages` are
    `HumanMessage`/`AIMessage` **objects** carrying the customer's text. A masker that only walks dict/list/str
    leaks the card through the root output (seen live: root output had the raw card while root input was masked).
  - Plain strings (`update_current_span(input=str)`), ints, and the router's `"agent"` / `"tools"` / `"__end__"`.
- **Not masked (verified):** `propagate_attributes(metadata={...})` values are copied onto every observation's metadata
  **unmasked** (a card placed there reached all 18 observations). Tags, user/session ids are not masked either.
- **The singleton trap:** `LangfuseResourceManager` is one instance per public key. After **any** `get_client()` /
  `Langfuse()` in the process, `Langfuse(mask=fn)` silently returns a client whose `_mask is None` (and
  `sample_rate=` is ignored the same way: a `Langfuse(sample_rate=0.0)` built after `get_client()` still exported its
  trace). No warning. `client.shutdown()` does not remove the instance. Fix: build the masked client **first** in the
  process (in a notebook: restart the kernel). `get_client()` afterwards copies the instance's mask.
- **Metadata is truncated to 200 chars on read** (`observations.get_many(fields=...metadata)`) unless
  `expand_metadata="key1,key2"` names the keys: a card at char 300 of a metadata value was invisible without it.
  Nested metadata dicts are flattened to dotted keys (`nested.a`). So a leak scanner must do two passes: list keys,
  then refetch expanded.
- **Sampling:** `sample_rate` (or env `LANGFUSE_SAMPLE_RATE`) installs OpenTelemetry's `TraceIdRatioBased(rate)` on
  the TracerProvider the **first** client creates. Keep iff `int(trace_id, 16) & (2**64 - 1) < round(rate * 2**64)`.
  Every span of a trace shares the decision: 40 traces at 0.25 → exactly the 11 predicted arrived, each with all 3
  observations; the 29 others left nothing. `Langfuse.create_trace_id(seed=...)` + `trace_context={"trace_id": id}`
  makes the ids (and so the kept set) deterministic. `get_current_trace_id()` returns the id even for a dropped trace.
- **Media:** `LangfuseMedia(content_bytes=, content_type="image/png"|"application/pdf")` anywhere in input/output/
  metadata is uploaded by a background thread to MinIO (`LANGFUSE_S3_MEDIA_UPLOAD_*`, presigned at
  `http://localhost:9090`) and stored as `@@@langfuseMedia:type=image/png|id=<22 chars>|source=bytes@@@`.
  **Correction found in validation:** the mask receives the `LangfuseMedia` **object** (the upload is queued first,
  the reference string is only produced at serialization, after the mask). A fail-closed masker that `str()`s unknown
  objects stored `"<langfuse.media.LangfuseMedia object at 0x…>"`: the file was uploaded but the trace lost it. So
  `mask_pii` must return `LangfuseMedia` objects unchanged (test + verify hint added). `LangfuseMedia.parse_reference_string(ref)["media_id"]` → `api.media.get(id)` →
  `mediaId, contentType, contentLength, uploadedAt, url (presigned, 1h), urlExpiry`; `httpx.get(url)` returns the
  exact bytes (sha256 matched). The media id is derived from the content hash (same bytes → same id). There is no
  "media of a trace" endpoint: find the reference strings in the observations.
- Probe traces were tagged `zz_probe_m05_*` / named `zz-probe-*` (traces only; no project-global objects created).

---

### Task 1: lab: module 05 demo traffic + attachment bytes

**Files:** modify `langfuse_lab/traffic.py`, create `langfuse_lab/attachments.py`; tests in `tests/test_traffic.py`,
`tests/test_attachments.py`.

- `traffic.mask_demo_conversations(*, run_id, env="dev") -> list[ConversationSpec]`, exactly:
  1. `double_charge` cust_00, "I was charged twice for order 8129. Please fix it." + `PII_SUFFIX`, `failures=("pii",)`
  2. `s_refund_followup` cust_00 (2 turns, turn 1 + `PII_SUFFIX`), `failures=("pii",)`
  3. `password_reset` cust_02 "I can't log in to my account cust_02. Please reset my password." (the `reset_password`
     tool **output** carries the customer's email)
- `traffic.upload_demo_case(*, run_id, env="dev") -> ConversationSpec`: `damaged_item_photo` cust_05, one turn:
  "My item from order 8115 arrived broken, photo and receipt attached. Call me back at +1 415-555-0132 or email
  farah@example.com." (phone + email in free text, order id that must survive).
- `attachments.py` (pure python, no PIL): `damage_photo_png() -> bytes` (a valid small PNG) and
  `receipt_pdf(order_id="8115") -> bytes` (a valid one-page PDF). Deterministic bytes.
- Commit: "lab: module 05 demo traffic (pii, email, phone, upload) + attachment bytes".

### Task 2: verify: module 05 checks

**Files:** `verify.py`, tests in `tests/test_verify_m05.py` (fakes shaped like `ObservationV2`: `.dict()` with
camelCase keys, `input`/`output` JSON strings, metadata dicts truncated to 200 chars unless `expand_metadata`).

- Helpers: `ALL_FIELDS = "core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context"`;
  `full_trace_observations(client, trace_id)` (two passes: keys, then `expand_metadata=`); `card_numbers(text)`
  (Luhn, 13–19 digits, single space/dash separators); `observation_texts(o) -> {field: text}`.
- `module_05_checks(run_tag, client, *, sampling_tag, sampled_ids, predicted_ids, sample_rate, attachment_sha256,
  expected_traces=5, placeholders=...)`:
  1. **planted pii caught: no card number in any field** of the `pii`-tagged traces (all fields, metadata expanded).
  2. **mask ran: placeholder where the card was**: every "My card number is" is followed by the card placeholder, in
     the root input and in at least one GENERATION input per pii trace.
  3. **guardrail still saw the raw card**: each pii trace's GUARDRAIL output flags `pii_card_number` (masking happens at
     export, not by changing what the app sees).
  4. **emails and phones masked** across the whole run (no email address, no `415-555-0132`), with the email and phone
     placeholders in the upload turn's root input.
  5. **ids not over-masked**: `8129` / `8115` / `cust_02` survive in the root inputs, and `lookup_order` TOOL inputs keep
     their `order_id`.
  6. **attachment stored and retrievable**: the `damaged_item_photo` trace references media; each reference resolves via
     `api.media.get`, downloads, and the bytes' sha256 equals the notebook's (`{content_type: sha256}`).
  7. **sampling: whole traces kept or dropped by trace id**: the traces tagged `sampling_tag` are exactly the
     `TraceIdRatioBased(sample_rate)` prediction for `sampled_ids`, each complete (same observation count), the count
     within 3σ of `n·rate`, `0 < rate < 1`, and the learner's `predicted_ids` equal the truth.
- Missing traces → `NotYet`. `check_module_05(...)` with `retries=12`.
- Commit: "lab: module 05 checks (pii scan with expanded metadata, placeholders, over-masking, media, sampling)".

### Task 3: learner pure functions: `langfuse_mastery/masking.py` + tests; sampling worker stub

- `luhn_valid(digits) -> bool`; `redact_text(text) -> str` (cards → `[REDACTED_CARD]` only when Luhn-valid,
  emails → `[REDACTED_EMAIL]`, phones → `[REDACTED_PHONE]`); `mask_pii(data)`: recursive over dict (keys kept),
  list, tuple, `LangfuseMedia` returned as is, pydantic objects (`model_dump()`, so LangChain messages), dataclasses,
  other objects via `str()`; ints,
  floats, bools, None unchanged; the input is never mutated; media reference strings survive.
- ~30 parametrized tests, including the live shapes above. Stubs raise `NotImplementedError`.
- `langfuse_mastery/_sampling_worker.py`: given trace emission (root + guardrail + kb-search, no LLM, seeded ids),
  TODO `make_client(rate)` raising `NotImplementedError`.
- Commit: "mastery: module 05 masking stubs + tests, sampling worker stub (learner implements)".

### Task 4: Module 05 notebook + live validation

`src/05_data_protection.py` → `05_data_protection.ipynb`. Setup like module 04 (`RUN_TAG = f"run:m05-…"`), but the
bootstrap cell must **not** create a Langfuse client.

1. Concept table + diagram (guardrail vs mask vs not-masked paths vs sampling vs media).
2. Given demo (subprocess, so this kernel stays clean): the singleton trap, `_mask is None`.
3. TODO 1: `masking.py` + its own pytest file.
4. TODO 2: `build_masked_client()`: a reload-safe callback + `Langfuse(mask=...)` + guards that it took effect.
   Given smoke: one span with card/email/phone in input + propagated metadata; read back: input masked, propagated
   metadata NOT masked (the lesson).
5. Given recap: instrumentation of modules 01–02 (`traced_turn`, `run_conversation`).
6. TODO 3: `upload_turn(spec, photo, receipt)` with `LangfuseMedia`, and `fetch_attachments(trace_id)`.
7. Run: 3 conversations + the upload turn (5 traces, ~15 Azure calls); print what Langfuse stored.
8. TODO 4: sampling: `make_client(rate)` in `_sampling_worker.py` and `predict_kept(trace_ids, rate)`; given runner
   launches the worker with N=40, rate 0.25.
9. Verify, UI tour (trace with placeholders, media preview, Settings), production notes + leadership line.

**Live validation:** `_validate_05.ipynb` + reference `masking.py` and `_sampling_worker.py`, `MODULE PASSED`. Then
restore the stubs and delete the validation notebook. Module 05 creates no project-global objects (only traces and
media), so there is nothing else to clean up.

---

### As implemented (2026-11-11)

- **Validation:** `run:m05-aa6846` passed (`MODULE PASSED`): 3 pii traces, 1806 fields scanned with metadata expanded,
  no card number; placeholder in 2 root inputs and in the LLM messages of all 3 pii traces (incl. the follow-up turn,
  whose history carries the card); the guardrail flagged `pii_card_number` on both first turns while its stored input
  showed `[REDACTED_CARD]`; no email/phone in 5 traces (the `reset_password` tool output's email was masked); ids
  intact; PNG + PDF downloaded byte-identical from MinIO; sampling kept 7/40 at 0.25, exactly the trace-id prediction,
  every kept trace complete.
- **First validation attempt failed** on the attachment: the fail-closed reference masker `str()`-ed the
  `LangfuseMedia` objects (see the correction under "Verified live"). Fixed in the spec, a new test, and a verify hint.
- **Live negative evidence for the leak scanner:** on the unmasked/naively-masked probe trace, `card_numbers` found the
  card in 19 fields: propagated metadata on every observation, and the root output (`@observe` captured the
  `(AgentRun, trace_id)` return value with raw LangChain messages) while the root input was masked.
- **Scanner false positive found and fixed against real data:** a LangGraph uuid (`…-edd5-1143-459802812a86`) contains a
  Luhn-valid 13-digit run across dashes. Digit runs glued to letters are now ignored (verify and the learner spec).
- The SDK also drops **scores** on sampled-out traces (`add_score_task`: same TraceIdRatioBased decision; session and
  dataset-run scores are kept). From the SDK source, not exercised live.
- **Cleanup:** module 05 creates no project-global objects (only traces and media objects), so nothing was deleted.
  The probe traces are tagged `zz_probe_m05_*` / named `zz-probe-*`; the validation traces are `run:m05-aa6846*`.
