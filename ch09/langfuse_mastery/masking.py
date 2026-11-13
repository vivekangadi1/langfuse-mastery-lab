"""Module 05: a PII masker for the Langfuse `mask=` callback. You implement these; `pytest
langfuse_mastery/tests/test_masking.py` checks them.

Pure functions; the only Langfuse import is the LangfuseMedia type. The client calls your mask as `mask(data=...)` on
the input, output and metadata of EVERY observation, just before it is exported. What `data` can be (all recorded live from one agent turn):
- a plain string:                      "I was charged twice for order 8129. My card number is 4111 1111 1111 1111."
- LLM messages (a GENERATION input):   [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
- LangGraph state:                     {"messages": [{"content": "...", "type": "human", ...}], "guardrail_flags": []}
- tool args / tool results:            {"order_id": "8129"} / {"content": '{"email": "chen@example.com"}', "type": "tool"}
- @observe captures:                   {"args": ("…",), "kwargs": {}}  (a TUPLE inside)
- raw Python objects from @observe:    a GuardrailResult dataclass, or (AgentRun, trace_id) where AgentRun is a
                                       dataclass whose `messages` are LangChain HumanMessage/AIMessage OBJECTS
- numbers and None
- LangfuseMedia OBJECTS (an attachment you put in an input): the SDK starts the upload, then hands the mask the
  object itself; it becomes the reference "@@@langfuseMedia:type=image/png|id=…|source=bytes@@@" only when the span
  is serialized, AFTER your mask. Turn it into text and the trace loses the attachment (verified live).

Rules for a production masker:
1. Fail closed: anything you don't recognise is turned into text and masked, never passed through raw.
   The one exception is LangfuseMedia: return the very same object (its bytes are a file, not text).
2. Don't over-mask: an order id (8129), a customer id (cust_02), a date, a uuid or a token count must survive, or the
   traces become useless for debugging.
3. Never mutate `data`: the SDK hands you the app's live objects.
"""
from __future__ import annotations

from typing import Any

from langfuse.media import LangfuseMedia  # noqa: F401  (for mask_pii's isinstance check)

CARD_PLACEHOLDER = "[REDACTED_CARD]"
EMAIL_PLACEHOLDER = "[REDACTED_EMAIL]"
PHONE_PLACEHOLDER = "[REDACTED_PHONE]"
PLACEHOLDERS = {"card": CARD_PLACEHOLDER, "email": EMAIL_PLACEHOLDER, "phone": PHONE_PLACEHOLDER}


def luhn_valid(digits: str) -> bool:
    """The Luhn checksum of a string of digits (no separators): True for "4111111111111111", False for
    "4111111111111112". From the right, double every second digit (subtract 9 if > 9) and sum: valid iff sum % 10 == 0.
    A non-digit string or an empty string is False.
    """
    raise NotImplementedError("TODO 1: implement luhn_valid")


def redact_text(text: str) -> str:
    """Replace PII in one string, leaving everything else byte-identical.

    - Card numbers -> CARD_PLACEHOLDER: 13 to 19 digits, optionally grouped by single spaces or dashes
      ("4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111", Amex "3782 822463 10005"), and ONLY when
      Luhn-valid. A digit run glued to letters (inside a uuid or a hex id) is not a card.
    - Email addresses -> EMAIL_PLACEHOLDER ("farah@example.com", "a.b+tag@mail.example.co.uk").
    - Phone numbers -> PHONE_PLACEHOLDER: North-American style 3-3-4 digits with separators, with an optional
      "+<country code>" prefix: "+1 415-555-0132", "(415) 555-0132", "415.555.0132", "415 555 0132".
    Do cards first (a card contains groups that look like a phone number's).
    "Order 8129 on 2026-11-11" is returned unchanged.
    """
    raise NotImplementedError("TODO 1: implement redact_text")


def mask_pii(data: Any) -> Any:
    """Mask every string anywhere in `data`, recursively, and return a NEW JSON-friendly value (never mutate `data`).

    - str: redact_text
    - dict: same keys, masked values (keys are field names, not customer data)
    - list / tuple: masked items, keeping the type (list -> list, tuple -> tuple)
    - None, bool, int, float: unchanged (ids and token counts are numbers; customer text arrives as strings)
    - a LangfuseMedia object: returned as is, the SAME object (an attachment; see the module docstring)
    - an object with a `model_dump()` method (pydantic models, which includes LangChain messages): mask its model_dump()
    - a dataclass instance: a dict of its fields, masked
    - anything else: redact_text(str(obj))  (fail closed)
    """
    raise NotImplementedError("TODO 1: implement mask_pii")
