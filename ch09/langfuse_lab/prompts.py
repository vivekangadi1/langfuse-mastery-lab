"""Agent system prompts. Module 04 moves SYSTEM_PROMPT_TEMPLATE into Langfuse prompt management as PROMPT_NAME."""
from datetime import datetime, timezone

from .failures import FailureConfig

# Module 04 (and module 09's prompt experiment) manage this prompt in Langfuse under PROMPT_NAME.
# `production` = the version the agent serves; `staging` = the candidate under test.
PROMPT_NAME = "support-agent-system"
PROMPT_VARIABLES = {"company": "Acme", "max_sentences": 3}  # supplied by the app at compile time
PROMPT_CONFIG = {"temperature": 0, "max_tokens": 400}  # model parameters stored with the prompt version

SYSTEM_PROMPT_TEMPLATE = """You are {{company}}'s customer support agent.
Use the tools to look things up; never invent order, shipment, or account data.
Use search_kb for policy questions.
Routing rules for create_handoff:
- double charge, refund, invoice -> billing_specialist
- login, password, account access -> call reset_password if you know the customer id, then ALWAYS create_handoff to account_security
- lost or delayed shipment -> shipping_specialist
- questions you can fully answer yourself -> no handoff
Reply to the customer in at most {{max_sentences}} sentences."""

# The planted bad version (module 04 promotes it, then rolls it back): login issues go to billing.
WRONG_HANDOFF_TEMPLATE = SYSTEM_PROMPT_TEMPLATE.replace(
    "create_handoff to account_security", "create_handoff to billing_specialist"
)


def render(template: str, variables: dict) -> str:
    """Plain {{name}} substitution (no Langfuse). The lab's own prompts are always fully rendered."""
    for name, value in variables.items():
        template = template.replace("{{" + name + "}}", str(value))
    return template


SYSTEM_PROMPT = render(SYSTEM_PROMPT_TEMPLATE, PROMPT_VARIABLES)
WRONG_HANDOFF_PROMPT = render(WRONG_HANDOFF_TEMPLATE, PROMPT_VARIABLES)

COST_SPIKE_PADDING = "\n\nPolicy appendix:\n" + ("Be courteous, precise, and consistent in every reply. " * 400)

# One deployment for everything (`Settings.single_model`, e.g. a single gpt-5.4): cost_spike has no pricier model to
# switch to, and the appendix above alone barely shows: Azure caches it after the first call (verified live: 4,480
# of its ~4,800 prompt tokens are `input_cache_read`, billed at a tenth). So the single-model v2 ships a bigger
# appendix (~17,600 tokens) behind a header that changes on EVERY call (`request_time_header`). Azure's prompt cache
# matches identical leading tokens only, so nothing after the header is ever cached and every padded token is billed
# at the full input price: the same lesson (only cost tracking sees it), with a real cause (a timestamp at the top of
# a prompt silently disables prompt caching). Sized so that a ONE-call spike turn still costs >= 5x a three-call
# clean turn (verified live on gpt-5.2-chat at 1000 repetitions: 11,500 uncached input tokens, $0.021 per call vs a
# $0.0014 clean call, too thin a margin against a three-call turn; at 1600: ~18,200 input tokens, 0 cached, module
# 03's two spike traces 20x and 30x the clean median trace).
SINGLE_MODEL_SPIKE_PADDING = "\n\nPolicy appendix:\n" + ("Be courteous, precise, and consistent in every reply. " * 1600)


def request_time_header(now: datetime | None = None) -> str:
    """The single-model cost_spike's cache buster: the first line of the system prompt, new on every LLM call."""
    return f"Request time: {(now or datetime.now(timezone.utc)).isoformat(timespec='microseconds')}\n"


def system_prompt(failures: FailureConfig, *, single_model: bool = False) -> str:
    """`single_model`: cost_spike pads with SINGLE_MODEL_SPIKE_PADDING (the agent adds `request_time_header` per call)."""
    base = WRONG_HANDOFF_PROMPT if failures.wrong_handoff else SYSTEM_PROMPT
    if not failures.cost_spike:
        return base
    return base + (SINGLE_MODEL_SPIKE_PADDING if single_model else COST_SPIKE_PADDING)

# Module 10: a CLEAN candidate for the CI gate (label CANDIDATE_LABEL). Same routing as production, with the login rule
# made explicit for requests without a customer id (verified live: production and this version both route the vague
# login and the locked-out cust_03 to account_security). `staging` stays the planted bad version.
CANDIDATE_LABEL = "candidate"
CLEAN_CANDIDATE_TEMPLATE = SYSTEM_PROMPT_TEMPLATE.replace(
    "call reset_password if you know the customer id, then ALWAYS create_handoff to account_security",
    "call reset_password if you know the customer id; with or without it, ALWAYS create_handoff to account_security",
)

# Module 14 (the capstone): two fixes for the planted deploy (live -> WRONG_HANDOFF_TEMPLATE, login -> billing).
# - the QUICK fix is a find-and-replace billing_specialist -> account_security over the routing rules: it repairs login,
#   but it also sends double charges, refunds and invoices to account security (the CI gate must block it);
# - the FIX is production's rules with the clean candidate's login wording and one explicit guard line.
# HOTFIX_LABEL is the label of the fix under review (what the gate's CANDIDATE_LABEL points at).
HOTFIX_LABEL = "hotfix"
QUICK_FIX_TEMPLATE = WRONG_HANDOFF_TEMPLATE.replace("billing_specialist", "account_security")
LOOP_FIX_TEMPLATE = CLEAN_CANDIDATE_TEMPLATE.replace(
    "- lost or delayed shipment -> shipping_specialist",
    "- never send login, password or account access issues to billing_specialist\n"
    "- lost or delayed shipment -> shipping_specialist",
)
