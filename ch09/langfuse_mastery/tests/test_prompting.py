"""Module 04 TODO 1. These fail until you implement langfuse_mastery/prompting.py.

Run from ch09/:   .venv-langfuse/Scripts/python.exe -m pytest langfuse_mastery/tests/test_prompting.py -q
"""
import pytest

from langfuse_lab.prompts import PROMPT_CONFIG, PROMPT_VARIABLES, SYSTEM_PROMPT, SYSTEM_PROMPT_TEMPLATE
from langfuse_mastery.prompting import compile_strict, llm_params, template_variables


@pytest.mark.parametrize("template, expected", [
    ("no variables here", []),
    ("{{a}}", ["a"]),
    ("Hi {{ name }}, {{x}} and {{name}} again", ["name", "x"]),
    ("{{b}} {{a}} {{b}}", ["b", "a"]),
    ("broken {{ open and never closed", []),
    ("json is fine: {\"k\": 1}", []),
])
def test_template_variables(template, expected):
    assert template_variables(template) == expected


def test_template_variables_of_the_agent_prompt():
    assert template_variables(SYSTEM_PROMPT_TEMPLATE) == ["company", "max_sentences"]


def test_compile_strict_renders_the_agent_prompt():
    assert compile_strict(SYSTEM_PROMPT_TEMPLATE, PROMPT_VARIABLES) == SYSTEM_PROMPT


@pytest.mark.parametrize("template, variables, expected", [
    ("Hi {{ name }}!", {"name": "Ann"}, "Hi Ann!"),
    ("{{n}} + {{n}}", {"n": 2}, "2 + 2"),
    ("plain", {}, "plain"),
    ("plain", {"unused": 1}, "plain"),
    ("{{a}}{{b}}", {"a": "x", "b": "y", "c": "z"}, "xy"),
])
def test_compile_strict(template, variables, expected):
    assert compile_strict(template, variables) == expected


def test_compile_strict_names_every_missing_variable():
    with pytest.raises(ValueError, match="company, max_sentences"):
        compile_strict(SYSTEM_PROMPT_TEMPLATE, {})
    with pytest.raises(ValueError, match="max_sentences"):
        compile_strict(SYSTEM_PROMPT_TEMPLATE, {"company": "Acme"})


@pytest.mark.parametrize("config, expected", [
    (None, {}),
    ({}, {}),
    (PROMPT_CONFIG, {"temperature": 0, "max_tokens": 400}),
    ({"temperature": 0.7, "top_p": 1, "owner": "support", "model": "gpt-4.1-mini"}, {"temperature": 0.7, "top_p": 1}),
    ({"temperature": 2, "max_tokens": 1}, {"temperature": 2, "max_tokens": 1}),
])
def test_llm_params(config, expected):
    assert llm_params(config) == expected


@pytest.mark.parametrize("config, key", [
    ({"temperature": 2.5}, "temperature"),
    ({"temperature": -0.1}, "temperature"),
    ({"temperature": "0"}, "temperature"),
    ({"temperature": True}, "temperature"),
    ({"max_tokens": 0}, "max_tokens"),
    ({"max_tokens": 400.5}, "max_tokens"),
    ({"max_tokens": True}, "max_tokens"),
    ({"top_p": 1.5}, "top_p"),
])
def test_llm_params_rejects_bad_values(config, key):
    with pytest.raises(ValueError, match=key):
        llm_params(config)
