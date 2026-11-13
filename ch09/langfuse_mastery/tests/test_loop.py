"""Module 14 TODO 1: the production loop's decisions (`langfuse_mastery/loop.py`). No server needed."""
from datetime import datetime, timedelta, timezone

import pytest

from langfuse_lab.regression import dataset_item_id
from langfuse_mastery import loop

NAMES = {"human": "human_routing_ok", "expected": "expected_handoff", "error_type": "review_error_type"}
T = "2026-11-12T10:00:00+00:00"


def judge(value, *, source="API", evaluator="stand-in:keyword-v1", name="routing_judge"):
    return {"name": name, "value": value, "source": source, "dataType": "BOOLEAN",
            "metadata": {"evaluator": evaluator} if source == "API" else {"evaluator_id": "e1"}, "timestamp": T}


def ann(name, value, *, ts=T, source="ANNOTATION"):
    return {"name": name, "value": value, "source": source, "timestamp": ts, "queueId": "q1"}


# ---------------------------------------------------------------- 1a: the policy
def test_policy_is_a_deliberate_release_policy():
    p = loop.LOOP_POLICY
    assert set(p) >= {"reruns", "max_rerun_items", "max_regressions", "max_drop", "incident_pass_rate"}
    assert isinstance(p["reruns"], int) and p["reruns"] >= 1
    assert isinstance(p["max_rerun_items"], int) and p["max_rerun_items"] >= 1
    assert p["max_regressions"] == 0, "an incident fix may not break anything that works today"
    assert 0 <= p["max_drop"] <= 0.2
    assert 0.5 < p["incident_pass_rate"] <= 1


# ---------------------------------------------------------------- 1b: the review queue
def test_review_selection_flags_false_verdicts_and_adds_a_control():
    scores = {"t1": [judge(True)], "t2": [judge(False)], "t3": [judge(0.0, source="EVAL")], "t4": [judge(True)]}
    sel = loop.review_selection(scores, salt="s")
    assert sel["flagged"] == ["t2", "t3"]
    assert sel["unjudged"] == []
    assert len(sel["controls"]) == 1 and sel["controls"][0] in ("t1", "t4")
    assert sel["queue"] == [t for t in scores if t in {"t2", "t3", *sel["controls"]}]
    assert sel["reasons"]["t2"] == "routing_judge=False" and sel["reasons"][sel["controls"][0]] == "control"
    assert list(sel["reasons"]) == sel["queue"]


def test_review_selection_queues_unjudged_requests_and_ignores_non_judge_scores():
    code = {"name": "routing_judge", "value": False, "source": "API", "metadata": {"evaluator": "code:v1"}}
    scores = {"t1": [code], "t2": [], "t3": [judge(True)], "t4": [judge(False)]}
    sel = loop.review_selection(scores, salt="s")
    assert sel["flagged"] == ["t4"]
    assert sel["unjudged"] == ["t1", "t2"]  # a code score is not the judge; no score at all is not a pass
    assert sel["controls"] == ["t3"]
    assert sel["queue"] == ["t1", "t2", "t3", "t4"] and sel["reasons"]["t1"] == "unjudged"


def test_review_selection_uses_the_judge_name_and_is_deterministic():
    scores = {f"t{i}": [judge(True, name="zz_routing_judge")] for i in range(8)}
    scores["t9"] = [judge(False, name="zz_routing_judge")]
    a = loop.review_selection(scores, judge="zz_routing_judge", control_rate=0.25, salt="run1")
    b = loop.review_selection(dict(scores), judge="zz_routing_judge", control_rate=0.25, salt="run1")
    assert a == b and a["flagged"] == ["t9"] and len(a["controls"]) == 2
    assert loop.review_selection(scores, salt="x")["unjudged"] == list(scores)  # wrong judge name: nothing judged
    assert loop.review_selection({}) == {"queue": [], "reasons": {}, "flagged": [], "unjudged": [], "controls": []}


# ---------------------------------------------------------------- 1c: labels -> items
def review_row(tid, message, handoff, scores):
    return {"trace_id": tid, "message": message, "handoff": handoff, "scores": scores}


def test_incident_items_turn_confirmed_failures_into_labelled_tests():
    msg = "  I can't log in to my account cust_02. Please reset my password. "
    rows = [review_row("abcdef1234", msg, "billing_specialist",
                       [ann("human_routing_ok", 0.0), ann("expected_handoff", "account_security"),
                        ann("review_error_type", "wrong_team"), judge(False)])]
    items = loop.incident_items(rows, names=NAMES, dataset_name="ds", incident="INC-1")
    assert items == [{
        "id": dataset_item_id("ds", msg.strip()), "input": {"message": msg.strip()},
        "expected_output": {"handoff": "account_security", "reference_answer": None},
        "metadata": {"case_id": "INC-1:abcdef12", "source": "incident", "label": "confirmed_failure",
                     "incident": "INC-1", "error_type": "wrong_team", "trace_id": "abcdef1234"},
        "source_trace_id": "abcdef1234"}]


def test_incident_items_skip_passes_non_human_labels_and_contradictions():
    rows = [
        review_row("t1", "m1", "billing_specialist", [ann("human_routing_ok", True), ann("expected_handoff", "billing_specialist")]),
        review_row("t2", "m2", "billing_specialist", [ann("human_routing_ok", False, source="API"),
                                                      ann("expected_handoff", "account_security")]),
        review_row("t3", "m3", "billing_specialist", [ann("human_routing_ok", False)]),  # no expected team
        review_row("t4", "m4", "billing_specialist", [ann("human_routing_ok", False),
                                                      ann("expected_handoff", "billing_specialist")]),
    ]
    assert loop.incident_items(rows, names=NAMES, dataset_name="ds", incident="INC-1") == []


def test_incident_items_use_the_latest_label_derive_the_error_type_and_dedupe_by_message():
    rows = [
        review_row("t1", "m1", None, [ann("human_routing_ok", True, ts="2026-11-12T10:00:00+00:00"),
                                      ann("human_routing_ok", "false", ts="2026-11-12T10:05:00+00:00"),
                                      ann("expected_handoff", "billing_specialist", ts="2026-11-12T10:00:00+00:00"),
                                      ann("expected_handoff", "account_security", ts="2026-11-12T10:06:00+00:00")]),
        review_row("t2", "m1 ", "billing_specialist", [ann("human_routing_ok", 0), ann("expected_handoff", "account_security")]),
        review_row("t3", "m3", "account_security", [ann("human_routing_ok", False), ann("expected_handoff", "none")]),
    ]
    items = loop.incident_items(rows, names=NAMES, dataset_name="ds", incident="INC-2")
    assert [i["source_trace_id"] for i in items] == ["t1", "t3"]
    assert items[0]["expected_output"]["handoff"] == "account_security"
    assert items[0]["metadata"]["error_type"] == "missed_handoff"      # agent handed off to nobody
    assert items[1]["metadata"]["error_type"] == "unnecessary_handoff"
    zz = {k: "zz_" + v for k, v in NAMES.items()}
    assert loop.incident_items([review_row("t5", "m5", "billing_specialist",
                                           [ann("zz_human_routing_ok", False), ann("zz_expected_handoff", "account_security")])],
                               names=zz, dataset_name="ds", incident="I")[0]["metadata"]["error_type"] == "wrong_team"


# ---------------------------------------------------------------- 1d: gate env
def test_gate_env_maps_the_policy_to_module_10s_variables():
    env = loop.gate_env({"reruns": 1, "max_rerun_items": 3, "max_regressions": 0, "max_drop": 0.1, "max_items": 20,
                         "incident_pass_rate": 1.0})
    assert env == {"GATE_RERUNS": "1", "GATE_MAX_RERUN_ITEMS": "3", "GATE_MAX_REGRESSIONS": "0",
                   "GATE_MAX_DROP": "0.1", "GATE_MAX_ITEMS": "20"}
    assert loop.gate_env({"max_drop": 0.05}) == {"GATE_MAX_DROP": "0.05"} and loop.gate_env({}) == {}


def test_gate_env_round_trips_through_the_harness_policy():
    from langfuse_mastery.ci import harness
    policy = harness.policy_from_env(loop.gate_env({"reruns": 2, "max_regressions": 0, "max_drop": 0.1}))
    assert (policy["reruns"], policy["max_regressions"], policy["max_drop"]) == (2, 0, 0.1)


# ---------------------------------------------------------------- 1e: promotion
POLICY = {"reruns": 1, "max_regressions": 0, "max_drop": 0.1, "incident_pass_rate": 1.0}


def report(decision="ship", *, reasons=(), rates=None, policy=None, version=5):
    return {"decision": {"decision": decision, "reasons": list(reasons), "exit_code": {"ship": 0, "block": 1}.get(decision, 2)},
            "policy": policy or {"reruns": 1, "max_rerun_items": 3, "max_regressions": 0, "max_drop": 0.1},
            "confirmed": {"rates": rates if rates is not None else {"i1": [1.0, 1.0], "i2": [1.0, 1.0], "x": [1.0, 1.0]}},
            "candidate_version": version}


def test_promote_a_shipped_fix_that_passes_the_incident_tests():
    d = loop.promotion_decision(report(), 0, incident_ids=["i1", "i2"], policy=POLICY)
    assert d == {"promote": True, "version": 5, "reasons": ["gate shipped", "2 incident item(s) pass on the candidate"]}


def test_never_promote_a_blocked_fix_or_a_failed_ci_run():
    blocked = loop.promotion_decision(report("block", reasons=["1 regressed item(s) > 0 allowed"]), 1,
                                      incident_ids=["i1"], policy=POLICY)
    assert blocked["promote"] is False
    assert blocked["reasons"] == ["gate decision block (exit 1): 1 regressed item(s) > 0 allowed"]
    crashed = loop.promotion_decision(report(), 3, incident_ids=["i1"], policy=POLICY)
    assert crashed["promote"] is False and crashed["reasons"][0].startswith("gate decision ship (exit 3)")


def test_a_gate_run_looser_than_the_loop_policy_does_not_count():
    loose = report(policy={"reruns": 0, "max_regressions": 1, "max_drop": 0.2})
    d = loop.promotion_decision(loose, 0, incident_ids=["i1"], policy=POLICY)
    assert d["promote"] is False
    assert d["reasons"] == ["gate policy looser than the loop's: max_regressions 1 > 0",
                            "gate policy looser than the loop's: max_drop 0.2 > 0.1",
                            "gate policy looser than the loop's: reruns 0 < 1"]
    missing = report(policy={"reruns": 1, "max_drop": 0.1})
    assert loop.promotion_decision(missing, 0, incident_ids=[], policy=POLICY)["reasons"] == \
        ["gate policy looser than the loop's: max_regressions None > 0"]


def test_the_incident_tests_must_have_run_and_passed_on_the_candidate():
    stale = loop.promotion_decision(report(rates={"x": [1.0, 1.0]}), 0, incident_ids=["i1", "i2"], policy=POLICY)
    assert stale["promote"] is False
    assert stale["reasons"] == ["incident item(s) not in the gate run: i1, i2 (pin the dataset version that contains them)"]
    flaky = report(rates={"i1": [0.5, 0.5], "i2": [1.0, None], "x": [1.0, 1.0]})  # shipped: not regressions vs production
    d = loop.promotion_decision(flaky, 0, incident_ids=["i1", "i2"], policy=POLICY)
    assert d["reasons"] == ["incident item i1 passes 0.5 of attempts on the candidate (< 1.0)",
                            "incident item i2 passes None of attempts on the candidate (< 1.0)"]
    assert loop.promotion_decision(flaky, 0, incident_ids=["i1"], policy={**POLICY, "incident_pass_rate": 0.5})["promote"]


# ---------------------------------------------------------------- 1f: timeline
def ev(stage, minute, **extra):
    return {"stage": stage, "at": f"2026-11-12T10:{minute:02d}:00+00:00", **extra}


def full(**overrides):
    order = {"baseline": 0, "deploy": 2, "judge": 3, "alert": 5, "review": 8, "dataset": 10, "fix": 11,
             "experiment": 12, "gate": 14, "promote": 15, "recovered": 16, "resolved": 18}
    order.update(overrides)
    return [ev(s, m) for s, m in order.items()]


def test_timeline_sorts_events_and_measures_the_incident():
    events = full() + [ev("fix", 13, kind="fix"), ev("review", 9)]
    t = loop.timeline(list(reversed(events)))
    assert [e["at"] for e in t["events"]] == sorted(e["at"] for e in t["events"])
    assert t["problems"] == []
    assert t["durations"] == {"time_to_detect_s": 180.0, "time_to_review_s": 240.0, "time_to_fix_s": 600.0,
                              "time_to_recover_s": 960.0}
    assert t["events"][0]["at"] == "2026-11-12T10:00:00+00:00"


def test_timeline_reports_missing_unknown_and_out_of_order_stages():
    events = [e for e in full(alert=1) if e["stage"] != "resolved"] + [ev("lunch", 4)]
    t = loop.timeline(events)
    assert t["problems"] == ["unknown stage: lunch", "missing stage: resolved", "alert before judge"]
    assert t["durations"]["time_to_recover_s"] is None and t["durations"]["time_to_detect_s"] == -60.0


def test_timeline_tolerates_clock_skew_and_accepts_datetimes():
    events = full()
    events[3] = {"stage": "alert", "at": datetime(2026, 11, 12, 10, 2, 55)}  # naive = UTC, 5 s before the judge event
    t = loop.timeline(events)  # 5 s < skew_s (two clocks)
    assert "alert before judge" not in t["problems"] and t["problems"] == []
    assert any(e["at"] == "2026-11-12T10:02:55+00:00" for e in t["events"])
    assert loop.timeline(events, skew_s=1)["problems"] == ["alert before judge"]
    late = datetime(2026, 11, 12, 10, 2, tzinfo=timezone(timedelta(hours=1)))  # 09:02 UTC
    assert loop.timeline([{"stage": "deploy", "at": late}])["events"][0]["at"] == "2026-11-12T09:02:00+00:00"
