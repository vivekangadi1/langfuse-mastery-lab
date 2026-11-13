"""Module 04: pure prompt functions. You implement these; `pytest langfuse_mastery/tests/test_prompting.py` checks them.

No Langfuse imports here. A prompt served by Langfuse is a **template** plus a **config**:
    template = "You are {{company}}'s support agent. Reply in at most {{max_sentences}} sentences."
    config   = {"temperature": 0, "max_tokens": 400}

Two things Langfuse leaves to you (both verified live):
- `prompt.compile(company="Acme")` silently leaves `{{max_sentences}}` in the text when you forget a variable.
  The model then reads a literal "{{max_sentences}}". `compile_strict` refuses instead.
- The config is free-form JSON that anyone with UI access can edit. `llm_params` turns it into model kwargs you
  are willing to send, and rejects values the model API would refuse (a typo here is a production outage).
"""
from __future__ import annotations


def template_variables(template: str) -> list[str]:
    """The variable names in a Langfuse template, unique, in first-seen order.

    A variable is `{{name}}`; whitespace inside the braces is ignored: `{{ name }}` is `name`.
    template_variables("Hi {{ name }}, {{x}} and {{name}} again") == ["name", "x"]
    A "{{" with no closing "}}" is plain text.
    """
    raise NotImplementedError("TODO 1: implement template_variables")


def compile_strict(template: str, variables: dict) -> str:
    """Substitute every `{{name}}` (whitespace inside the braces allowed) with str(variables[name]).

    Raise ValueError naming EVERY missing variable (sorted), e.g. "missing prompt variables: company, max_sentences".
    Extra keys in `variables` are ignored. A template with no variables is returned unchanged.
    """
    raise NotImplementedError("TODO 1: implement compile_strict")


def llm_params(config: dict | None) -> dict:
    """The model kwargs to send for a prompt config: only the keys below, validated; other keys are ignored.

    - "temperature": a number (int or float, not bool) in [0, 2]
    - "max_tokens":  an int (not bool) >= 1
    - "top_p":       a number (int or float, not bool) in [0, 1]

    Raise ValueError naming the key on an invalid value. None or {} -> {}.
    llm_params({"temperature": 0, "max_tokens": 400, "owner": "support"}) == {"temperature": 0, "max_tokens": 400}
    """
    raise NotImplementedError("TODO 1: implement llm_params")
