"""Planted failures. Each one should later be CAUGHT by a Langfuse feature."""
from __future__ import annotations

from dataclasses import dataclass

FAILURE_MODES = ("wrong_handoff", "slow_tool", "cost_spike", "pii")


@dataclass(frozen=True)
class FailureConfig:
    wrong_handoff: bool = False      # prompt routes login issues to billing   -> caught by judges / CI gate
    slow_tool_seconds: float = 0.0   # lookup tools sleep                      -> caught by latency views / alerts
    cost_spike: bool = False         # expensive deployment + padded prompt    -> caught by cost tracking / alerts
                                     # (one deployment only: a bigger, cache-busting padded prompt; see prompts.py)
    pii: bool = False                # traffic pastes card numbers             -> caught by guardrail flags / masking

    @classmethod
    def from_names(cls, names) -> "FailureConfig":
        if isinstance(names, str):
            names = (names,)
        names = set(names or ())
        unknown = names - set(FAILURE_MODES)
        if unknown:
            raise ValueError(f"Unknown failure modes {sorted(unknown)}; choose from {FAILURE_MODES}")
        return cls(
            wrong_handoff="wrong_handoff" in names,
            slow_tool_seconds=3.0 if "slow_tool" in names else 0.0,
            cost_spike="cost_spike" in names,
            pii="pii" in names,
        )
