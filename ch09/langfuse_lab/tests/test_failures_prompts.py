import pytest

from langfuse_lab.failures import FailureConfig
from langfuse_lab.prompts import SYSTEM_PROMPT, system_prompt


def test_from_names():
    f = FailureConfig.from_names(["slow_tool", "pii"])
    assert f.slow_tool_seconds > 0 and f.pii and not f.wrong_handoff


def test_unknown_failure_rejected():
    with pytest.raises(ValueError, match="bogus"):
        FailureConfig.from_names(["bogus"])


def test_wrong_handoff_prompt_routes_login_to_billing():
    p = system_prompt(FailureConfig(wrong_handoff=True))
    assert p != SYSTEM_PROMPT and "create_handoff to billing_specialist" in p
    login_line = next(line for line in p.splitlines() if line.startswith("- login"))
    assert "account_security" not in login_line
    assert "- double charge, refund, invoice -> billing_specialist" in p


def test_login_rule_always_hands_off_to_account_security():
    assert "ALWAYS create_handoff to account_security" in SYSTEM_PROMPT


def test_from_names_accepts_bare_string():
    assert FailureConfig.from_names("pii").pii is True


def test_cost_spike_pads_prompt():
    assert len(system_prompt(FailureConfig(cost_spike=True))) > 10 * len(SYSTEM_PROMPT)


def test_templates_render_to_the_unchanged_prompts():
    from langfuse_lab.prompts import (PROMPT_VARIABLES, SYSTEM_PROMPT_TEMPLATE, WRONG_HANDOFF_PROMPT,
                                      WRONG_HANDOFF_TEMPLATE, render)
    assert SYSTEM_PROMPT.startswith("You are Acme's customer support agent.\n")
    assert SYSTEM_PROMPT.endswith("Reply to the customer in at most 3 sentences.")
    assert "{{" not in SYSTEM_PROMPT and "{{" not in WRONG_HANDOFF_PROMPT
    assert render(SYSTEM_PROMPT_TEMPLATE, PROMPT_VARIABLES) == SYSTEM_PROMPT
    assert render(WRONG_HANDOFF_TEMPLATE, PROMPT_VARIABLES) == WRONG_HANDOFF_PROMPT
    assert "{{company}}" in SYSTEM_PROMPT_TEMPLATE and "{{max_sentences}}" in SYSTEM_PROMPT_TEMPLATE


def test_wrong_handoff_template_differs_only_in_the_login_route():
    from langfuse_lab.prompts import SYSTEM_PROMPT_TEMPLATE, WRONG_HANDOFF_TEMPLATE
    diff = [(a, b) for a, b in zip(SYSTEM_PROMPT_TEMPLATE.splitlines(), WRONG_HANDOFF_TEMPLATE.splitlines()) if a != b]
    assert len(diff) == 1 and diff[0][0].startswith("- login") and "billing_specialist" in diff[0][1]
