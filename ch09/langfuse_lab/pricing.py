"""Module 03: list prices for the Azure deployments the course knows, and budgets that follow the agent's price.

No Langfuse code: the learner turns `definitions(settings)` into Langfuse model definitions (module 03 TODO 2).
Prices are USD per 1M tokens (what vendor pages quote), list prices at the time of writing; in production, use the
prices from your Azure agreement. A deployment is looked up by its exact NAME (case-insensitive), like
`config.takes_temperature`: name each deployment after its model, or add a row here.
"""
from __future__ import annotations

LIST_PRICES_PER_M = {
    "gpt-4.1-mini": {"input": 0.40, "input_cache_read": 0.10, "output": 1.60},
    # GPT-5 family: reasoning tokens are billed as output tokens. Langfuse's LangChain handler moves them out of
    # `output` into their own usage type, `output_reasoning`, so without a price of their own they would cost 0.
    "gpt-5.2-chat": {"input": 1.75, "input_cache_read": 0.175, "output": 14.00, "output_reasoning": 14.00},
    "gpt-5.4": {"input": 2.50, "input_cache_read": 0.25, "output": 15.00, "output_reasoning": 15.00},
}
REFERENCE_AGENT = "gpt-4.1-mini"                  # the course's budgets were set for this agent model
REFERENCE_CALL = {"input": 1000, "output": 100}   # a typical agent LLM call: prompt + tools in, a short reply out


def list_prices(deployment: str) -> dict[str, float]:
    """USD per 1M tokens per usage type for `deployment` (a copy)."""
    prices = LIST_PRICES_PER_M.get((deployment or "").strip().lower())
    if prices is None:
        raise KeyError(f"no list prices for deployment {deployment!r}: add its model's USD-per-1M-token prices to "
                       f"LIST_PRICES_PER_M in langfuse_lab/pricing.py (known: {sorted(LIST_PRICES_PER_M)})")
    return dict(prices)


def definition_name(deployment: str) -> str:
    """The custom model definition's `model_name` for a deployment: "azure-gpt-4.1-mini"."""
    return f"azure-{deployment}"


def definitions(settings) -> list[dict]:
    """Module 03's TODO 2 table: one custom model definition per DISTINCT deployment.

    Two deployments: rows "agent" and "expensive" (azure-gpt-4.1-mini, azure-gpt-5.2-chat). One deployment for
    everything (`settings.single_model`): a single "agent" row; it is also the judge and what cost_spike runs on.
    Each row: {"label", "deployment", "model_name", "prices_per_m"}."""
    labels = [("agent", settings.agent_deployment)]
    if not settings.single_model:
        labels.append(("expensive", settings.expensive_deployment))
    return [{"label": label, "deployment": d, "model_name": definition_name(d), "prices_per_m": list_prices(d)}
            for label, d in labels]


def call_cost(deployment: str, usage: dict | None = None) -> float:
    """USD for one call with `usage` tokens per type (default REFERENCE_CALL) at `deployment`'s list prices."""
    prices = list_prices(deployment)
    return sum(n * prices.get(kind, 0.0) / 1e6 for kind, n in (usage or REFERENCE_CALL).items())


def scaled_budget(usd: float, deployment: str) -> float:
    """A USD budget that was set for REFERENCE_AGENT, scaled by what a typical call costs on `deployment`
    (2 significant digits): 0.01 stays 0.01 on gpt-4.1-mini and becomes 0.071 on gpt-5.4. A per-conversation budget
    written for a cheap model would flag every clean conversation of a 7x pricier one."""
    return float(f"{usd * call_cost(deployment) / call_cost(REFERENCE_AGENT):.2g}")
