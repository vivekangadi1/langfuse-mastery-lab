"""Production ops as code (module 13, TODO 1): pure functions, no Langfuse calls. You implement the six functions
that raise NotImplementedError; `pytest langfuse_mastery/tests/test_ops.py` checks them.

The platform team's questions, answered from data:
- who can do what?            `effective_role`, `can`, `least_role` (roles and actions below, from the server's tables)
- is our separation plan sound for THIS edition?   `lint_plan` (the plan's shape is in `ops_plan.py`)
- what does the nightly retention job delete?      `expired`
- what did the SDK do to the app and to the data?  `delivery_report`

Given (from the server source at v4.77.0, `packages/shared/src/features/rbac/projectAccessRights.ts` and the API key
roles; simplified to the actions a plan talks about):
"""
from __future__ import annotations

import math  # noqa: F401  (math.ceil for the percentiles)
import re
from datetime import datetime, timezone

ROLES = ("OWNER", "ADMIN", "MEMBER", "VIEWER", "NONE")  # highest first
ROLE_ACTIONS = {  # action -> the LOWEST role allowed to do it in a project
    "read_traces": "VIEWER",       # traces, sessions, scores, dashboards (read)
    "read_prompts": "VIEWER",
    "create_scores": "MEMBER",     # scores, annotations, comments
    "edit_prompts": "MEMBER",      # includes moving the `production` label (unless protected labels, Enterprise)
    "edit_datasets": "MEMBER",
    "edit_evaluators": "MEMBER",
    "delete_traces": "ADMIN",
    "manage_api_keys": "ADMIN",
    "manage_members": "ADMIN",
    "edit_project_settings": "ADMIN",
    "delete_project": "OWNER",
}
KEY_PERMISSIONS = {  # project API key permission sets (Project settings -> API keys -> Create) -> what a key can do
    "ADMIN": frozenset({"ingest", "create_scores", "read_prompts", "read_traces", "delete_traces"}),  # "Full access"
    "VIEWER": frozenset({"read_prompts", "read_traces"}),
    "INGEST": frozenset({"ingest", "create_scores"}),  # traces:create, scores:save, media:create: it cannot read
    "SCORES_INGEST": frozenset({"create_scores"}),
}
ENV_NAME = re.compile(r"^(?!langfuse)[a-z0-9_-]{1,40}$")  # Langfuse's rule for environment names
TIERS = ("production", "non-production")
ENTERPRISE_FEATURES = {  # plan field -> entitlement it needs
    "project_roles": "rbac-project-roles",
    "native_retention": "data-retention",
    "protected_labels": "prompt-protected-labels",
}


def _rank(role: str) -> int:
    """Given: 0 for OWNER ... 4 for NONE (lower = more power); unknown role -> ValueError."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; one of {ROLES}")
    return ROLES.index(role)


def effective_role(org_role: str, project_role: str | None = None, *, entitlements) -> str:
    """The role a member has in one project.

    A member's ORGANIZATION role applies to every project of the organization. A project role overrides it (up or
    down) for that one project, but only where project-level roles exist (`"rbac-project-roles"` in `entitlements`,
    Enterprise). A project role of None or "NONE" means "no override". Unknown roles raise ValueError.
    """
    raise NotImplementedError("TODO 1 in langfuse_mastery/ops.py: effective_role")


def can(role: str, action: str) -> bool:
    """True when `role` may do `action` in a project (ROLE_ACTIONS). NONE can do nothing. Unknown role -> ValueError,
    unknown action -> KeyError."""
    raise NotImplementedError("TODO 1 in langfuse_mastery/ops.py: can")


def least_role(actions) -> str:
    """The lowest role that may do every action in `actions` ("NONE" for no actions)."""
    raise NotImplementedError("TODO 1 in langfuse_mastery/ops.py: least_role")


def lint_plan(plan: dict, *, entitlements, max_key_age_days: int = 90) -> list[str]:
    """Every problem of a separation plan (shape: `ops_plan.py`), as "<code>: <detail>" strings; [] = sound.

    Codes (one string per problem found):
    - `env-name`:   an environment name breaks Langfuse's rule (ENV_NAME: lowercase, digits, - and _, <= 40 chars,
                    not starting with "langfuse").
    - `deploy`:     a deployment names an unknown project, an environment its project does not list, or a tier not in
                    TIERS.
    - `prod-isolation`: one project hosts deployments of BOTH tiers (a staging key could read or delete production).
    - `key`:        a deployment uses an unknown key or another project's key; a key is used by more than one
                    deployment; a key has an unknown owner team, an unknown permission, or no `rotate_days` /
                    `rotate_days` > max_key_age_days.
    - `key-needs`:  a deployment's keys together do not grant everything in its `needs` (KEY_PERMISSIONS).
    - `key-delete`: a deployment's keys grant "delete_traces" but it does not need it (a leaked key could wipe data).
    - `team-needs`: in a project where a team needs actions, its effective role cannot do one of them.
    - `team-excess`: in a project the team can access (effective role above NONE), the role is above the least role its
                    needs there require (no needs = NONE). One problem per (team, project).
    - `retention`:  a retention value is not None and < 1 day, or (with native retention) < 3 days.
    - `ee`:         the plan uses an Enterprise feature its edition lacks: a team's `project_roles` (rbac-project-roles),
                    `"retention_by": "native"` (data-retention), non-empty `protected_labels` (prompt-protected-labels).

    A team's effective role in a project = `effective_role(<its role in the project's organization, or "NONE">,
    <its project_roles entry for that project>, entitlements=...)`.
    """
    raise NotImplementedError("TODO 1 in langfuse_mastery/ops.py: lint_plan")


def retention_policy(plan: dict) -> dict:
    """Given: {environment: days or None} from the plan's projects (None = keep forever), plus the plan's default for
    environments it does not list under "*" (`plan["retention_default_days"]`, None when absent)."""
    policy = {env: days for proj in plan.get("projects", {}).values()
              for env, days in proj.get("environments", {}).items()}
    policy["*"] = plan.get("retention_default_days")
    return policy


def _when(v) -> datetime:
    """Given: an ISO string or datetime -> an aware datetime (naive = UTC)."""
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def expired(rows, policy: dict, *, now) -> list[str]:
    """The trace ids a retention job run at `now` deletes, sorted.

    rows: dicts with "trace_id", "environment", "timestamp" (ISO string or datetime; naive = UTC); a trace may appear
    in several rows: it is judged by its OLDEST timestamp (and its first row's environment).
    policy: {environment: days | None, "*": days | None}: an environment not in the policy uses "*" (absent = None).
    None keeps forever. A trace expires when `now - oldest timestamp > days` (strictly older).
    """
    raise NotImplementedError("TODO 1 in langfuse_mastery/ops.py: expired")


def delivery_report(sent: dict, exports: list[dict], stored: dict, *, spans_per_request: int) -> dict:
    """What one deployment's SDK did to the request path and to the data.

    sent:    the worker's result: {"trace_ids": [...], "latencies_ms": [...], ...}
    exports: the tap's log rows for this deployment, oldest first: {"batch", "status", "spans", "trace_ids", ...}
             (one row per export ATTEMPT; a retry repeats the same "batch")
    stored:  {trace_id: number of observations Langfuse stored} (a missing id = 0)

    Returns (percentiles by nearest rank: sorted(v)[ceil(p*n) - 1], None when empty):
      requests, p50_ms, p95_ms, max_ms                      (request path, from latencies_ms)
      attempts           number of export attempts
      batches            number of distinct batches
      failed_attempts    attempts whose status is not 2xx
      dropped_batches    batches that never got a 2xx
      spans_exported     spans in the batches that got a 2xx (each batch counted once)
      delivered          number of sent traces with stored == spans_per_request
      partial            sorted ids with 0 < stored < spans_per_request
      lost               sorted ids with nothing stored
      ok                 True when nothing is lost or partial
    """
    raise NotImplementedError("TODO 1 in langfuse_mastery/ops.py: delivery_report")
