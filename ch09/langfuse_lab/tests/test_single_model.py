"""One Azure deployment for everything (e.g. a single gpt-5.4 as agent AND judge): config, prompts, agent, pricing.

The two-deployment course (gpt-4.1-mini agent + gpt-5.2-chat judge/expensive) must behave exactly as before."""
import re
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage

from langfuse_lab import pricing
from langfuse_lab.config import Settings, sent_params
from langfuse_lab.failures import FailureConfig
from langfuse_lab.prompts import (COST_SPIKE_PADDING, SINGLE_MODEL_SPIKE_PADDING, SYSTEM_PROMPT, request_time_header,
                                  system_prompt)
from langfuse_lab.tests.fakes import tool_call
from langfuse_lab.tests.test_agent import RecordingModel

TWO = Settings("https://x.openai.azure.com/", "k", "2025-04-01-preview", "gpt-4.1-mini", "gpt-5.2-chat",
               "http://localhost:3000")
ONE = Settings("https://x.openai.azure.com/", "k", "2025-04-01-preview", "gpt-5.4", "gpt-5.4", "http://localhost:3000")
SPIKE = FailureConfig.from_names(["cost_spike"])


@pytest.fixture(autouse=True)
def _no_temperature_override(monkeypatch):
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)


# ---------------------------------------------------------------- config
def test_single_model_flag():
    assert ONE.single_model is True and TWO.single_model is False


def test_sent_params_unchanged_for_a_deployment_that_takes_temperature():
    cfg = {"temperature": 0, "max_tokens": 400}
    assert sent_params(cfg, "gpt-4.1-mini") == cfg
    assert sent_params(None, "gpt-4.1-mini") == {}


def test_sent_params_on_gpt5_drop_temperature_and_rename_max_tokens():
    assert sent_params({"temperature": 0, "max_tokens": 400}, "gpt-5.4") == {"max_completion_tokens": 400}
    assert sent_params({"temperature": 0}, "gpt-5.2-chat") == {}


def test_sent_params_follow_the_env_override(monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURE", "off")
    assert sent_params({"temperature": 0, "max_tokens": 400}, "my-deployment") == {"max_completion_tokens": 400}


def test_sent_params_match_what_the_agent_binds():
    """verify (module 04) and the agent must agree on what a GPT-5 deployment is sent."""
    from langfuse_lab.agent import SupportAgent, turn_params

    cfg = {"temperature": 0, "max_tokens": 400}
    for s in (ONE, TWO):
        agent = SupportAgent(s)
        assert (turn_params(cfg, agent.drop_params, agent.rename_params) or {}) == sent_params(cfg, s.agent_deployment)


# ---------------------------------------------------------------- prompts
def test_two_deployment_cost_spike_prompt_is_unchanged():
    assert system_prompt(SPIKE) == SYSTEM_PROMPT + COST_SPIKE_PADDING
    assert system_prompt(FailureConfig(), single_model=True) == SYSTEM_PROMPT


def test_single_model_cost_spike_prompt_is_the_bigger_padding():
    p = system_prompt(SPIKE, single_model=True)
    assert p == SYSTEM_PROMPT + SINGLE_MODEL_SPIKE_PADDING
    assert len(SINGLE_MODEL_SPIKE_PADDING) > 2 * len(COST_SPIKE_PADDING)


def test_request_time_header_is_one_line_with_microseconds():
    h = request_time_header(datetime(2026, 11, 13, 7, 0, 0, 123456, tzinfo=timezone.utc))
    assert h == "Request time: 2026-11-13T07:00:00.123456+00:00\n"
    assert re.fullmatch(r"Request time: \S+\n", request_time_header())


# ---------------------------------------------------------------- agent
def _agent(settings, failures, monkeypatch, *replies):
    """SupportAgent built like the notebooks build it (llm=None), with make_llm swapped for a recording fake."""
    from langfuse_lab import agent as agent_mod

    llm, made = RecordingModel(*replies), []

    def fake_make_llm(s, *, expensive=False):
        made.append(s.expensive_deployment if expensive else s.agent_deployment)
        return llm

    monkeypatch.setattr(agent_mod, "make_llm", fake_make_llm)
    return agent_mod.SupportAgent(settings, failures=failures), llm, made


def test_two_deployment_cost_spike_switches_model_and_keeps_the_cacheable_prompt(monkeypatch):
    agent, llm, made = _agent(TWO, SPIKE, monkeypatch,
                              tool_call("create_handoff", {"target": "account_security", "reason": "login"}),
                              AIMessage(content="Done."))
    agent.invoke("I can't log in")
    assert made == ["gpt-5.2-chat"] and agent.cache_busting is False
    assert [p for p, _ in llm.calls] == [SYSTEM_PROMPT + COST_SPIKE_PADDING] * 2


def test_single_model_cost_spike_busts_the_prompt_cache_on_every_call(monkeypatch):
    agent, llm, made = _agent(ONE, SPIKE, monkeypatch,
                              tool_call("create_handoff", {"target": "account_security", "reason": "login"}),
                              AIMessage(content="Done."))
    agent.invoke("I can't log in")
    assert made == ["gpt-5.4"] and agent.cache_busting is True
    prompts = [p for p, _ in llm.calls]
    assert len(prompts) == 2 and prompts[0] != prompts[1]  # a new first line on every LLM call
    for p in prompts:
        header, rest = p.split("\n", 1)
        assert header.startswith("Request time: ") and rest == SYSTEM_PROMPT + SINGLE_MODEL_SPIKE_PADDING


def test_single_model_clean_agent_and_per_turn_prompts_get_no_header(monkeypatch):
    agent, llm, _ = _agent(ONE, FailureConfig(), monkeypatch, AIMessage(content="hi"), AIMessage(content="hi"))
    agent.invoke("hello")
    assert agent.cache_busting is False and llm.calls[0][0] == SYSTEM_PROMPT
    spike, llm2, _ = _agent(ONE, SPIKE, monkeypatch, AIMessage(content="hi"))
    spike.invoke("hello", system_prompt="SERVED")
    assert llm2.calls[0][0] == "SERVED"


def test_injected_llm_never_busts_the_cache():
    from langfuse_lab.agent import SupportAgent

    agent = SupportAgent(llm=RecordingModel(), failures=SPIKE)
    assert agent.cache_busting is False and agent.system_prompt == SYSTEM_PROMPT + COST_SPIKE_PADDING


# ---------------------------------------------------------------- pricing
def test_list_prices_known_and_unknown():
    assert pricing.list_prices("GPT-4.1-mini") == {"input": 0.40, "input_cache_read": 0.10, "output": 1.60}
    assert pricing.list_prices("gpt-5.4")["output_reasoning"] == pricing.list_prices("gpt-5.4")["output"]
    with pytest.raises(KeyError, match="LIST_PRICES_PER_M"):
        pricing.list_prices("gpt-5.4-mini")  # exact names only: a mini is not priced like its big sibling


def test_definitions_two_deployments_are_the_existing_pair():
    from langfuse_lab.verify import MODEL_DEFS

    rows = pricing.definitions(TWO)
    assert [r["label"] for r in rows] == ["agent", "expensive"]
    assert tuple(r["model_name"] for r in rows) == MODEL_DEFS
    assert rows[1]["prices_per_m"]["output"] == 14.00


def test_definitions_single_deployment_is_one_row():
    rows = pricing.definitions(ONE)
    assert [(r["label"], r["deployment"], r["model_name"]) for r in rows] == [("agent", "gpt-5.4", "azure-gpt-5.4")]


def test_scaled_budget_keeps_the_reference_and_scales_pricier_models():
    assert pricing.scaled_budget(0.01, "gpt-4.1-mini") == 0.01
    assert pricing.scaled_budget(0.01, "gpt-5.4") == 0.071
    assert pricing.scaled_budget(0.01, "gpt-5.2-chat") == 0.056


@pytest.mark.parametrize("deployment", ["gpt-5.2-chat", "gpt-5.4"])
def test_single_model_spike_is_visible_at_list_prices(deployment):
    """Design check for module 03's '>= 5x the clean median trace' on ONE model, worst case: a spike turn of ONE
    uncached call vs a clean turn of THREE calls. Live on gpt-5.2-chat: clean calls ~400-600 input / ~20-95 output
    tokens; 1000 repetitions of the appendix sentence were 11,000 input tokens (11 per repetition)."""
    padding_tokens = SINGLE_MODEL_SPIKE_PADDING.count("Be courteous") * 11
    call = {"input": 550, "output": 50}
    clean_turn = 3 * pricing.call_cost(deployment, call)
    spiked = pricing.call_cost(deployment, {"input": call["input"] + padding_tokens, "output": call["output"]})
    assert spiked >= 5 * clean_turn
    cached = pricing.call_cost(deployment, {**call, "input_cache_read": padding_tokens})
    assert 3 * cached < 5 * clean_turn  # why the cache must be defeated: even 3 cached spike calls would not stand out
    smaller = pricing.call_cost(deployment, {"input": call["input"] + 1000 * 11, "output": call["output"]})
    assert smaller < 5 * clean_turn  # why 1000 repetitions (verified live) were not enough
