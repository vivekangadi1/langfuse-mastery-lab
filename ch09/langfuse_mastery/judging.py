"""Module 07: pure helpers for live evaluation. You implement these; `pytest langfuse_mastery/tests/test_judging.py`
checks them.

No Langfuse imports here. These functions build the plain dicts the evaluator / evaluation-rule APIs accept
(verified live: `variable_mapping` and `filter` take dicts), and they check things the server would otherwise reject
with a 400 at create time, or, worse, accept and then never match.
"""
from __future__ import annotations
import re
# Sources a prompt variable can be filled from on live observations (expected_output / experiment_item_metadata
# exist too, but only for experiments: module 09).
SOURCES = ("input", "output", "metadata", "tool_calls")
_VARIABLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

def prompt_variables(template: str) -> list[str]:
    """The `{{name}}` variables in an evaluator prompt, in first-seen order, without duplicates.

    Whitespace inside the braces is allowed: "{{ input }}" is the variable "input". Names are word characters
    (letters, digits, underscore). Text that only looks similar ("{input}", "{{}}") is not a variable.
    """
    names = []
    for name in _VARIABLE.findall(template):
        if name not in names:      # keep the first occurrence only
            names.append(name)
    return names
    


def variable_mapping(spec: dict[str, str]) -> list[dict]:
    """Turn a compact {variable: source} spec into the API's mapping list, in the spec's order.

        {"input": "input", "handoff": "metadata:$.handoff_target"}
        -> [{"variable": "input", "source": "input"},
            {"variable": "handoff", "source": "metadata", "jsonPath": "$.handoff_target"}]

    A source may carry a JSONPath after the first ":". Raise ValueError if the source is not in SOURCES, or if a
    JSONPath is given but does not start with "$". Leave "jsonPath" out entirely when there is none.
    """
    mapping = []
    for variable, source_spec in spec.items():
        source, sep, json_path = source_spec.partition(":")   # split on the FIRST ":" only
        if source not in SOURCES:
            raise ValueError(f"{variable}: unknown source {source!r}, expected one of {SOURCES}")
        entry = {"variable": variable, "source": source}
        if sep:                                               # a ":" was there, so a JSONPath was given
            if not json_path.startswith("$"):
                raise ValueError(f"{variable}: JSONPath {json_path!r} must start with '$'")
            entry["jsonPath"] = json_path
        mapping.append(entry)
    return mapping


def check_mapping(template: str, mapping: list[dict]) -> None:
    """Raise ValueError unless `mapping` has exactly one entry per prompt variable and nothing else.

    The message must name the missing variables (e.g. "missing: handoff, answer") and/or the extra ones
    ("extra: foo"), and duplicates ("duplicate: input"). This is the check the server does at create time
    ("Missing mappings for evaluator variables: handoff, answer"); doing it first saves a slow round trip.
    """
    wanted = prompt_variables(template)                  # what the judge prompt needs
    mapped = [m["variable"] for m in mapping]            # what the mapping provides
    unique = list(dict.fromkeys(mapped))                 # mapped, without repeats, in order

    missing = [v for v in wanted if v not in mapped]
    extra = [v for v in unique if v not in wanted]
    duplicate = [v for v in unique if mapped.count(v) > 1]

    problems = []
    if missing:
        problems.append("missing: " + ", ".join(missing))
    if extra:
        problems.append("extra: " + ", ".join(extra))
    if duplicate:
        problems.append("duplicate: " + ", ".join(duplicate))
    if problems:
        raise ValueError("; ".join(problems))
   


def rule_filters(live_tag: str, root_name: str) -> list[dict]:
    """The evaluation-rule filter list: judge only ROOT observations named `root_name` on traces tagged `live_tag`.

    Each column accepts exactly one filter type (the wrong one is a 400):
        tags              -> {"type": "arrayOptions",  "column": "tags", "operator": "any of", "value": [live_tag]}
        isRootObservation -> {"type": "boolean",       "column": "isRootObservation", "operator": "=", "value": True}
        name              -> {"type": "stringOptions", "column": "name", "operator": "any of", "value": [root_name]}
    Return exactly these three, in this order. Do NOT filter on "traceName": our observations carry no trace name,
    so such a rule silently never fires (verified live).
    """
    return [
        {"type": "arrayOptions", "column": "tags", "operator": "any of", "value": [live_tag]},
        {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
        {"type": "stringOptions", "column": "name", "operator": "any of", "value": [root_name]},
    ]
    raise NotImplementedError("TODO 1: implement rule_filters")


def as_verdict(value) -> bool | None:
    """A BOOLEAN score value as read back from the API -> bool.

    True/False, 1/0 (int or float) and the strings "true"/"false"/"1"/"0" (any case, surrounding spaces allowed)
    map to a bool. Anything else (None, 0.5, "maybe", a missing score) -> None.
    """
    if isinstance(value, bool):              # check bool FIRST: True is also an int in Python
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None                          # 0.5, 2, ... are not a verdict
    if isinstance(value, str):
        text = value.strip().lower()         # " FALSE " -> "false"
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
    return None


def agreement(pairs) -> dict:
    """Calibrate a judge against a trusted signal. `pairs` = [(judge_ok, code_ok), ...] where each item is a bool or
    None ("ok" = routed correctly). Pairs with a None on either side are skipped.

    Returns:
        n                  pairs used
        agree              pairs where judge_ok == code_ok
        rate               agree / n, or None when n == 0
        judge_flagged      pairs where the judge said NOT ok
        code_flagged       pairs where the code score said NOT ok
        both_flagged       pairs flagged by both
        judge_missed       code flagged, judge passed (the dangerous one: a failure the judge lets through)
        judge_false_alarm  judge flagged, code passed (noise a reviewer has to look at)
    """
    used = [(j, c) for j, c in pairs if j is not None and c is not None]   # skip incomplete pairs
    n = len(used)
    agree = sum(1 for j, c in used if j == c)
    return {
        "n": n,
        "agree": agree,
        "rate": agree / n if n else None,                                   # no division by zero
        "judge_flagged": sum(1 for j, c in used if not j),
        "code_flagged": sum(1 for j, c in used if not c),
        "both_flagged": sum(1 for j, c in used if not j and not c),
        "judge_missed": sum(1 for j, c in used if j and not c),
        "judge_false_alarm": sum(1 for j, c in used if not j and c),
    }
