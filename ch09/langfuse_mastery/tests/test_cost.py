"""Module 03 TODO 1. These fail until you implement langfuse_mastery/cost.py.

Run from ch09/:   .venv-langfuse/Scripts/python.exe -m pytest langfuse_mastery/tests/test_cost.py -q
"""
import pytest

from langfuse_mastery.cost import cost_from_usage, cost_spikes, over_budget, per_token

GPT52 = {"input": 1.75e-06, "input_cache_read": 1.75e-07, "output": 1.4e-05}


def test_per_token_divides_by_a_million():
    got = per_token({"input": 0.40, "input_cache_read": 0.10, "output": 1.60})
    assert got == pytest.approx({"input": 4e-07, "input_cache_read": 1e-07, "output": 1.6e-06})


def test_per_token_keeps_keys_and_handles_empty():
    assert per_token({}) == {}
    assert set(per_token({"input": 1, "request": 2})) == {"input", "request"}


def test_cost_from_usage_matches_a_real_cached_generation():
    """Live numbers from the module probe: 4421 prompt tokens, 4224 of them served from Azure's prompt cache."""
    usage = {"input": 197, "input_cache_read": 4224, "output": 5, "total": 4426}
    assert cost_from_usage(usage, GPT52) == pytest.approx(0.00115395)


@pytest.mark.parametrize("usage, prices, expected", [
    ({"input": 1000, "output": 100}, {"input": 1e-6, "output": 2e-6}, 0.0012),
    ({"input": 1000, "output": 100, "total": 1100}, {"input": 1e-6, "output": 2e-6}, 0.0012),  # total skipped
    ({"input": 1000, "input_cache_read": 5000}, {"input": 1e-6}, 0.001),          # unpriced cache = free
    ({"input": 10, "output_audio": 7}, {"input": 1.0}, 10.0),                    # unknown type costs 0
    ({"total": 500}, {"total": 2e-6}, 0.001),                                    # flat per-token model
    ({"input": 100, "output": 10, "total": 110}, {"total": 1e-3}, 0.11),         # total-only price uses total
    ({"input": None, "output": 10}, {"input": 1.0, "output": 1.0}, 10.0),        # None count = 0
    ({}, GPT52, 0.0),
    ({"input": 10}, {}, 0.0),                                                    # unmatched model: no prices
])
def test_cost_from_usage(usage, prices, expected):
    assert cost_from_usage(usage, prices) == pytest.approx(expected)


def test_over_budget_strict_and_sorted():
    costs = {"a": 0.01, "b": 0.05, "c": 0.02, "d": None}
    assert over_budget(costs, 0.015) == ["b", "c"]
    assert over_budget(costs, 0.02) == ["b"]          # equal to the budget is not over
    assert over_budget(costs, 1.0) == []
    assert over_budget({}, 0.0) == []
    assert over_budget({"x": None}, -1.0) == ["x"]    # None counts as 0


@pytest.mark.parametrize("costs, factor, expected", [
    ({"a": 1.0, "b": 1.2, "c": 0.9, "spike": 20.0}, 5.0, ["spike"]),
    ({"a": 1.0, "b": 1.0, "s1": 6.0, "s2": 9.0, "c": 1.0}, 5.0, ["s2", "s1"]),   # most expensive first
    ({"a": 1.0, "b": 2.0, "c": 5.0}, 2.5, ["c"]),                                # >= factor x median (2.0)
    ({"a": 1.0, "b": 1.1}, 5.0, []),
    ({"a": 0.0, "b": 0.0, "c": 0.003}, 5.0, ["c"]),                             # median 0: positives are spikes
    ({"a": None, "b": None, "c": 0.5}, 5.0, ["c"]),                             # None = 0
    ({}, 5.0, []),
    ({"only": 3.0}, 5.0, []),                                                    # median is itself
])
def test_cost_spikes(costs, factor, expected):
    assert cost_spikes(costs, factor=factor) == expected


def test_cost_spikes_uses_the_median_not_the_mean():
    """With the mean, the 50.0 spike would raise the bar to 5 x 13.25 = 66 and hide itself."""
    assert cost_spikes({"a": 1.0, "b": 1.0, "c": 1.0, "spike": 50.0}) == ["spike"]


def test_cost_spikes_even_count_median_averages_the_middle_pair():
    # median of [1, 2, 4, 30] = 3.0 -> threshold 15 with factor 5
    assert cost_spikes({"a": 1.0, "b": 2.0, "c": 4.0, "d": 30.0}) == ["d"]
    # median of [1, 3, 3, 15] = 3.0 -> 15 is exactly 5x: a spike
    assert cost_spikes({"a": 1.0, "b": 3.0, "c": 3.0, "d": 15.0}) == ["d"]
