"""Module 08: a STAND-IN routing judge, for when module 07's live LLM judge is not set up.

Deterministic and deliberately naive: it maps keywords to the team the routing policy requires and compares that with
the team the agent handed off to. Like the first draft of module 07's judge prompt, it reads "refund" as a billing
problem, so it raises a false alarm on "What is your refund policy?". That is the point: human review exists to catch
judge errors as well as agent errors.

No Langfuse code here. The notebook writes this judge's verdict as a score (source=API, metadata.evaluator=EVALUATOR).
"""
from __future__ import annotations

EVALUATOR = "stand-in:keyword-v1"  # verify recognises stand-in judge scores by this metadata.evaluator prefix
STAND_IN_PREFIX = "stand-in"

# (team, keywords); first match wins. "none" when nothing matches.
POLICY = (
    ("billing_specialist", ("charged", "charge", "refund", "invoice", "billing")),
    ("account_security", ("password", "log in", "login", "locked out", "2fa", "account access")),
    ("shipping_specialist", ("lost", "delayed", "never arrived", "damaged")),
)


def policy_team(message: str) -> str:
    text = (message or "").lower()
    for team, keywords in POLICY:
        if any(k in text for k in keywords):
            return team
    return "none"


def judge_routing(message: str, handoff: str | None) -> tuple[bool, str]:
    """(ok, reasoning): ok when the agent's handoff equals the keyword policy's team."""
    want, got = policy_team(message), handoff or "none"
    ok = want == got
    return ok, f"[stand-in keyword judge] policy team: {want}; agent chose: {got}."
