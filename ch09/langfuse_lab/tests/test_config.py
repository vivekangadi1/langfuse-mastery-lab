import pytest

from langfuse_lab.config import Settings, azure_endpoint_root


def test_endpoint_root_strips_path():
    assert azure_endpoint_root("https://x.openai.azure.com/openai/deployments/a/chat") == "https://x.openai.azure.com/"
    assert azure_endpoint_root("https://x.openai.azure.com") == "https://x.openai.azure.com/"


def test_from_env_reads_deployments(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "small")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "big")
    s = Settings.from_env(load=False)
    assert (s.agent_deployment, s.expensive_deployment) == ("small", "big")


def test_from_env_missing_key(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x")
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_API_KEY"):
        Settings.from_env(load=False)


@pytest.mark.parametrize("deployment, expected", [
    ("gpt-4.1-mini", True), ("my-chat-deployment", True),
    ("gpt-5.4", False), ("gpt-5.2-chat", False), ("GPT-5-mini", False), ("o4-mini", False), ("o3", False),
])
def test_takes_temperature_by_name(monkeypatch, deployment, expected):
    from langfuse_lab.config import takes_temperature

    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    assert takes_temperature(deployment) is expected


def test_takes_temperature_env_override(monkeypatch):
    from langfuse_lab.config import takes_temperature

    monkeypatch.setenv("LLM_TEMPERATURE", "off")
    assert takes_temperature("my-chat-deployment") is False
    monkeypatch.setenv("LLM_TEMPERATURE", "on")
    assert takes_temperature("gpt-5.4") is True


def test_single_gpt5_deployment_gets_no_temperature_anywhere(monkeypatch):
    """One gpt-5.4 deployment for everything (the office setup): no temperature on the model, none bound per turn."""
    from langfuse_lab.agent import SupportAgent, make_llm, turn_params

    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    s = Settings("https://x.openai.azure.com/", "k", "2025-04-01-preview", "gpt-5.4", "gpt-5.4", "http://localhost:3000")
    assert make_llm(s).temperature is None
    agent = SupportAgent(s)
    assert agent.drop_params == frozenset({"temperature"})
    assert turn_params({"temperature": 0, "max_tokens": 400}, agent.drop_params, agent.rename_params) == {
        "max_completion_tokens": 400}
    assert turn_params({"temperature": 0}, agent.drop_params, agent.rename_params) is None


def test_two_deployments_keep_their_temperature(monkeypatch):
    """The original setup is unchanged: gpt-4.1-mini at temperature 0, prompt-config params bound as they are."""
    from langfuse_lab.agent import SupportAgent, turn_params

    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    s = Settings("https://x.openai.azure.com/", "k", "2024-12-01-preview", "gpt-4.1-mini", "gpt-5.2-chat",
                 "http://localhost:3000")
    agent = SupportAgent(s)
    assert (agent.drop_params, agent.rename_params) == (frozenset(), {})
    assert turn_params({"temperature": 0, "max_tokens": 400}, agent.drop_params, agent.rename_params) == {
        "temperature": 0, "max_tokens": 400}


def test_make_llm_expensive_deployment_omits_temperature():
    """gpt-5.2-chat rejects temperature=0 (400, verified live); the agent deployment stays deterministic."""
    from langfuse_lab.agent import make_llm

    s = Settings("https://x.openai.azure.com/", "k", "2024-12-01-preview", "small", "big", "http://localhost:3000")
    cheap, expensive = make_llm(s), make_llm(s, expensive=True)
    assert (cheap.deployment_name, cheap.temperature) == ("small", 0)
    assert (expensive.deployment_name, expensive.temperature) == ("big", None)
