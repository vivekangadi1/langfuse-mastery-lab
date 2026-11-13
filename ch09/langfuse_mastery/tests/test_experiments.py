import hashlib
import hmac
import json
from types import SimpleNamespace as NS

import pytest

from langfuse_mastery import experiments as ex

MSG = "I cannot log in after changing my password."


def label(value, expected="account_security", ts="2026-11-12T06:00:00Z", **meta):
    return {"name": "handoff_correct", "value": value, "dataType": "BOOLEAN", "source": "API", "timestamp": ts,
            "metadata": {"evaluator": "code:v1", "expected": expected, **meta}}


def correction(text, ts="2026-11-12T06:00:01Z"):
    return {"name": "output", "value": text, "dataType": "CORRECTION", "source": "API", "timestamp": ts, "metadata": {}}


# ---------------------------------------------------------------- item_from_trace
@pytest.mark.parametrize("trace_input", [MSG, json.dumps(MSG), {"message": MSG}, f"  {MSG}\n"])
def test_item_from_trace_accepts_input_shapes(trace_input):
    item = ex.item_from_trace(trace_input, [label(True)], case_id="c1")
    assert item["input"] == {"message": MSG}


def test_item_from_trace_failure_with_correction():
    item = ex.item_from_trace(MSG, [label(False), correction("routed to security")], case_id="c1")
    assert item == {"input": {"message": MSG},
                    "expected_output": {"handoff": "account_security", "reference_answer": "routed to security"},
                    "metadata": {"case_id": "c1", "source": "trace", "label": "confirmed_failure"}}


def test_item_from_trace_golden_without_correction():
    item = ex.item_from_trace(MSG, [label(True)], case_id="c2")
    assert item["metadata"]["label"] == "golden"
    assert item["expected_output"] == {"handoff": "account_security", "reference_answer": None}


@pytest.mark.parametrize("value", [False, 0, "false", "0", "FALSE"])
def test_item_from_trace_false_values(value):
    assert ex.item_from_trace(MSG, [label(value)], case_id="c")["metadata"]["label"] == "confirmed_failure"


def test_item_from_trace_uses_latest_label():
    scores = [label(True, expected="billing_specialist", ts="2026-11-12T06:00:00Z"),
              label(False, expected="account_security", ts="2026-11-12T06:05:00Z")]
    item = ex.item_from_trace(MSG, scores, case_id="c")
    assert item["expected_output"]["handoff"] == "account_security"
    assert item["metadata"]["label"] == "confirmed_failure"


def test_item_from_trace_latest_correction_wins():
    scores = [label(False), correction("old", ts="2026-11-12T06:00:01Z"), correction("new", ts="2026-11-12T06:09:00Z")]
    assert ex.item_from_trace(MSG, scores, case_id="c")["expected_output"]["reference_answer"] == "new"


def test_item_from_trace_human_annotation_confirms_failure():
    human = {"name": "human_routing_ok", "value": False, "source": "ANNOTATION", "dataType": "BOOLEAN",
             "timestamp": "2026-11-12T07:00:00Z", "metadata": {}}
    not_human = dict(human, source="API")
    assert ex.item_from_trace(MSG, [label(True), human], case_id="c")["metadata"]["label"] == "confirmed_failure"
    assert ex.item_from_trace(MSG, [label(True), not_human], case_id="c")["metadata"]["label"] == "golden"


def test_item_from_trace_unlabelled_is_none():
    assert ex.item_from_trace(MSG, [], case_id="c") is None
    no_expected = {"name": "handoff_correct", "value": True, "dataType": "BOOLEAN", "source": "API",
                   "timestamp": "2026-11-12T06:00:00Z", "metadata": {"evaluator": "code:v1"}}
    assert ex.item_from_trace(MSG, [no_expected, correction("x")], case_id="c") is None
    other = dict(label(False), name="kb_hit")
    assert ex.item_from_trace(MSG, [other], case_id="c") is None


def test_item_from_trace_tolerates_none_metadata():
    s = dict(label(True), metadata=None)
    assert ex.item_from_trace(MSG, [s, label(True)], case_id="c")["metadata"]["label"] == "golden"


# ---------------------------------------------------------------- curated_item / changed
def test_curated_item():
    assert ex.curated_item("inv", " Invoice please ", "billing_specialist", "ok") == {
        "input": {"message": "Invoice please"},
        "expected_output": {"handoff": "billing_specialist", "reference_answer": "ok"},
        "metadata": {"case_id": "inv", "source": "curated", "label": "curated"}}
    assert ex.curated_item("x", "m", "none")["expected_output"]["reference_answer"] is None


def stored(wanted, status="ACTIVE", **extra):
    return {"id": "di-1", "input": wanted["input"], "expectedOutput": wanted["expected_output"],
            "metadata": wanted["metadata"], "status": status, "version": "2026-11-12T07:00:00Z",
            "sourceTraceId": "t-old", **extra}


def test_changed():
    wanted = ex_item = {"input": {"message": "m"}, "expected_output": {"handoff": "none", "reference_answer": None},
                        "metadata": {"case_id": "c", "source": "curated", "label": "curated"}}
    assert ex.changed(None, wanted) is True
    assert ex.changed(stored(ex_item), wanted) is False
    assert ex.changed(stored(ex_item, sourceTraceId="t-new"), wanted) is False
    assert ex.changed(stored(ex_item, status=NS(value="ACTIVE")), wanted) is False
    assert ex.changed(stored(ex_item), {**wanted, "expected_output": {"handoff": "billing_specialist",
                                                                        "reference_answer": None}}) is True
    assert ex.changed(stored(ex_item), {**wanted, "metadata": {**wanted["metadata"], "label": "golden"}}) is True
    assert ex.changed(stored(ex_item), {**wanted, "status": "ARCHIVED"}) is True
    assert ex.changed(stored(ex_item, status="ARCHIVED"), wanted) is True


# ---------------------------------------------------------------- concise / quality
@pytest.mark.parametrize("answer,n,want", [
    ("Order 8129. Refund in 5-7 days!", 3, True),
    ("One. Two. Three.", 3, True),
    ("One. Two. Three. Four.", 3, False),
    ("Really?! Yes... Done", 2, False),
    ("v2.1 is out", 1, True),
    ("No terminator here", 1, True),
    ("", 3, False),
    (None, 3, False),
])
def test_concise(answer, n, want):
    assert ex.concise(answer, n) is want


def test_quality_weighted_mean():
    evals = [NS(name="handoff_correct", value=True), NS(name="concise", value=False), NS(name="other", value=1.0)]
    assert ex.quality(evals, {"handoff_correct": 0.75, "concise": 0.25}) == pytest.approx(0.75)
    assert ex.quality([{"name": "handoff_correct", "value": 1.0}], {"handoff_correct": 1, "concise": 1}) == 0.5
    assert ex.quality([], {"handoff_correct": 1}) == 0.0
    with pytest.raises(ValueError):
        ex.quality(evals, {"handoff_correct": 0})


# ---------------------------------------------------------------- compare_runs / gate
def test_compare_runs():
    base = {"a": True, "b": True, "c": False, "d": True, "e": None}
    cand = {"a": True, "b": False, "c": True, "d": False, "e": True, "f": True}
    got = ex.compare_runs(base, cand)
    assert got["n"] == 4
    assert got["baseline_rate"] == pytest.approx(0.75)
    assert got["candidate_rate"] == pytest.approx(0.5)
    assert got["delta"] == pytest.approx(-0.25)
    assert got["regressed"] == ["b", "d"]
    assert got["improved"] == ["c"]
    assert got["missing"] == ["e", "f"]


def test_compare_runs_empty():
    got = ex.compare_runs({}, {"x": None})
    assert got == {"n": 0, "baseline_rate": None, "candidate_rate": None, "delta": None, "regressed": [],
                   "improved": [], "missing": ["x"]}


def test_gate():
    ok = ex.compare_runs({"a": True, "b": False}, {"a": True, "b": True})
    assert ex.gate(ok) == ("ship", [])
    bad = ex.compare_runs({"a": True, "b": True, "c": True}, {"a": False, "b": True, "c": True})
    decision, reasons = ex.gate(bad)
    assert decision == "block" and len(reasons) == 2
    assert "1" in reasons[0] and "0.33" in reasons[1]
    assert ex.gate(bad, max_regressions=1, max_drop=0.5) == ("ship", [])
    decision, reasons = ex.gate(bad, max_regressions=1)
    assert decision == "block" and len(reasons) == 1
    assert ex.gate(ex.compare_runs({}, {}))[0] == "block"


# ---------------------------------------------------------------- remote trigger
def sig(body, secret, t):
    text = body.decode() if isinstance(body, bytes) else body
    return f"t={t},v1=" + hmac.new(secret.encode(), f"{t}.{text}".encode(), hashlib.sha256).hexdigest()


BODY = '{"projectId":"p","datasetId":"d","datasetName":"ds","payload":"{\\"label\\":\\"staging\\"}"}'


def test_verify_signature():
    good = sig(BODY, "lf-whsec_s", 1000)
    assert ex.verify_signature(BODY, good, "lf-whsec_s", now=1000) is True
    assert ex.verify_signature(BODY.encode(), good, "lf-whsec_s", now=1200) is True
    assert ex.verify_signature(BODY, good, "lf-whsec_s", now=1000 + 301) is False  # replay
    assert ex.verify_signature(BODY, good, "other-secret", now=1000) is False
    assert ex.verify_signature(BODY + " ", good, "lf-whsec_s", now=1000) is False
    for bad in (None, "", "v1=abc", "t=1000", "t=x,v1=abc", good.replace("v1=", "v0=")):
        assert ex.verify_signature(BODY, bad, "lf-whsec_s", now=1000) is False


def test_trigger_request():
    assert ex.trigger_request(BODY) == {"project_id": "p", "dataset_id": "d", "dataset_name": "ds",
                                        "payload": {"label": "staging"}}
    as_dict = json.dumps({"projectId": "p", "datasetId": "d", "datasetName": "ds", "payload": {"limit": 2}})
    assert ex.trigger_request(as_dict.encode())["payload"] == {"limit": 2}
    for payload in (None, "", "not json", "[1, 2]"):
        body = json.dumps({"projectId": "p", "datasetId": "d", "datasetName": "ds", "payload": payload})
        assert ex.trigger_request(body)["payload"] == {}
    with pytest.raises(ValueError):
        ex.trigger_request("[]")
    with pytest.raises(ValueError):
        ex.trigger_request('{"projectId": "p"}')


def test_item_from_trace_custom_label_score_name():
    prefixed = dict(label(False), name="team_handoff_correct")
    assert ex.item_from_trace(MSG, [prefixed], case_id="c") is None
    item = ex.item_from_trace(MSG, [prefixed], case_id="c", label_score="team_handoff_correct")
    assert item["metadata"]["label"] == "confirmed_failure"
