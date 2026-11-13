"""Module 08 TODO 1: tests for langfuse_mastery/review.py (they fail until you implement it)."""
import hashlib

import pytest

from langfuse_mastery import review


def score(name, value, source="EVAL", evaluator=None):
    return {"name": name, "value": value, "source": source,
            "metadata": {"evaluator": evaluator} if evaluator else {}}


# ---------------------------------------------------------------- is_judge_score
@pytest.mark.parametrize("s, expected", [
    (score("routing_judge", False), True),
    (score("routing_judge", False, "API", "stand-in:keyword-v1"), True),
    (score("handoff_correct", 1.0, "API", "code:v1"), False),
    (score("human_routing_ok", True, "ANNOTATION"), False),
    ({"name": "x", "value": 1, "source": "API", "metadata": None}, False),
    ({"name": "x", "value": 1, "source": "ANNOTATION", "metadata": {"evaluator": "stand-in:x"}}, False),
])
def test_is_judge_score(s, expected):
    assert review.is_judge_score(s) is expected


# ---------------------------------------------------------------- judge_flags
B, N = ("routing_judge",), ("helpfulness_judge",)


@pytest.mark.parametrize("value", [False, 0, 0.0, "false", " FALSE ", "0"])
def test_boolean_false_flags(value):
    assert review.judge_flags([score("routing_judge", value)], boolean_judges=B) == ["routing_judge=False"]


@pytest.mark.parametrize("value", [True, 1, "true", None])
def test_boolean_ok_does_not_flag(value):
    assert review.judge_flags([score("routing_judge", value)], boolean_judges=B) == []


def test_numeric_threshold_and_order():
    scores = [score("helpfulness_judge", 0.3), score("routing_judge", False)]
    assert review.judge_flags(scores, boolean_judges=B, numeric_judges=N, threshold=0.5) == [
        "routing_judge=False", "helpfulness_judge=0.3<0.5"]
    assert review.judge_flags([score("helpfulness_judge", 0.5)], numeric_judges=N, threshold=0.5) == []


def test_only_judge_scores_and_named_judges_count():
    scores = [score("routing_judge", False, "ANNOTATION"),            # a human, not a judge
              score("routing_judge", False, "API", "code:v1"),       # a code score with a judge's name
              score("other_judge", False)]                            # not asked about
    assert review.judge_flags(scores, boolean_judges=B, numeric_judges=N) == []


def test_one_reason_per_judge():
    scores = [score("routing_judge", False), score("routing_judge", "false", "API", "stand-in:keyword-v1")]
    assert review.judge_flags(scores, boolean_judges=B) == ["routing_judge=False"]


# ---------------------------------------------------------------- pick_controls
IDS = [f"t{i}" for i in range(10)]


def _rank(ids, salt=""):
    return sorted(ids, key=lambda t: hashlib.sha256((salt + t).encode()).hexdigest())


def test_pick_controls_size_and_order():
    got = review.pick_controls(IDS, rate=0.2)
    assert len(got) == 2 and set(got) == set(_rank(IDS)[:2])
    assert got == [t for t in IDS if t in got]  # original order


def test_pick_controls_min_n_and_cap():
    assert len(review.pick_controls(IDS[:3], rate=0.01)) == 1
    assert len(review.pick_controls(IDS[:3], rate=0.0, min_n=0)) == 0
    assert review.pick_controls(IDS[:2], rate=0.0, min_n=5) == IDS[:2]
    assert review.pick_controls([], rate=0.5) == []


def test_pick_controls_deterministic_salted_and_deduped():
    assert review.pick_controls(IDS, rate=0.3) == review.pick_controls(list(IDS), rate=0.3)
    assert set(review.pick_controls(IDS, rate=0.3, salt="week-46")) == set(_rank(IDS, "week-46")[:3])
    assert review.pick_controls(["a", "a", "b"], rate=1.0) == ["a", "b"]


# ---------------------------------------------------------------- items_to_add
def test_items_to_add():
    existing = [{"id": "i1", "objectId": "t1", "objectType": "TRACE", "status": "PENDING"},
                {"id": "i2", "objectId": "t2", "objectType": "TRACE", "status": "COMPLETED"},
                {"id": "i3", "objectId": "t3", "objectType": "OBSERVATION", "status": "PENDING"}]
    assert review.items_to_add(existing, ["t1", "t2", "t3", "t4", "t4"]) == ["t3", "t4"]
    assert review.items_to_add([], []) == []
    assert review.items_to_add(existing, ["t3"], object_type="OBSERVATION") == []


# ---------------------------------------------------------------- error_type
@pytest.mark.parametrize("expected, actual, label", [
    ("account_security", "billing_specialist", "wrong_team"),
    ("none", "billing_specialist", "unnecessary_handoff"),
    (None, "shipping_specialist", "unnecessary_handoff"),
    ("billing_specialist", "none", "missed_handoff"),
    ("billing_specialist", None, "missed_handoff"),
    ("billing_specialist", "billing_specialist", None),
    ("none", None, None),
    ("", "none", None),
])
def test_error_type(expected, actual, label):
    assert review.error_type(expected, actual) == label
    assert label is None or label in review.ERROR_TYPES


# ---------------------------------------------------------------- agreement
def test_agreement_queue_of_flags_plus_control():
    # judge flagged 2 (one false alarm), the control passed by both
    got = review.agreement([(False, True), (False, False), (True, True)])
    assert {k: got[k] for k in ("n", "agree", "judge_flagged", "human_flagged", "both_flagged",
                                "judge_missed", "judge_false_alarm")} == {
        "n": 3, "agree": 2, "judge_flagged": 2, "human_flagged": 1, "both_flagged": 1,
        "judge_missed": 0, "judge_false_alarm": 1}
    assert got["rate"] == pytest.approx(2 / 3)
    assert got["flag_precision"] == pytest.approx(0.5)
    assert got["kappa"] == pytest.approx(0.4)


def test_agreement_skips_none_and_counts_misses():
    got = review.agreement([(True, False), (None, True), (True, None), (True, True)])
    assert got["n"] == 2 and got["judge_missed"] == 1 and got["judge_flagged"] == 0
    assert got["flag_precision"] is None
    assert got["kappa"] == pytest.approx(0.0)


def test_agreement_empty_and_degenerate():
    empty = review.agreement([])
    assert empty["n"] == 0 and empty["rate"] is None and empty["kappa"] is None and empty["flag_precision"] is None
    same = review.agreement([(True, True), (True, True)])  # chance agreement is 1: kappa undefined
    assert same["rate"] == 1.0 and same["kappa"] is None


def test_agreement_perfect_disagreement():
    got = review.agreement([(True, False), (False, True)])
    assert got["agree"] == 0 and got["kappa"] == pytest.approx(-1.0)
