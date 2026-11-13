"""Support tools over the fixtures. `search_fn` lets a notebook inject an instrumented retriever."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from langchain_core.tools import StructuredTool

from . import kb
from .failures import FailureConfig
from .fixtures import Fixtures, load_fixtures

HandoffTarget = Literal["billing_specialist", "account_security", "shipping_specialist"]


@dataclass
class ToolContext:
    fixtures: Fixtures = field(default_factory=load_fixtures)
    failures: FailureConfig = field(default_factory=FailureConfig)
    search_fn: Callable[[str, int], list] | None = None
    handoffs: list = field(default_factory=list)  # per-invocation state: cleared by SupportAgent.invoke; not safe for concurrent conversations on one instance
    sleep: Callable[[float], None] = time.sleep


def make_tools(ctx: ToolContext) -> list[StructuredTool]:
    def maybe_slow():
        if ctx.failures.slow_tool_seconds:
            ctx.sleep(ctx.failures.slow_tool_seconds)

    def lookup_order(order_id: str) -> dict:
        """Look up an order's amount, status, and customer by order id."""
        maybe_slow()
        order = ctx.fixtures.orders.get(order_id)
        return dict(order) if order else {"error": "order_not_found", "order_id": order_id}

    def lookup_shipment(order_id: str) -> dict:
        """Look up carrier, shipping status, and ETA in days for an order."""
        maybe_slow()
        order = ctx.fixtures.orders.get(order_id)
        if not order:
            return {"error": "order_not_found", "order_id": order_id}
        return {k: order[k] for k in ("order_id", "carrier", "status", "eta_days")}

    def reset_password(customer_id: str) -> dict:
        """Send a password reset link to a customer."""
        customer = ctx.fixtures.customers.get(customer_id)
        if not customer:
            return {"error": "customer_not_found", "customer_id": customer_id}
        return {"reset_link_sent": True, "email": customer["email"]}

    def cancel_subscription(subscription_id: str) -> dict:
        """Cancel a customer's subscription by subscription id."""
        if not any(c["subscription_id"] == subscription_id for c in ctx.fixtures.customers.values()):
            return {"error": "subscription_not_found", "subscription_id": subscription_id}
        return {"cancelled": True, "subscription_id": subscription_id}

    def search_kb(query: str) -> list:
        """Search help-center articles; returns the top 3 matches."""
        search = ctx.search_fn or (lambda q, k: kb.search(q, ctx.fixtures.kb, k=k))
        return search(query, 3)

    def create_handoff(target: HandoffTarget, reason: str) -> dict:
        """Escalate the conversation to a specialist team."""
        record = {"target": target, "reason": reason}
        ctx.handoffs.append(record)
        return {"handoff_created": True, **record}

    functions = [lookup_order, lookup_shipment, reset_password, cancel_subscription, search_kb, create_handoff]
    return [StructuredTool.from_function(fn) for fn in functions]
