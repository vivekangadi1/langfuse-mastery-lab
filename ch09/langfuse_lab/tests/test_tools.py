from langfuse_lab.failures import FailureConfig
from langfuse_lab.tools import ToolContext, make_tools


def tools_by_name(ctx):
    return {t.name: t for t in make_tools(ctx)}


def test_tool_names():
    assert set(tools_by_name(ToolContext())) == {
        "lookup_order", "lookup_shipment", "reset_password", "cancel_subscription", "search_kb", "create_handoff",
    }


def test_lookup_order_found_and_missing():
    t = tools_by_name(ToolContext())["lookup_order"]
    assert t.invoke({"order_id": "8129"})["status"] == "charged_twice"
    assert t.invoke({"order_id": "nope"})["error"] == "order_not_found"


def test_slow_tool_sleeps():
    slept = []
    ctx = ToolContext(failures=FailureConfig(slow_tool_seconds=3.0), sleep=slept.append)
    tools_by_name(ctx)["lookup_shipment"].invoke({"order_id": "9912"})
    assert slept == [3.0]


def test_handoff_recorded():
    ctx = ToolContext()
    tools_by_name(ctx)["create_handoff"].invoke({"target": "billing_specialist", "reason": "double charge"})
    assert ctx.handoffs == [{"target": "billing_specialist", "reason": "double charge"}]


def test_search_fn_override():
    calls = []
    ctx = ToolContext(search_fn=lambda q, k: calls.append((q, k)) or [])
    tools_by_name(ctx)["search_kb"].invoke({"query": "refund"})
    assert calls == [("refund", 3)]


def test_account_security_target_accepted():
    ctx = ToolContext()
    tools_by_name(ctx)["create_handoff"].invoke({"target": "account_security", "reason": "locked out"})
    assert ctx.handoffs == [{"target": "account_security", "reason": "locked out"}]
