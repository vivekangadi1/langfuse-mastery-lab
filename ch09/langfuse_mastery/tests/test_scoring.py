"""Module 06 TODO 2. These fail until you implement langfuse_mastery/scoring.py.

Run from ch09/:   .venv-langfuse/Scripts/python.exe -m pytest langfuse_mastery/tests -q
"""
import pytest

from langfuse_mastery.scoring import handoff_correct, kb_hit, validate_score

KB_HIT = {"name": "kb_hit", "dataType": "NUMERIC", "minValue": 0.0, "maxValue": 1.0, "categories": None}
CSAT = {"name": "csat", "dataType": "NUMERIC", "minValue": 1.0, "maxValue": 5.0, "categories": None}
OPEN = {"name": "latency_ms", "dataType": "NUMERIC", "minValue": 0.0, "maxValue": None, "categories": None}
BOOL = {"name": "handoff_correct", "dataType": "BOOLEAN", "minValue": None, "maxValue": None, "categories": None}
FEEDBACK = {"name": "user_feedback", "dataType": "CATEGORICAL", "minValue": None, "maxValue": None,
            "categories": [{"label": "thumbs_up", "value": 1.0}, {"label": "thumbs_down", "value": 0.0}]}


@pytest.mark.parametrize("config, value", [
    (KB_HIT, 0.0), (KB_HIT, 1.0), (KB_HIT, 0.5),   # bounds are inclusive
    (CSAT, 1), (CSAT, 5),                           # ints are numbers too
    (OPEN, 10_000),                                 # maxValue None = no upper bound
    (BOOL, True), (BOOL, False), (BOOL, 0), (BOOL, 1),
    (FEEDBACK, "thumbs_up"), (FEEDBACK, "thumbs_down"),
])
def test_valid_scores(config, value):
    ok, reason = validate_score(config, value)
    assert ok and reason == "", reason


@pytest.mark.parametrize("config, value", [
    (KB_HIT, 7),            # the module demo: silently dropped by the server
    (KB_HIT, -0.1),
    (CSAT, 0),
    (OPEN, -1),
    (KB_HIT, "0.5"),        # a string is not a number
    (KB_HIT, True),         # bool is an int in Python, but not a NUMERIC score
    (BOOL, 2),
    (BOOL, "true"),
    (FEEDBACK, "meh"),      # unknown category: also silently dropped
    (FEEDBACK, 1.0),        # send the label, not the value
])
def test_invalid_scores_explain_why(config, value):
    ok, reason = validate_score(config, value)
    assert not ok
    assert reason, "say why, so the error is actionable"


def test_reason_names_the_problem():
    _, reason = validate_score(KB_HIT, 7)
    assert "7" in reason and "1" in reason  # mention the value and the bound it broke
    _, reason = validate_score(FEEDBACK, "meh")
    assert "meh" in reason


@pytest.mark.parametrize("actual, expected, ok", [
    ("billing_specialist", "billing_specialist", True),
    ("none", "none", True),
    ("billing_specialist", "account_security", False),  # the planted wrong_handoff
    ("none", "billing_specialist", False),               # missed handoff
    ("billing_specialist", "none", False),               # unnecessary handoff
])
def test_handoff_correct(actual, expected, ok):
    assert handoff_correct(actual, expected) is ok


@pytest.mark.parametrize("returned, expected, score", [
    (["kb_refunds", "kb_returns"], "kb_refunds", 1.0),
    (["kb_returns"], "kb_refunds", 0.0),
    ([], "kb_refunds", 0.0),
    (["kb_refunds"], None, None),  # case expects no article: no score at all
])
def test_kb_hit(returned, expected, score):
    assert kb_hit(returned, expected) == score
