"""Module 07 TODO 1: tests for langfuse_mastery/judging.py (they fail until you implement it)."""
import pytest

from langfuse_mastery import judging

ROUTING = ("Policy...\nCustomer message: {{input}}\nTeam the agent handed off to: {{ handoff }}\n"
           "Agent's reply: {{answer}}\nRepeat: {{input}}")


@pytest.mark.parametrize("template, expected", [
    (ROUTING, ["input", "handoff", "answer"]),
    ("no variables here", []),
    ("{input} and {{}} and {{ two words }}", []),
    ("{{a}}{{b_2}}{{a}}", ["a", "b_2"]),
])
def test_prompt_variables(template, expected):
    assert judging.prompt_variables(template) == expected


def test_variable_mapping_plain_and_jsonpath():
    got = judging.variable_mapping({"input": "input", "handoff": "metadata:$.handoff_target", "answer": "output"})
    assert got == [{"variable": "input", "source": "input"},
                   {"variable": "handoff", "source": "metadata", "jsonPath": "$.handoff_target"},
                   {"variable": "answer", "source": "output"}]


def test_variable_mapping_jsonpath_with_colon_inside():
    assert judging.variable_mapping({"x": "metadata:$['a:b']"}) == [
        {"variable": "x", "source": "metadata", "jsonPath": "$['a:b']"}]


@pytest.mark.parametrize("spec", [{"x": "trace"}, {"x": "metadata:handoff_target"}, {"x": "expected_output"}])
def test_variable_mapping_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        judging.variable_mapping(spec)


def test_check_mapping_ok():
    judging.check_mapping(ROUTING, judging.variable_mapping(
        {"input": "input", "handoff": "metadata:$.handoff_target", "answer": "output"}))


def test_check_mapping_missing_and_extra():
    with pytest.raises(ValueError) as exc:
        judging.check_mapping(ROUTING, [{"variable": "input", "source": "input"}, {"variable": "foo", "source": "output"}])
    msg = str(exc.value)
    assert "missing" in msg and "handoff" in msg and "answer" in msg and "extra" in msg and "foo" in msg


def test_check_mapping_duplicate():
    mapping = judging.variable_mapping({"input": "input", "handoff": "metadata:$.h", "answer": "output"})
    with pytest.raises(ValueError) as exc:
        judging.check_mapping(ROUTING, mapping + [{"variable": "input", "source": "output"}])
    assert "duplicate" in str(exc.value) and "input" in str(exc.value)


def test_rule_filters_exact_shape():
    assert judging.rule_filters("live-eval", "support-request") == [
        {"type": "arrayOptions", "column": "tags", "operator": "any of", "value": ["live-eval"]},
        {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
        {"type": "stringOptions", "column": "name", "operator": "any of", "value": ["support-request"]},
    ]


def test_rule_filters_never_use_trace_name():
    assert all(f["column"] != "traceName" for f in judging.rule_filters("t", "r"))


@pytest.mark.parametrize("value, expected", [
    (True, True), (False, False), (1, True), (0, False), (1.0, True), (0.0, False),
    ("true", True), (" FALSE ", False), ("1", True), ("0", False),
    (None, None), (0.5, None), ("maybe", None), (2, None),
])
def test_as_verdict(value, expected):
    assert judging.as_verdict(value) is expected


def test_agreement_counts():
    pairs = [(True, True), (False, False), (False, True), (True, False), (None, True), (True, None)]
    assert judging.agreement(pairs) == {"n": 4, "agree": 2, "rate": 0.5, "judge_flagged": 2, "code_flagged": 2,
                                        "both_flagged": 1, "judge_missed": 1, "judge_false_alarm": 1}


def test_agreement_empty():
    got = judging.agreement([(None, None)])
    assert got["n"] == 0 and got["rate"] is None and got["agree"] == 0
