"""Input guardrail: blocks prompt injection, flags card numbers (PII)."""
from __future__ import annotations

import re
from dataclasses import dataclass

INJECTION_PATTERNS = [
    r"ignore (all )?(previous|prior) instructions",
    r"reveal (your )?(system )?prompt",
    r"disregard (the |your )?rules",
    r"you are now (?:a|an|in|dan)\b",
]
CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


@dataclass(frozen=True)
class GuardrailResult:
    allowed: bool
    flags: tuple = ()


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def check_input(text: str) -> GuardrailResult:
    flags = []
    lowered = text.lower()
    if any(re.search(p, lowered) for p in INJECTION_PATTERNS):
        flags.append("prompt_injection")
    for match in CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            flags.append("pii_card_number")
            break
    return GuardrailResult(allowed="prompt_injection" not in flags, flags=tuple(flags))
