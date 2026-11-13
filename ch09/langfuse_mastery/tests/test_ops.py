"""Module 13 TODO 1: production ops as pure functions (no server needed)."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from langfuse_mastery import ops

OSS = frozenset({"trace-deletion", "scheduled-blob-exports", "in-app-agent"})
EE = OSS | {"rbac-project-roles", "data-retention", "prompt-protected-labels", "audit-logs", "admin-api"}
UTC = timezone.utc
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

# A small plan that is sound on OSS: production and non-production in separate organizations (so roles can differ).
GOOD = {
    "edition": "oss", "retention_by": "job", "retention_default_days": None, "protected_labels": [],
    "organizations": {"org-prod": {"projects": ["prod"]}, "org-dev": {"projects": ["dev"]}},
    "projects": {"prod": {"id": "p1", "environments": {"production": 30}},
                 "dev": {"id": None, "environments": {"staging": 14, "ci": 7}}},
    "deployments": {
        "api@production": {"project": "prod", "environment": "production", "tier": "production",
                           "keys": ["api-prod-ingest", "api-prod-read"], "needs": ["ingest", "read_prompts"]},
        "api@staging": {"project": "dev", "environment": "staging", "tier": "non-production",
                        "keys": ["api-staging"], "needs": ["ingest"]},
        "jobs@production": {"project": "prod", "environment": "production", "tier": "production",
                            "keys": ["jobs-prod"], "needs": ["read_traces", "delete_traces"]},
    },
    "keys": {"api-prod-ingest": {"project": "prod", "permission": "INGEST", "owner": "platform", "rotate_days": 90},
             "api-prod-read": {"project": "prod", "permission": "VIEWER", "owner": "platform", "rotate_days": 90},
             "api-staging": {"project": "dev", "permission": "INGEST", "owner": "platform", "rotate_days": 60},
             "jobs-prod": {"project": "prod", "permission": "ADMIN", "owner": "platform", "rotate_days": 30}},
    "teams": {"platform": {"roles": {"org-prod": "ADMIN", "org-dev": "ADMIN"},
                           "needs": {"prod": ["manage_api_keys"], "dev": ["manage_members"]}},
              "engineers": {"roles": {"org-prod": "VIEWER", "org-dev": "MEMBER"},
                            "needs": {"prod": ["read_traces"], "dev": ["edit_prompts"]}},
              "contractors": {"roles": {"org-dev": "MEMBER"}, "needs": {"dev": ["create_scores"]}}},
}


def plan(**changes):
    p = copy.deepcopy(GOOD)
    for path, value in changes.items():
        node = p
        *parents, leaf = path.split("__")
        for k in parents:
            node = node[k]
        node[leaf] = value
    return p


def codes(problems):
    return sorted({p.split(":", 1)[0] for p in problems})


# ---------------------------------------------------------------- roles
def test_effective_role_on_oss_is_the_organization_role():
    assert ops.effective_role("MEMBER", "VIEWER", entitlements=OSS) == "MEMBER"
    assert ops.effective_role("VIEWER", "ADMIN", entitlements=OSS) == "VIEWER"
    assert ops.effective_role("NONE", "MEMBER", entitlements=OSS) == "NONE"


def test_effective_role_with_project_roles_overrides_up_and_down():
    assert ops.effective_role("MEMBER", "VIEWER", entitlements=EE) == "VIEWER"
    assert ops.effective_role("NONE", "MEMBER", entitlements=EE) == "MEMBER"
    assert ops.effective_role("ADMIN", None, entitlements=EE) == "ADMIN"
    assert ops.effective_role("ADMIN", "NONE", entitlements=EE) == "ADMIN"  # NONE = "no override"


def test_effective_role_rejects_unknown_roles():
    with pytest.raises(ValueError):
        ops.effective_role("SUPERUSER", None, entitlements=OSS)
    with pytest.raises(ValueError):
        ops.effective_role("VIEWER", "EDITOR", entitlements=EE)


@pytest.mark.parametrize("role, action, allowed", [
    ("VIEWER", "read_traces", True), ("VIEWER", "read_prompts", True), ("VIEWER", "create_scores", False),
    ("MEMBER", "edit_prompts", True), ("MEMBER", "create_scores", True), ("MEMBER", "delete_traces", False),
    ("ADMIN", "delete_traces", True), ("ADMIN", "manage_api_keys", True), ("ADMIN", "delete_project", False),
    ("OWNER", "delete_project", True), ("NONE", "read_traces", False)])
def test_can(role, action, allowed):
    assert ops.can(role, action) is allowed


def test_can_rejects_unknown_action_and_role():
    with pytest.raises(KeyError):
        ops.can("ADMIN", "launch_rockets")
    with pytest.raises(ValueError):
        ops.can("ROOT", "read_traces")


def test_least_role():
    assert ops.least_role([]) == "NONE"
    assert ops.least_role(["read_traces"]) == "VIEWER"
    assert ops.least_role(["read_traces", "edit_prompts"]) == "MEMBER"
    assert ops.least_role(("delete_traces", "read_prompts")) == "ADMIN"
    assert ops.least_role(["delete_project"]) == "OWNER"


# ---------------------------------------------------------------- lint_plan
def test_a_sound_plan_has_no_problems():
    assert ops.lint_plan(GOOD, entitlements=OSS) == []


def test_environment_names_follow_langfuse_rules():
    p = plan(projects__dev__environments={"Staging": 14, "langfuse-ci": 7, "x" * 41: 1, "ok_name-2": 3})
    problems = [x for x in ops.lint_plan(p, entitlements=OSS) if x.startswith("env-name:")]
    assert len(problems) == 3 and not any("ok_name-2" in x for x in problems)


def test_deployments_must_point_at_a_known_project_environment_and_tier():
    p = plan(deployments__api_x={"project": "nope", "environment": "production", "tier": "production", "keys": [],
                                 "needs": []})
    p["deployments"]["api@staging"]["environment"] = "qa"
    p["deployments"]["api@staging"]["tier"] = "test"
    problems = [x for x in ops.lint_plan(p, entitlements=OSS) if x.startswith("deploy:")]
    assert len(problems) == 3


def test_production_needs_its_own_project():
    p = plan()
    p["deployments"]["api@staging"].update(project="prod", environment="production", keys=["api-staging-2"])
    p["keys"]["api-staging-2"] = {"project": "prod", "permission": "INGEST", "owner": "platform", "rotate_days": 30}
    assert codes(ops.lint_plan(p, entitlements=OSS)) == ["prod-isolation"]


def test_key_problems():
    p = plan()
    p["deployments"]["api@staging"]["keys"] = ["api-prod-ingest", "ghost"]  # another project's key + unknown key
    p["keys"]["api-staging"].update(owner="nobody", rotate_days=None)
    p["keys"]["jobs-prod"]["rotate_days"] = 365
    p["keys"]["api-prod-read"]["permission"] = "SUPER"
    problems = [x for x in ops.lint_plan(p, entitlements=OSS) if x.startswith("key:")]
    text = " | ".join(problems)
    assert "ghost" in text and "nobody" in text and "SUPER" in text and "365" in text
    assert any("shared" in x and "api-prod-ingest" in x for x in problems)  # api@production + api@staging
    assert any("api@staging" in x and "api-prod-ingest" in x and "shared" not in x for x in problems)  # wrong project
    assert sum("rotates every" in x for x in problems) == 2


def test_keys_must_grant_what_the_deployment_needs():
    p = plan()
    p["deployments"]["api@production"]["keys"] = ["api-prod-ingest"]  # INGEST cannot read prompts
    p["keys"].pop("api-prod-read")
    assert codes(ops.lint_plan(p, entitlements=OSS)) == ["key-needs"]


def test_a_service_key_must_not_be_able_to_delete():
    p = plan()
    p["keys"]["api-prod-ingest"]["permission"] = "ADMIN"  # "Full access" for the app: a leaked key could wipe data
    problems = ops.lint_plan(p, entitlements=OSS)
    assert codes(problems) == ["key-delete"] and "api@production" in problems[0]


def test_one_organization_on_oss_over_privileges_people():
    p = plan(organizations={"org": {"projects": ["prod", "dev"]}})
    p["teams"] = {"engineers": {"roles": {"org": "MEMBER"}, "needs": {"prod": ["read_traces"], "dev": ["edit_prompts"]}},
                  "contractors": {"roles": {"org": "MEMBER"}, "needs": {"dev": ["create_scores"]}},
                  "platform": {"roles": {"org": "ADMIN"}, "needs": {"prod": ["manage_api_keys"],
                                                                    "dev": ["manage_members"]}}}
    problems = ops.lint_plan(p, entitlements=OSS)
    assert codes(problems) == ["team-excess"]
    assert len(problems) == 2  # engineers in prod (VIEWER would do), contractors in prod (NONE would do)
    assert any("contractors" in x and "prod" in x and "NONE" in x for x in problems)


def test_project_roles_fix_it_on_enterprise_but_are_flagged_on_oss():
    p = plan(organizations={"org": {"projects": ["prod", "dev"]}})
    p["teams"] = {"engineers": {"roles": {"org": "MEMBER"}, "project_roles": {"prod": "VIEWER"},
                                "needs": {"prod": ["read_traces"], "dev": ["edit_prompts"]}},
                  "platform": {"roles": {"org": "ADMIN"}, "needs": {"prod": ["manage_api_keys"],
                                                                    "dev": ["manage_members"]}}}
    assert ops.lint_plan(p, entitlements=EE) == []
    problems = ops.lint_plan(p, entitlements=OSS)
    assert "ee" in codes(problems) and "team-excess" in codes(problems)


def test_a_team_that_cannot_do_its_job():
    p = plan()
    p["teams"]["engineers"]["roles"]["org-dev"] = "VIEWER"  # needs edit_prompts in dev
    problems = ops.lint_plan(p, entitlements=OSS)
    assert codes(problems) == ["team-needs"] and "edit_prompts" in problems[0]


def test_enterprise_features_on_oss():
    p = plan(retention_by="native", protected_labels=["production"])
    assert sum(x.startswith("ee:") for x in ops.lint_plan(p, entitlements=OSS)) == 2
    assert ops.lint_plan(p, entitlements=EE) == []


def test_retention_values():
    p = plan(projects__dev__environments={"staging": 0, "ci": 2})
    assert [x for x in ops.lint_plan(p, entitlements=OSS) if x.startswith("retention:")] == \
        ["retention: dev/staging keeps 0 days"]
    p["retention_by"] = "native"
    assert len([x for x in ops.lint_plan(p, entitlements=EE) if x.startswith("retention:")]) == 2  # native: >= 3


def test_retention_policy_is_given():
    assert ops.retention_policy(GOOD) == {"production": 30, "staging": 14, "ci": 7, "*": None}


# ---------------------------------------------------------------- retention job
def test_expired_applies_the_policy_per_environment():
    rows = [{"trace_id": "a", "environment": "staging", "timestamp": NOW - timedelta(days=15)},
            {"trace_id": "b", "environment": "staging", "timestamp": NOW - timedelta(days=13)},
            {"trace_id": "c", "environment": "production", "timestamp": NOW - timedelta(days=15)},
            {"trace_id": "d", "environment": "sdk-lab", "timestamp": NOW - timedelta(days=2)},
            {"trace_id": "e", "environment": "other", "timestamp": NOW - timedelta(days=400)}]
    policy = {"production": 30, "staging": 14, "sdk-lab": 1, "*": None}
    assert ops.expired(rows, policy, now=NOW) == ["a", "d"]
    assert ops.expired(rows, {**policy, "*": 365}, now=NOW) == ["a", "d", "e"]
    assert ops.expired(rows, {"staging": 14}, now=NOW) == ["a"]  # no "*": keep everything else


def test_expired_uses_the_oldest_row_iso_strings_and_a_strict_boundary():
    rows = [{"trace_id": "t", "environment": "staging", "timestamp": (NOW - timedelta(days=1)).isoformat()},
            {"trace_id": "t", "environment": "staging", "timestamp": "2026-10-01T00:00:00Z"},
            {"trace_id": "u", "environment": "staging", "timestamp": (NOW - timedelta(days=14)).replace(tzinfo=None)}]
    assert ops.expired(rows, {"staging": 14}, now=NOW) == ["t"]  # u is exactly 14 days old: kept
    assert ops.expired(rows, {"staging": 14}, now=NOW.isoformat()) == ["t"]


# ---------------------------------------------------------------- delivery report
def row(batch, status, spans, tids):
    return {"batch": batch, "status": status, "spans": spans, "trace_ids": tids}


def test_delivery_report_healthy_batches():
    sent = {"trace_ids": ["t1", "t2", "t3", "t4"], "latencies_ms": [1.0, 4.0, 2.0, 3.0]}
    exports = [row("b1", 200, 6, ["t1", "t2"]), row("b2", 200, 6, ["t3", "t4"])]
    rep = ops.delivery_report(sent, exports, {"t1": 3, "t2": 3, "t3": 3, "t4": 3}, spans_per_request=3)
    assert rep == {"requests": 4, "p50_ms": 2.0, "p95_ms": 4.0, "max_ms": 4.0, "attempts": 2, "batches": 2,
                   "failed_attempts": 0, "dropped_batches": 0, "spans_exported": 12, "delivered": 4, "partial": [],
                   "lost": [], "ok": True}


def test_delivery_report_outage_retries_drops_and_losses():
    lat = [float(i) for i in range(1, 21)]  # 1..20 ms
    sent = {"trace_ids": ["t1", "t2", "t3", "t4", "t5"], "latencies_ms": lat}
    exports = [row("b1", 200, 6, ["t1", "t2"]),
               row("b2", 503, 6, ["t3", "t4"]), row("b2", 503, 6, ["t3", "t4"]), row("b2", 503, 6, ["t3", "t4"]),
               row("b3", 503, 3, ["t5"]), row("b3", 202, 3, ["t5"])]
    rep = ops.delivery_report(sent, exports, {"t1": 3, "t2": 2, "t5": 3}, spans_per_request=3)
    assert (rep["p50_ms"], rep["p95_ms"], rep["max_ms"]) == (10.0, 19.0, 20.0)
    assert (rep["attempts"], rep["batches"], rep["failed_attempts"], rep["dropped_batches"]) == (6, 3, 4, 1)
    assert rep["spans_exported"] == 9 and rep["delivered"] == 2
    assert rep["partial"] == ["t2"] and rep["lost"] == ["t3", "t4"] and rep["ok"] is False


def test_delivery_report_killed_job_exports_nothing():
    sent = {"trace_ids": ["k2", "k1"], "latencies_ms": [0.5, 0.7]}
    rep = ops.delivery_report(sent, [], {}, spans_per_request=3)
    assert rep["attempts"] == 0 and rep["batches"] == 0 and rep["spans_exported"] == 0
    assert rep["lost"] == ["k1", "k2"] and rep["delivered"] == 0 and rep["ok"] is False
    empty = ops.delivery_report({"trace_ids": [], "latencies_ms": []}, [], {}, spans_per_request=3)
    assert empty["p50_ms"] is None and empty["max_ms"] is None and empty["ok"] is True
