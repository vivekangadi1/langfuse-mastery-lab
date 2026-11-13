"""Module 03 verify checks against fakes shaped like the live API (server 4.77, fields=...,usage,model)."""
from types import SimpleNamespace as NS

from langfuse_lab.verify import NotYet, check_module_03, module_03_checks

from .test_verify_modules import FakeObservations, obs

MINI, BIG = "m-mini", "m-big"
PRICES_MINI = {"input": 4e-7, "output": 1.6e-6, "input_cache_read": 1e-7}
PRICES_BIG = {"input": 1.75e-6, "output": 1.4e-5, "input_cache_read": 1.75e-7}


def model(mid, name, pattern, prices, *, managed=False, unit="TOKENS"):
    """Shaped like Model.dict(): camelCase, prices as {type: {"price": x}}."""
    return {"id": mid, "modelName": name, "matchPattern": pattern, "unit": unit, "isLangfuseManaged": managed,
            "prices": {k: {"price": v} for k, v in prices.items()}}


def models():
    return [
        model("builtin-1", "gpt-4.1-mini-2025-04-14", "(?i)^(openai/)?(gpt-4.1-mini-2025-04-14)$", PRICES_MINI,
              managed=True),
        model(MINI, "azure-gpt-4.1-mini", r"(?i)^gpt-4\.1-mini(-\d{4}-\d{2}-\d{2})?$", PRICES_MINI),
        model(BIG, "azure-gpt-5.2-chat", r"(?i)^(gpt-5\.2-chat|gpt-chat-latest)$", PRICES_BIG),
    ]


def root(tid, session, user, *, tags=()):
    return obs(id=f"r-{tid}", trace_id=tid, type="AGENT", is_root_observation=True, session_id=session, user_id=user,
               tags=["run:x", *tags], total_cost=None)


def gen(gid, tid, cost, *, model_name="gpt-4.1-mini-2025-04-14", mid=MINI, usage=None, cost_details=None,
        level="DEFAULT"):
    usage = usage or {"input": 600, "output": 40, "total": 640, "input_cache_read": 0}
    cost_details = cost_details if cost_details is not None else ({"total": cost} if cost else {})
    return obs(id=gid, trace_id=tid, type="GENERATION", model=model_name, internal_model_id=mid if cost else "",
               usage_details=usage, cost_details=cost_details, total_cost=cost, level=level)


def run():
    """5 clean turns on mini (3 sessions, users u0/u1/u5) + 2 spike turns on the expensive model (session sB, u2)."""
    items = [
        root("t1", "sA", "u0"), gen("g1", "t1", 0.0004), gen("g1b", "t1", 0.0002),
        root("t2", "sA", "u0"), gen("g2", "t2", 0.0005),
        root("t3", "sC", "u1"), gen("g3", "t3", 0.0006),
        root("t4", "sC", "u1"), gen("g4", "t4", 0.0004),
        root("t5", "sD", "u5"), gen("g5", "t5", 0.0003),
        root("t6", "sB", "u2", tags=["cost_spike"]),
        gen("g6", "t6", 0.012, model_name="gpt-chat-latest", mid=BIG),
        gen("g6b", "t6", 0.002, model_name="gpt-chat-latest", mid=BIG,
            usage={"input": 197, "input_cache_read": 4224, "output": 5, "total": 4426},
            cost_details={"input": 0.00034, "input_cache_read": 0.00074, "output": 0.00007, "total": 0.002}),
        root("t7", "sB", "u2", tags=["cost_spike"]),
        gen("g7", "t7", 0.011, model_name="gpt-chat-latest", mid=BIG),
    ]
    sessions = {"sA": 0.0011, "sC": 0.0010, "sD": 0.0003, "sB": 0.025}
    users = {"u0": 0.0011, "u1": 0.0010, "u5": 0.0003, "u2": 0.025}
    return items, models(), sessions, users, ["sB"]


class FakeModels:
    def __init__(self, rows, per_page=2):
        self.rows, self.per_page = rows, per_page

    def list(self, page=1, limit=100):
        pages = max(1, -(-len(self.rows) // self.per_page))
        chunk = self.rows[(page - 1) * self.per_page: page * self.per_page]
        return NS(data=chunk, meta=NS(total_pages=pages))


def client(items, rows):
    return NS(api=NS(observations=FakeObservations(items), models=FakeModels(rows)), flush=lambda: None)


def results(items, rows, sessions, users, flagged, **kw):
    checks = module_03_checks("run:x", client(items, rows), session_costs=sessions, user_costs=users,
                              flagged_sessions=flagged, **kw)
    return {c.name: _call(c) for c in checks}


def _call(check):
    try:
        return check.fn()
    except NotYet as exc:
        return ("not_yet", str(exc))


def test_all_pass():
    r = results(*run())
    assert all(v[0] is True for v in r.values()), r
    assert "4224 cached" in r["usage details captured (incl. cached tokens)"][1]


def test_paginates_and_ignores_builtins():
    items, rows, s, u, f = run()
    rows = [model(f"b{i}", f"builtin-{i}", "x", PRICES_MINI, managed=True) for i in range(5)] + rows
    assert results(items, rows, s, u, f)["model definitions defined once"][0] is True


def test_missing_and_duplicate_definitions_fail():
    items, rows, s, u, f = run()
    ok, detail = results(items, rows[:2], s, u, f)["model definitions defined once"]
    assert not ok and "azure-gpt-5.2-chat: missing" in detail
    ok, detail = results(items, rows + [{**rows[2], "id": "m-big2"}], s, u, f)["model definitions defined once"]
    assert not ok and "duplicates" in detail


def test_per_million_prices_and_missing_cache_price_fail():
    items, rows, s, u, f = run()
    rows[1] = model(MINI, "azure-gpt-4.1-mini", "p", {"input": 0.4, "output": 1.6, "input_cache_read": 0.1})
    rows[2] = model(BIG, "azure-gpt-5.2-chat", "p", {"input": 1.75e-6, "output": 1.4e-5})
    ok, detail = results(items, rows, s, u, f)["model definitions defined once"]
    assert not ok and "per TOKEN" in detail and "no price for input_cache_read" in detail


def test_unmatched_model_fails():
    items, rows, s, u, f = run()
    items[-1] = gen("g7", "t7", None, model_name="gpt-chat-latest")  # live shape: cost_details={}, total_cost=None
    ok, detail = results(items, rows, s, u, f)["every generation priced by your definitions"]
    assert not ok and "'gpt-chat-latest' has no cost" in detail


def test_builtin_priced_generation_fails():
    items, rows, s, u, f = run()
    items[1] = gen("g1", "t1", 0.0004, mid="builtin-1")
    ok, detail = results(items, rows, s, u, f)["every generation priced by your definitions"]
    assert not ok and "built-in" in detail and "gpt-4.1-mini-2025-04-14" in detail


def test_failed_llm_call_is_ignored():
    items, rows, s, u, f = run()
    items.append(gen("gx", "t1", None, model_name="gpt-5.2-chat", usage={"input": 0, "output": 0}, level="ERROR"))
    r = results(items, rows, s, u, f)
    assert r["every generation priced by your definitions"][0] is True
    assert r["usage details captured (incl. cached tokens)"][0] is True


def test_unpriced_cached_tokens_fail():
    items, rows, s, u, f = run()
    items[13] = gen("g6b", "t6", 0.002, model_name="gpt-chat-latest", mid=BIG,
                    usage={"input": 197, "input_cache_read": 4224, "output": 5},
                    cost_details={"input": 0.00034, "output": 0.00007, "total": 0.002})
    ok, detail = results(items, rows, s, u, f)["usage details captured (incl. cached tokens)"]
    assert not ok and "input_cache_read price" in detail


def test_session_costs_must_match():
    items, rows, s, u, f = run()
    ok, detail = results(items, rows, {**s, "sB": 0.0}, u, f)["cost per session (your query)"]
    assert not ok and "sB" in detail
    ok, detail = results(items, rows, {k: v for k, v in s.items() if k != "sD"}, u, f)["cost per session (your query)"]
    assert not ok and "missing=['sD']" in detail
    assert results(items, rows, {**s, "sB": 0.02501}, u, f)["cost per session (your query)"][0] is True  # within 1%


def test_user_costs_must_match():
    items, rows, s, u, f = run()
    ok, detail = results(items, rows, s, {**u, "other": 1.0}, f)["cost per user (your query)"]
    assert not ok and "extra=['other']" in detail


def test_spike_not_flagged_or_false_alarm_fails():
    items, rows, s, u, _ = run()
    ok, detail = results(items, rows, s, u, [])["planted cost_spike caught"]
    assert not ok and "missed ['sB']" in detail
    ok, detail = results(items, rows, s, u, ["sB", "sA"])["planted cost_spike caught"]
    assert not ok and "false alarms ['sA']" in detail


def test_weak_spike_fails():
    """If the expensive model were priced like the cheap one, the spike would not stand out."""
    items, rows, s, u, f = run()
    items[12] = gen("g6", "t6", 0.0001, model_name="gpt-chat-latest", mid=BIG)
    items[13] = gen("g6b", "t6", 0.0001, model_name="gpt-chat-latest", mid=BIG)
    ok, detail = results(items, rows, s, u, f)["planted cost_spike caught"]
    assert not ok and "not >= 5x" in detail


def test_not_ingested_yet_is_not_yet():
    items, rows, s, u, f = run()
    r = results(items[:4], rows, s, u, f)
    assert all(v[0] == "not_yet" for v in r.values()), r


def test_check_module_03_reports(capsys):
    items, rows, s, u, f = run()
    assert check_module_03("run:x", session_costs=s, user_costs=u, flagged_sessions=f,
                           client=client(items, rows), retries=1) is True
    assert "MODULE PASSED" in capsys.readouterr().out


# ---------------------------------------------------------------- one deployment for everything (e.g. gpt-5.4)
ONE = "m-one"
PRICES_ONE = {"input": 2.5e-6, "output": 1.5e-5, "input_cache_read": 2.5e-7, "output_reasoning": 1.5e-5}


def single_run():
    """Same 7 turns, all on ONE model: the spike is the uncached padded prompt (big input, no cache reads)."""
    def g(gid, tid, cost, **kw):
        return gen(gid, tid, cost, model_name="gpt-5.4-2026-03-05", mid=ONE, **kw)

    items = [
        root("t1", "sA", "u0"), g("g1", "t1", 0.0015), g("g1b", "t1", 0.0012),
        root("t2", "sA", "u0"), g("g2", "t2", 0.0018),
        root("t3", "sC", "u1"), g("g3", "t3", 0.0016),
        root("t4", "sC", "u1"), g("g4", "t4", 0.0014),
        root("t5", "sD", "u5"), g("g5", "t5", 0.0013),
        root("t6", "sB", "u2", tags=["cost_spike"]),
        g("g6", "t6", 0.029, usage={"input": 11400, "input_cache_read": 0, "output": 40, "total": 11440}),
        g("g6b", "t6", 0.029, usage={"input": 11500, "input_cache_read": 0, "output": 30, "total": 11530}),
        root("t7", "sB", "u2", tags=["cost_spike"]),
        g("g7", "t7", 0.030),
    ]
    rows = [model("builtin-5", "gpt-5.4", "(?i)^(openai/)?(gpt-5.4)$", PRICES_ONE, managed=True),
            model(ONE, "azure-gpt-5.4", r"(?i)^gpt-5\.4(-\d{4}-\d{2}-\d{2})?$", PRICES_ONE)]
    sessions = {"sA": 0.0045, "sC": 0.0030, "sD": 0.0013, "sB": 0.088}
    users = {"u0": 0.0045, "u1": 0.0030, "u5": 0.0013, "u2": 0.088}
    return items, rows, sessions, users, ["sB"]


def test_single_deployment_passes_with_its_one_definition():
    r = results(*single_run(), model_defs=("azure-gpt-5.4",))
    assert all(v[0] is True for v in r.values()), r
    assert "azure-gpt-5.4" in r["every generation priced by your definitions"][1]


def test_single_deployment_still_needs_the_pair_by_default():
    """The default stays the two-deployment pair: nothing passes by accident when the notebook forgets model_defs."""
    ok, detail = results(*single_run())["model definitions defined once"]
    assert not ok and "azure-gpt-4.1-mini: missing" in detail


def test_check_module_03_passes_model_defs_through(capsys):
    items, rows, s, u, f = single_run()
    assert check_module_03("run:x", session_costs=s, user_costs=u, flagged_sessions=f, client=client(items, rows),
                           retries=1, model_defs=("azure-gpt-5.4",)) is True
    assert "MODULE PASSED" in capsys.readouterr().out


def test_unpriced_reasoning_tokens_fail_and_priced_ones_pass():
    items, rows, s, u, f = single_run()
    reasoning = {"input": 400, "output": 30, "output_reasoning": 220, "total": 650}
    items[1] = gen("g1", "t1", 0.0015, model_name="gpt-5.4-2026-03-05", mid=ONE, usage=reasoning,
                   cost_details={"input": 0.001, "output": 0.00045, "total": 0.0015})
    ok, detail = results(items, rows, s, u, f, model_defs=("azure-gpt-5.4",))[
        "usage details captured (incl. cached tokens)"]
    assert not ok and "220 reasoning tokens" in detail and "output_reasoning price" in detail
    items[1] = gen("g1", "t1", 0.0015, model_name="gpt-5.4-2026-03-05", mid=ONE, usage=reasoning,
                   cost_details={"input": 0.001, "output": 0.00045, "output_reasoning": 0.0033, "total": 0.0048})
    ok, detail = results(items, rows, s, u, f, model_defs=("azure-gpt-5.4",))[
        "usage details captured (incl. cached tokens)"]
    assert ok and "220 reasoning token(s), priced" in detail


def test_single_deployment_spike_served_from_cache_is_too_weak():
    """If the padded prompt were cached (no cache-busting header), the one-model spike would not stand out."""
    items, rows, s, u, f = single_run()
    items[12] = gen("g6", "t6", 0.002, model_name="gpt-5.4-2026-03-05", mid=ONE)  # cached: ~2x a clean call
    items[13] = gen("g6b", "t6", 0.002, model_name="gpt-5.4-2026-03-05", mid=ONE)
    ok, detail = results(items, rows, s, u, f, model_defs=("azure-gpt-5.4",))["planted cost_spike caught"]
    assert not ok and "not >= 5x" in detail and "uncached" in detail
