"""Module 05 TODO 1. These fail until you implement langfuse_mastery/masking.py.

Run from ch09/:   .venv-langfuse/Scripts/python.exe -m pytest langfuse_mastery/tests/test_masking.py -q

The shapes below are what the Langfuse `mask=` callback really receives (recorded live from one agent turn).
"""
import copy
from dataclasses import dataclass

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langfuse.media import LangfuseMedia

from langfuse_lab.guardrail import GuardrailResult
from langfuse_lab.traffic import PII_SUFFIX
from langfuse_mastery.masking import (CARD_PLACEHOLDER as CARD, EMAIL_PLACEHOLDER as EMAIL,
                                      PHONE_PLACEHOLDER as PHONE, luhn_valid, mask_pii, redact_text)

PASTED = "I was charged twice for order 8129." + PII_SUFFIX
PASTED_MASKED = f"I was charged twice for order 8129. My card number is {CARD}."


@pytest.mark.parametrize("digits, ok", [
    ("4111111111111111", True), ("4111111111111112", False), ("5555555555554444", True),
    ("378282246310005", True), ("79927398713", True), ("0", True), ("", False), ("4111 1111", False),
])
def test_luhn_valid(digits, ok):
    assert luhn_valid(digits) is ok


@pytest.mark.parametrize("text, expected", [
    (PASTED, PASTED_MASKED),
    ("card 4111-1111-1111-1111 please", f"card {CARD} please"),
    ("card 4111111111111111", f"card {CARD}"),
    ("amex 3782 822463 10005.", f"amex {CARD}."),
    ("mastercard 5555 5555 5555 4444, thanks", f"mastercard {CARD}, thanks"),
    ("two cards: 4111 1111 1111 1111 and 5555555555554444", f"two cards: {CARD} and {CARD}"),
    ("email farah@example.com now", f"email {EMAIL} now"),
    ("a.b+tag@mail.example.co.uk", EMAIL),
    ("call +1 415-555-0132 today", f"call {PHONE} today"),
    ("(415) 555-0132", PHONE),
    ("415.555.0132 or 415 555 0132", f"{PHONE} or {PHONE}"),
    ("Call me back at +1 415-555-0132 or email farah@example.com.", f"Call me back at {PHONE} or email {EMAIL}."),
])
def test_redact_text_masks_pii(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize("text", [
    "I was charged twice for order 8129. Please fix it.",
    "I can't log in to my account cust_02. Please reset my password.",
    "Order 8129 on 2026-11-11 at 14:40:09, amount $206.22, 3 items",
    "not a card (fails Luhn): 4111 1111 1111 1112",
    "too short: 4111 1111 111",
    "trace 98bb87abc6b9076f3119b1e9740e2eb6, run lc_run--01a1cb0d-5007-747d-a4ab-285675235c8c",
    "agent:08dc598d-3d95-edd5-1143-459802812a86",
    "@@@langfuseMedia:type=image/png|id=j4180rNKyB8ZlOv0etsBVf|source=bytes@@@",
    "tokens: 357 in, 16 out; latency 861 ms",
    "",
])
def test_redact_text_does_not_over_mask(text):
    assert redact_text(text) == text


def test_mask_pii_on_a_generation_input():
    messages = [{"role": "system", "content": "You are Acme's support agent."},
                {"role": "user", "content": PASTED}]
    assert mask_pii(messages) == [messages[0], {"role": "user", "content": PASTED_MASKED}]


def test_mask_pii_on_langgraph_state_and_metadata():
    state = {"messages": [{"content": PASTED, "type": "human", "id": None, "additional_kwargs": {}}],
             "guardrail_flags": ["pii_card_number"], "blocked": False}
    out = mask_pii(state)
    assert out["messages"][0]["content"] == PASTED_MASKED
    assert out["guardrail_flags"] == ["pii_card_number"] and out["blocked"] is False
    meta = {"langgraph_step": 2, "langgraph_triggers": ("branch:to:agent",), "ls_temperature": 0.0}
    assert mask_pii(meta) == meta and isinstance(mask_pii(meta)["langgraph_triggers"], tuple)


def test_mask_pii_on_tool_args_and_results():
    assert mask_pii({"order_id": "8129"}) == {"order_id": "8129"}
    result = {"content": '{"reset_link_sent": true, "email": "chen@example.com"}', "type": "tool"}
    assert mask_pii(result) == {"content": '{"reset_link_sent": true, "email": "' + EMAIL + '"}', "type": "tool"}


def test_mask_pii_on_observe_captures_with_tuples():
    out = mask_pii({"args": (PASTED, 3), "kwargs": {}})
    assert out == {"args": (PASTED_MASKED, 3), "kwargs": {}}


def test_mask_pii_on_langchain_message_objects():
    out = mask_pii([HumanMessage(content=PASTED), AIMessage(content="Your card 4111 1111 1111 1111 is noted.")])
    assert isinstance(out, list) and all(isinstance(m, dict) for m in out)
    assert out[0]["content"] == PASTED_MASKED and out[0]["type"] == "human"
    assert out[1]["content"] == f"Your card {CARD} is noted."


@dataclass
class FakeAgentRun:  # shaped like langfuse_lab.agent.AgentRun
    messages: list
    summary: dict
    guardrail_flags: list
    blocked: bool


def test_mask_pii_on_the_support_request_return_value():
    run = FakeAgentRun(messages=[HumanMessage(content=PASTED)], summary={"final_answer": "Escalated, order 8129."},
                       guardrail_flags=["pii_card_number"], blocked=False)
    out = mask_pii((run, "98bb87abc6b9076f3119b1e9740e2eb6"))
    assert isinstance(out, tuple) and out[1] == "98bb87abc6b9076f3119b1e9740e2eb6"
    assert out[0]["messages"][0]["content"] == PASTED_MASKED
    assert out[0]["summary"] == {"final_answer": "Escalated, order 8129."} and out[0]["blocked"] is False


def test_mask_pii_on_a_guardrail_result():
    assert mask_pii(GuardrailResult(allowed=True, flags=("pii_card_number",))) == {
        "allowed": True, "flags": ("pii_card_number",)}


def test_mask_pii_keeps_attachments_intact():
    photo = LangfuseMedia(content_bytes=b"PNG fake", content_type="image/png")
    out = mask_pii({"message": "call +1 415-555-0132", "photo": photo, "files": [photo]})
    assert out["photo"] is photo and out["files"][0] is photo
    assert out["message"] == f"call {PHONE}"


def test_mask_pii_fails_closed_on_unknown_objects():
    class Ticket:
        def __str__(self):
            return "ticket from farah@example.com"

    assert mask_pii(Ticket()) == f"ticket from {EMAIL}"


@pytest.mark.parametrize("value", [None, True, 0, 8129, 4111111111111111, 0.5, "", "cust_02"])
def test_mask_pii_leaves_scalars_and_ids(value):
    assert mask_pii(value) == value


def test_mask_pii_never_mutates_its_input():
    data = {"messages": [{"content": PASTED}], "args": [PASTED, ("farah@example.com",)]}
    before = copy.deepcopy(data)
    mask_pii(data)
    assert data == before
    message = HumanMessage(content=PASTED)
    mask_pii(message)
    assert message.content == PASTED
