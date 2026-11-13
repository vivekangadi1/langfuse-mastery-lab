"""Module 03: pure cost functions. You implement these; `pytest langfuse_mastery/tests/test_cost.py` checks them.

No Langfuse imports here. This is the arithmetic Langfuse runs at ingestion, plus the budget logic you would run
on top of its numbers, so you can reconcile a generation by hand and reuse the checks in CI (module 10) and alerts
(module 12).

Shapes, as Langfuse uses them:
    usage  = {"input": 197, "input_cache_read": 4224, "output": 5, "total": 4426}   # a generation's usage_details
    prices = {"input": 1.75e-06, "input_cache_read": 1.75e-07, "output": 1.4e-05}   # USD per token, one per usage type
"""
from __future__ import annotations


def per_token(prices_per_million: dict[str, float]) -> dict[str, float]:
    """Convert vendor prices (USD per 1M tokens, as pricing pages quote them) to Langfuse prices (USD per token).

    per_token({"input": 0.40, "output": 1.60}) == {"input": 4e-07, "output": 1.6e-06}
    """
    raise NotImplementedError("TODO 1: implement per_token")


def cost_from_usage(usage: dict[str, int], prices: dict[str, float]) -> float:
    """USD cost of one generation, the way Langfuse computes it: the sum of usage[type] * prices[type] over the
    usage types that have a price.

    - A usage type without a price costs 0 (that is how cached tokens end up free if you forget their price).
    - "total" is a sum of the other types, so skip it, unless "total" is the ONLY price
      (a flat per-token model): then the cost is usage["total"] * prices["total"].
    - Missing or None counts are 0.
    """
    raise NotImplementedError("TODO 1: implement cost_from_usage")


def over_budget(costs: dict[str, float | None], budget: float) -> list[str]:
    """Ids whose cost is strictly greater than `budget`, most expensive first. A None cost counts as 0."""
    raise NotImplementedError("TODO 1: implement over_budget")


def cost_spikes(costs: dict[str, float | None], *, factor: float = 5.0) -> list[str]:
    """Ids whose cost is >= factor x the MEDIAN cost of all ids, most expensive first.

    The median, not the mean: one huge spike drags the mean up and hides itself.
    - A None cost counts as 0.
    - If the median is 0, every positive cost is a spike.
    - No costs: [].
    """
    raise NotImplementedError("TODO 1: implement cost_spikes")
