"""The prod/staging separation plan (module 13, TODO 2): YOU write it. Data, reviewed like code, checked by `ops.lint_plan` and by
`tests/test_ops_plan.py`, and compared with the live stack by verify.

The team and its requirements (what the plan must satisfy):
- The **support-api** service runs in production and in staging. Each deployment sends traces (with scores) and reads
  its system prompt from Langfuse. In production it must not be able to delete anything.
- **ops-jobs** (GDPR erasure, the retention job) run against production: they read and delete traces.
- **platform** (2 people) administers both sides: keys, members, settings.
- **ml-engineers** iterate on prompts, datasets and evaluators in staging; in production they only read traces.
- **support-leads** read production traces (customer escalations). Nothing else.
- **contractors** label staging data (scores/annotations). They must not see production.
- Retention: production 30 days, staging 14, dev/ci 7, the SDK lab's load-test data 1 day.
- The production project is the one this lab already uses (`ch09-eval-project`, organization `ch09-eval-org`).
- This stack is the open-source edition (see `langfuse_lab.ops_lab.ENTERPRISE_ONLY` for what that rules out).

Shape (every key below is read by `lint_plan`):
    edition: "oss" | "enterprise"
    retention_by: "job" | "native";  retention_default_days: int | None (environments not listed)
    protected_labels: [label, ...]   (Enterprise only)
    organizations: {org: {"projects": [project, ...]}}
    projects: {project: {"id": str | None (None = not created yet), "environments": {env: retention days | None}}}
    deployments: {name: {"project", "environment", "tier": "production" | "non-production", "release",
                         "keys": [key, ...], "needs": [action, ...]}}   actions: ops.KEY_PERMISSIONS values
    keys: {key: {"project", "permission": ADMIN | VIEWER | INGEST | SCORES_INGEST, "owner": team, "rotate_days"}}
    teams: {team: {"roles": {org: role}, "project_roles": {project: role} (Enterprise only),
                   "needs": {project: [action, ...]}}}   actions: ops.ROLE_ACTIONS keys
"""

# TODO 2: fill in the plan. Start from the requirements above; run `pytest langfuse_mastery/tests/test_ops_plan.py`
# (it needs your TODO 1 `lint_plan`) until it passes. The skeleton has the right keys and no content yet.
PLAN = {
    "edition": "oss",
    "retention_by": None,             # "job" | "native"
    "retention_default_days": None,
    "protected_labels": [],
    "organizations": {},
    "projects": {},
    "deployments": {},
    "keys": {},
    "teams": {},
}
