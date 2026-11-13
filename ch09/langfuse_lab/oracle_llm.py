"""A deterministic tool-calling chat model for DRY rehearsals (no Azure calls). No Langfuse code here.

It plays the support agent's policy with keyword rules: look up what the request names, hand off when the routing rules
say so, then answer. LangGraph, the ToolNode and the tools run for real (so `slow_tool` really sleeps and a missing
order really fails); only the LLM is replaced. Its generations report the model name `oracle-dry-run`, which no model
definition prices, so dry traffic has usage but no cost.

It follows the system prompt's login rule (module 12): served the bad version (`prompts.WRONG_HANDOFF_TEMPLATE`,
"... ALWAYS create_handoff to billing_specialist"), it routes login issues to billing like the real agent does. It also
follows the double-charge rule (module 14): served the quick fix (`prompts.QUICK_FIX_TEMPLATE`, "double charge, refund,
invoice -> account_security"), it misroutes double charges the same way.
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

MODEL_NAME = "oracle-dry-run"
LOGIN_TEAM = "account_security"
BILLING_TEAM = "billing_specialist"
_LOGIN_RULE = re.compile(r"login, password, account access ->[^\n]*?create_handoff to (\w+)")
_BILLING_RULE = re.compile(r"double charge, refund, invoice -> (\w+)")


def login_team(system_prompt: str | None) -> str:
    """The team the system prompt's routing rules send login issues to (account_security when it doesn't say)."""
    m = _LOGIN_RULE.search(system_prompt or "")
    return m.group(1) if m else LOGIN_TEAM


def billing_team(system_prompt: str | None) -> str:
    """The team the system prompt's routing rules send double charges to (billing_specialist when it doesn't say)."""
    m = _BILLING_RULE.search(system_prompt or "")
    return m.group(1) if m else BILLING_TEAM


def plan_calls(text: str, *, login_to: str = LOGIN_TEAM, billing_to: str = BILLING_TEAM) -> list[tuple[str, dict]]:
    """The tool calls an agent following the routing rules makes for `text`, in order (create_handoff last).
    `login_to` / `billing_to`: where the system prompt sends login issues / double charges (`login_team`,
    `billing_team`)."""
    t = (text or "").lower()
    order = re.search(r"order (\d+)", t)
    customer = re.search(r"cust_\d+", t)
    subscription = re.search(r"sub_\d+", t)
    if any(k in t for k in ("log in", "login", "password", "locked out")):
        calls = [("reset_password", {"customer_id": customer.group()})] if customer else []
        return calls + [("create_handoff", {"target": login_to, "reason": "account access"})]
    if "charged twice" in t or "two charges" in t:
        calls = [("lookup_order", {"order_id": order.group(1)})] if order else []
        return calls + [("create_handoff", {"target": billing_to, "reason": "double charge"})]
    if order and ("where is" in t or "arrive" in t):
        return [("lookup_shipment", {"order_id": order.group(1)})]
    if "cancel" in t and subscription:
        return [("cancel_subscription", {"subscription_id": subscription.group()})]
    if "policy" in t or "return" in t:
        return [("search_kb", {"query": text})]
    return []


def _turn(messages: list[BaseMessage]) -> tuple[str, list[BaseMessage]]:
    """The last user message and everything after it (this turn's tool calls and results)."""
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return str(messages[i].content), messages[i + 1:]
    return "", []


def decide(messages: list[BaseMessage]) -> AIMessage:
    text, turn = _turn(messages)
    system = next((str(m.content) for m in messages if isinstance(m, SystemMessage)), None)
    called = [c["name"] for m in turn if isinstance(m, AIMessage) for c in m.tool_calls]
    pending = [(name, args) for name, args in plan_calls(text, login_to=login_team(system),
                                                            billing_to=billing_team(system)) if name not in called]
    usage = {"input_tokens": sum(len(str(m.content)) for m in messages) // 4 + 1, "output_tokens": 12}
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    meta = {"model_name": MODEL_NAME}
    if pending:
        name, args = pending[0]
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"oracle-{len(called)}"}],
                         usage_metadata=usage, response_metadata=meta)
    failed = any(isinstance(m, ToolMessage) and "error" in str(m.content) for m in turn)
    answer = ("I could not find that order; please check the number." if failed
              else "Done: I have taken care of that for you.")
    return AIMessage(content=answer, usage_metadata=usage, response_metadata=meta)


class OracleChatModel(BaseChatModel):
    """Drop-in for AzureChatOpenAI in `SupportAgent(llm=OracleChatModel())`. bind_tools is a no-op."""

    model_name: str = MODEL_NAME

    @property
    def _llm_type(self) -> str:
        return MODEL_NAME

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=decide(messages))])
