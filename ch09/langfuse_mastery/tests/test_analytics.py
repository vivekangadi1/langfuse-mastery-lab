"""Module 11 TODO 1: pure analytics helpers (no server needed)."""
from datetime import datetime, timedelta, timezone

import pytest

from langfuse_mastery import analytics as a

UTC = timezone.utc


# ---------------------------------------------------------------- week_windows
def test_week_windows_mid_week():
    prev, curr = a.week_windows(datetime(2026, 11, 12, 15, 30, tzinfo=UTC))  # a Thursday
    assert curr == (datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 9, tzinfo=UTC))
    assert prev == (datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC))


def test_week_windows_on_monday_midnight_reports_the_week_that_just_ended():
    prev, curr = a.week_windows(datetime(2026, 11, 9, 0, 0, tzinfo=UTC))
    assert curr == (datetime(2026, 11, 2, tzinfo=UTC), datetime(2026, 11, 9, tzinfo=UTC))


def test_week_windows_naive_and_other_timezones():
    naive = a.week_windows(datetime(2026, 11, 12, 15, 30))
    assert naive[1][0].tzinfo is not None and naive[1][0] == datetime(2026, 11, 2, tzinfo=UTC)
    # Monday 01:00 in UTC+2 is still Sunday in UTC
    plus2 = timezone(timedelta(hours=2))
    _, curr = a.week_windows(datetime(2026, 11, 9, 1, 0, tzinfo=plus2))
    assert curr == (datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC))


# ---------------------------------------------------------------- flatten / shares / error_rate
def test_flatten():
    rows = [{"name": "lookup_order", "p95_latency": 3001.0}, {"name": "search_kb", "p95_latency": None}]
    assert a.flatten(rows, "latency_ms.tool.{name}.p95", "p95_latency") == {
        "latency_ms.tool.lookup_order.p95": 3001.0, "latency_ms.tool.search_kb.p95": None}


def test_flatten_two_dimensions_and_missing_values():
    rows = [{"name": "handoff_correct", "source": "API", "avg_value": 0.5},
            {"name": "x", "source": None, "avg_value": 1}, {"name": "", "source": "EVAL", "avg_value": 0}]
    assert a.flatten(rows, "score.{name}.{source}", "avg_value") == {
        "score.handoff_correct.API": 0.5, "score.x.none": 1, "score.none.EVAL": 0}
    assert a.flatten([], "x.{name}", "v") == {}


def test_shares():
    assert a.shares({"DEFAULT": 6, "WARNING": 2}) == {"DEFAULT": 0.75, "WARNING": 0.25}
    assert a.shares({"a": None, "b": 2}) == {"a": 0.0, "b": 1.0}
    assert a.shares({}) == {} and a.shares({"a": 0}) == {}


def test_error_rate():
    assert a.error_rate({"DEFAULT": 5, "WARNING": 2}) == pytest.approx(2 / 7)
    assert a.error_rate({"WARNING": 1}) == 1.0
    assert a.error_rate({"DEFAULT": 3}) == 0.0
    assert a.error_rate({}) is None


# ---------------------------------------------------------------- per_trace / resolved / rollup
class Level:  # like ObservationLevel
    def __init__(self, value):
        self.value = value


def obs(oid, tid, *, root=False, cost=None, latency=0.5, user="u1", session="s1", version="v1", level="DEFAULT",
        parent="p"):
    return {"id": oid, "traceId": tid, "isRootObservation": root, "parentObservationId": None if root else parent,
            "totalCost": cost, "latency": latency, "userId": user, "sessionId": session, "version": version,
            "level": level}


def observations():
    return [
        obs("r1", "t1", root=True, latency=2.5, level=Level("DEFAULT")), obs("g1", "t1", cost=0.001),
        obs("g2", "t1", cost=0.002),
        obs("r2", "t2", root=True, latency=7.25, user="u2", session="s2", version="v2", level=Level("WARNING")),
        obs("g3", "t2", cost=0.004, user="u2", session="s2", version="v2"),
        obs("r3", "t3", root=True, latency=1.0, session="s3", level=None),
        obs("x1", "t4", cost=0.5),  # a trace whose root is not ingested yet
    ]


def test_per_trace():
    t = a.per_trace(observations())
    assert set(t) == {"t1", "t2", "t3"}
    assert t["t1"]["cost_usd"] == pytest.approx(0.003) and t["t1"]["observations"] == 3
    assert t["t1"]["latency_ms"] == pytest.approx(2500.0) and t["t1"]["level"] == "DEFAULT"
    assert t["t2"] == pytest.approx({"cost_usd": 0.004, "observations": 2, "user_id": "u2", "session_id": "s2",
                                     "version": "v2", "level": "WARNING", "latency_ms": 7250.0})
    assert t["t3"]["cost_usd"] == 0.0 and t["t3"]["level"] == "DEFAULT"


def test_per_trace_root_fallback_on_parent_id():
    rows = [{"traceId": "t", "parentObservationId": None, "totalCost": 0.1, "latency": 1, "userId": "u",
             "sessionId": "s", "version": None, "level": "ERROR"},
            {"traceId": "t", "parentObservationId": "x", "totalCost": None, "latency": 1}]
    t = a.per_trace(rows)
    assert t["t"]["level"] == "ERROR" and t["t"]["latency_ms"] == 1000 and t["t"]["cost_usd"] == 0.1


def score(name, value, ts, source="API"):
    return {"name": name, "value": value, "timestamp": ts, "source": source}


def test_latest_value():
    rows = [score("handoff_correct", False, "2026-11-12T08:00:00Z"), score("handoff_correct", True,
                                                                           "2026-11-12T08:00:05Z"),
            score("human", False, "2026-11-12T08:01:00Z", source=Level("ANNOTATION")),
            score("human", True, "2026-11-12T08:02:00Z", source="API")]
    assert a.latest_value(rows, "handoff_correct") is True
    assert a.latest_value(rows, "human", sources={"ANNOTATION"}) is False
    assert a.latest_value(rows, "human") is True
    assert a.latest_value(rows, "nope") is None
    assert a.latest_value([], "x", sources={"EVAL"}) is None


def test_resolved_needs_true_score_and_default_level():
    t = a.per_trace(observations())
    scores = {"t1": [score("handoff_correct", True, "1")],
              "t2": [score("handoff_correct", True, "1")],      # WARNING root: not resolved
              "t3": [score("handoff_correct", "false", "1")]}
    assert a.resolved(t, scores) == {"t1"}
    assert a.resolved(t, {"t3": [score("handoff_correct", 1, "1")]}) == {"t3"}
    assert a.resolved(t, {}) == set()
    assert a.resolved(t, {"t1": [score("other", True, "1")]}, score_name="other") == {"t1"}


def test_rollup():
    t = a.per_trace(observations())
    by_user = a.rollup(t, {"t1"}, by="user_id")
    assert set(by_user) == {"u1", "u2"}
    assert by_user["u1"] == pytest.approx({"requests": 2, "cost_usd": 0.003, "resolved": 1})
    assert by_user["u2"] == pytest.approx({"requests": 1, "cost_usd": 0.004, "resolved": 0})
    assert set(a.rollup(t, set(), by="session_id")) == {"s1", "s2", "s3"}
    t["t3"]["version"] = None
    assert a.rollup(t, set(), by="version")["none"]["requests"] == 1


def test_cost_per_resolved():
    t = a.per_trace(observations())
    assert a.cost_per_resolved(t, {"t1"}) == pytest.approx(0.007)       # ALL cost / 1 resolved
    assert a.cost_per_resolved(t, {"t1", "t3"}) == pytest.approx(0.0035)
    assert a.cost_per_resolved(t, {"t1", "t-other"}) == pytest.approx(0.007)  # unknown ids don't count
    assert a.cost_per_resolved(t, set()) is None


def test_agreement_rate():
    assert a.agreement_rate([(True, True), (False, True), (None, True), (True, None), (0, False)]) == {
        "n": 3, "agree": 2, "rate": pytest.approx(2 / 3)}
    assert a.agreement_rate([]) == {"n": 0, "agree": 0, "rate": None}


# ---------------------------------------------------------------- compare / flag
def test_compare():
    c = a.compare({"x": 10, "y": None, "z": 0, "s": "v1", "b": True, "gone": 2},
                  {"x": 15, "y": 3, "z": 4, "s": "v2", "b": False, "new": 1})
    assert c["x"] == {"prev": 10, "curr": 15, "delta": 5, "pct": 0.5}
    assert c["y"] == {"prev": None, "curr": 3, "delta": None, "pct": None}
    assert c["z"] == {"prev": 0, "curr": 4, "delta": 4, "pct": None}
    assert c["gone"]["curr"] is None and c["new"]["prev"] is None
    assert "s" not in c and "b" not in c
    assert list(c) == sorted(c)


def test_compare_negative_prev_uses_abs():
    assert a.compare({"x": -2.0}, {"x": -1.0})["x"]["pct"] == pytest.approx(0.5)


def test_flag_latency_regression():
    c = a.compare({"latency_ms.tool.lookup_order.p95": 2.0, "latency_ms.request.p95": 4000.0,
                   "latency_ms.tool.search_kb.p95": 1.0},
                  {"latency_ms.tool.lookup_order.p95": 3001.0, "latency_ms.request.p95": 4800.0,
                   "latency_ms.tool.search_kb.p95": 900.0})
    flags = a.flag(c)
    # request: +800 ms < min_abs 1000; search_kb: +899 ms < 1000 (huge pct, tiny abs)
    assert [f["metric"] for f in flags] == ["latency_ms.tool.lookup_order.p95"]
    assert flags[0] == {"metric": "latency_ms.tool.lookup_order.p95", "direction": "up", "prev": 2.0,
                        "curr": 3001.0, "delta": 2999.0, "pct": pytest.approx(1499.5),
                        "rule": "latency_ms.*.p95"}


def test_flag_directions_and_thresholds():
    c = a.compare({"score.handoff_correct.API": 0.9, "score.judge.API": 0.5, "error_rate": 0.1,
                   "resolved_rate": 0.8, "cost_usd.per_resolved": 0.010, "feedback_share.thumbs_down": 0.1,
                   "latency_ms.request.p95": 5000.0, "requests": 7},
                  {"score.handoff_correct.API": 0.7, "score.judge.API": 0.9, "error_rate": 0.25,
                   "resolved_rate": 0.7, "cost_usd.per_resolved": 0.0124, "feedback_share.thumbs_down": 0.4,
                   "latency_ms.request.p95": 2000.0, "requests": 70})
    got = {f["metric"] for f in a.flag(c)}
    # quality down 0.2 (flag), judge UP (no), errors +0.15 (flag), resolved -0.1 (< 0.15), cost +24% (< 25%),
    # thumbs_down +0.3 (flag), latency DOWN (no), requests (no rule)
    assert got == {"score.handoff_correct.API", "error_rate", "feedback_share.thumbs_down"}


def test_flag_first_matching_rule_wins_and_zero_prev():
    rules = ({"pattern": "latency_ms.tool.search_kb.p95", "direction": "down", "min_abs": None, "min_pct": None},
             {"pattern": "latency_ms.*", "direction": "up", "min_abs": 100, "min_pct": 0.5})
    c = a.compare({"latency_ms.tool.search_kb.p95": 1.0, "latency_ms.tool.new.p95": 0},
                  {"latency_ms.tool.search_kb.p95": 5000.0, "latency_ms.tool.new.p95": 3000})
    flags = a.flag(c, rules)
    assert [f["metric"] for f in flags] == ["latency_ms.tool.new.p95"]   # search_kb: its first rule says "down"
    assert flags[0]["pct"] is None and flags[0]["rule"] == "latency_ms.*"


def test_flag_order_and_no_change():
    c = a.compare({"score.a.API": 1.0, "score.b.API": 0.5, "score.c.API": 0.8},
                  {"score.a.API": 0.5, "score.b.API": 0.0, "score.c.API": 0.8})
    assert [f["metric"] for f in a.flag(c)] == ["score.b.API", "score.a.API"]  # -100% before -50%; c unchanged
    assert a.flag({}) == []
