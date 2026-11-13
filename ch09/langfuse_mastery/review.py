"""Module 08: pure helpers for human review. You implement these; `pytest langfuse_mastery/tests/test_review.py`
checks them.

No Langfuse imports here. Scores are plain dicts as `api.scores.get_many(..., fields="details,subject,annotation")`
returns them after `.dict()`: {"name", "value", "source", "dataType", "comment", "metadata", "queueId", ...}.
Queue items are dicts like {"id", "objectId", "objectType", "status"}.
"""
from __future__ import annotations

STAND_IN_PREFIX = "stand-in"  # metadata.evaluator of the notebook's stand-in judge ("stand-in:keyword-v1")
ERROR_TYPES = ("wrong_team", "missed_handoff", "unnecessary_handoff", "other")


def is_judge_score(score: dict) -> bool:
    """True for a judge's verdict: a live LLM-as-a-judge score (source "EVAL"), or the stand-in judge's score
    (source "API" and metadata["evaluator"] starting with STAND_IN_PREFIX). Everything else (code scores, human
    ANNOTATION scores, user feedback) is False. A missing/None metadata counts as {}.
    """
    raise NotImplementedError("TODO 1: implement is_judge_score")


def judge_flags(scores: list[dict], *, boolean_judges=(), numeric_judges=(), threshold: float = 0.5) -> list[str]:
    """Why a trace needs human review: one reason per judge that flagged it, [] when no judge did.

    Only judge scores count (is_judge_score). A judge named in `boolean_judges` flags with a False verdict
    (False, 0, "false", "0"; any case) -> "routing_judge=False". A judge in `numeric_judges` flags when its value is
    a number below `threshold` -> "helpfulness_judge=0.3<0.5" (format numbers with :g). At most one reason per judge
    name, in the order boolean_judges then numeric_judges. Scores of judges not named are ignored.
    """
    raise NotImplementedError("TODO 1: implement judge_flags")


def pick_controls(trace_ids: list[str], *, rate: float, min_n: int = 1, salt: str = "") -> list[str]:
    """A deterministic random sample of traces the judge PASSED, reviewed as controls (so judge misses show up).

    k = max(min_n, ceil(rate * len(trace_ids))), capped at len(trace_ids). Rank ids by
    hashlib.sha256((salt + id).encode()).hexdigest() and keep the k smallest. Return them in their original order.
    Duplicates in `trace_ids` count once. An empty list gives [].
    """
    raise NotImplementedError("TODO 1: implement pick_controls")


def items_to_add(existing_items: list[dict], wanted_ids: list[str], *, object_type: str = "TRACE") -> list[str]:
    """The ids from `wanted_ids` that are not yet in the queue as an item of `object_type`, in order, each once.

    The queue API does NOT dedupe: adding a trace twice creates two items. An existing item counts whatever its
    status (PENDING or COMPLETED): a reviewed trace is not re-queued. An item with the same id but another
    objectType (e.g. an OBSERVATION with that id) does not count.
    """
    raise NotImplementedError("TODO 1: implement items_to_add")


def error_type(expected: str | None, actual: str | None) -> str | None:
    """The reviewer's error taxonomy for a routing decision (None/"" mean "none" = no handoff):
        expected == actual                      -> None (no error)
        expected "none", actual a team          -> "unnecessary_handoff"
        expected a team, actual "none"          -> "missed_handoff"
        both teams, different                   -> "wrong_team"
    """
    raise NotImplementedError("TODO 1: implement error_type")


def agreement(pairs) -> dict:
    """Human-vs-judge agreement. `pairs` = [(judge_ok, human_ok), ...], each a bool or None (ok = routed correctly).
    Pairs with a None on either side are skipped. The human is the reference.

    Returns:
        n                  pairs used
        agree              pairs where judge_ok == human_ok
        rate               agree / n (None when n == 0)
        judge_flagged      judge said NOT ok
        human_flagged      human said NOT ok
        both_flagged       both said NOT ok
        judge_missed       human NOT ok, judge ok     (only control items can reveal these)
        judge_false_alarm  judge NOT ok, human ok     (wasted reviewer time; a reason to fix the judge prompt)
        flag_precision     both_flagged / judge_flagged (None when judge_flagged == 0)
        kappa              Cohen's kappa (None when n == 0 or chance agreement pe == 1):
                           po = agree / n; pj, ph = share of judge_ok / human_ok True;
                           pe = pj*ph + (1-pj)*(1-ph); kappa = (po - pe) / (1 - pe)
    """
    raise NotImplementedError("TODO 1: implement agreement")
