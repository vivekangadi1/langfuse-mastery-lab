"""Module 06: pure scoring functions. You implement these; `pytest langfuse_mastery/tests` checks them.

No Langfuse imports here: these are plain functions, so they're cheap to test and safe to reuse in CI (module 10).

A `config` is a score config as a dict with the API's camelCase keys. For example:
    {"id": "...", "name": "kb_hit", "dataType": "NUMERIC", "minValue": 0.0, "maxValue": 1.0, "categories": None}
    {"id": "...", "name": "user_feedback", "dataType": "CATEGORICAL",
     "categories": [{"label": "thumbs_up", "value": 1.0}, {"label": "thumbs_down", "value": 0.0}]}
"""
from __future__ import annotations


def validate_score(config: dict, value) -> tuple[bool, str]:
    """Would Langfuse accept `value` for this config? Returns (True, "") or (False, "<reason>").

    The server silently DROPS a score that violates its config (module 06 demo), so check before sending.
    Rules:
    - NUMERIC: an int or float (but not a bool) within [minValue, maxValue]. A bound that is None means unbounded.
    - BOOLEAN: True, False, 0 or 1.
    - CATEGORICAL: a string equal to one of the config's category labels.
    - any other dataType (TEXT): a non-empty string.
    """
    dtype = config.get("dataType")
    if dtype == "NUMERIC":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, f"{config.get('name')}: {value!r} is not a number"
        lo, hi = config.get("minValue"), config.get("maxValue")
        if lo is not None and value < lo:
            return False, f"{config.get('name')}: {value} is below the minimum {lo}"
        if hi is not None and value > hi:
            return False, f"{config.get('name')}: {value} is above the maximum {hi}"
        return True, ""
    if dtype == "BOOLEAN":
        if isinstance(value, bool) or value in (0, 1):
            return True, ""
        return False, f"{config.get('name')}: {value!r} is not a boolean (True/False/0/1)"
    if dtype == "CATEGORICAL":
        labels = [c["label"] for c in (config.get("categories") or [])]
        if value in labels:
            return True, ""
        return False, f"{config.get('name')}: {value!r} is not one of {labels}"
    
    if isinstance(value, str) and value.strip():
        return True, ""
    return False, f"{config.get('name')}: {dtype} scores need a non-empty string"


def handoff_correct(actual: str, expected: str) -> bool:
    """Did the agent hand off to the expected team? "none" means no handoff was expected or made."""
    return actual == expected
    
    raise NotImplementedError("TODO 2: implement handoff_correct")


def kb_hit(returned_ids: list[str], expected_id: str | None) -> float | None:
    """1.0 if the expected KB article was retrieved, 0.0 if not, None when the case expects no article."""
    if expected_id != None and expected_id in returned_ids:
            return 1.0
    if expected_id != None and expected_id not in returned_ids:
        return 0.0
    else:
        return None
    raise NotImplementedError("TODO 2: implement kb_hit")
