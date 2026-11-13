"""Module 13 TODO 2: your separation plan (`ops_plan.PLAN`) meets the team's requirements (its docstring) on THIS
edition. Needs TODO 1 (`ops.lint_plan`, `effective_role`, `can`)."""
import pytest

from langfuse_lab import ops_lab
from langfuse_mastery import ops
from langfuse_mastery.ops_plan import PLAN

PROD_DEPLOYMENT, STAGING_DEPLOYMENT = "support-api@production", "support-api@staging"
LAB_PROJECT_ID = "ch09-eval-project"


def entitlements():
    return ops_lab.ENTITLEMENTS[PLAN["edition"]]


def project_of(deployment):
    return PLAN["deployments"][deployment]["project"]


def role_in(team, project):
    org = next((o for o, v in PLAN["organizations"].items() if project in v.get("projects", [])), None)
    t = PLAN["teams"][team]
    return ops.effective_role(t.get("roles", {}).get(org, "NONE"), t.get("project_roles", {}).get(project),
                              entitlements=entitlements())


def test_the_plan_matches_this_stack_and_lints_clean():
    assert PLAN["edition"] == ops_lab.stack_edition()[0]
    assert ops.lint_plan(PLAN, entitlements=entitlements()) == []


def test_production_and_staging_deployments_live_in_separate_projects():
    deps = PLAN["deployments"]
    assert deps[PROD_DEPLOYMENT]["tier"] == "production" and deps[STAGING_DEPLOYMENT]["tier"] == "non-production"
    assert project_of(PROD_DEPLOYMENT) != project_of(STAGING_DEPLOYMENT)
    assert PLAN["projects"][project_of(PROD_DEPLOYMENT)]["id"] == LAB_PROJECT_ID  # the project this lab runs in
    assert deps[PROD_DEPLOYMENT]["environment"] != deps[STAGING_DEPLOYMENT]["environment"]


@pytest.mark.parametrize("deployment", [PROD_DEPLOYMENT, STAGING_DEPLOYMENT])
def test_the_service_sends_traces_with_scores_reads_its_prompt_and_cannot_delete(deployment):
    d = PLAN["deployments"][deployment]
    assert {"ingest", "create_scores", "read_prompts"} <= set(d["needs"])
    grants = set().union(*(ops.KEY_PERMISSIONS[PLAN["keys"][k]["permission"]] for k in d["keys"]))
    assert "delete_traces" not in grants


def test_the_ops_jobs_can_delete_in_production_only():
    jobs = [d for d in PLAN["deployments"].values() if "delete_traces" in d.get("needs", [])]
    assert jobs and all(d["tier"] == "production" for d in jobs)
    assert all(PLAN["keys"][k]["rotate_days"] <= 90 for d in jobs for k in d["keys"])


def test_people_get_what_they_need_and_nothing_more():
    prod, staging = project_of(PROD_DEPLOYMENT), project_of(STAGING_DEPLOYMENT)
    assert role_in("support-leads", prod) == "VIEWER" and role_in("support-leads", staging) == "NONE"
    assert role_in("contractors", prod) == "NONE" and ops.can(role_in("contractors", staging), "create_scores")
    assert role_in("ml-engineers", prod) == "VIEWER"
    assert all(ops.can(role_in("ml-engineers", staging), a) for a in ("edit_prompts", "edit_datasets",
                                                                      "edit_evaluators"))
    assert all(ops.can(role_in("platform", p), "manage_api_keys") for p in (prod, staging))
    assert not ops.can(role_in("platform", prod), "delete_project")  # nobody needs to delete the project day to day


def test_retention_follows_the_requirements_and_this_edition():
    policy = ops.retention_policy(PLAN)
    prod_env = PLAN["deployments"][PROD_DEPLOYMENT]["environment"]
    staging_env = PLAN["deployments"][STAGING_DEPLOYMENT]["environment"]
    assert policy[prod_env] == 30 and policy[staging_env] == 14
    assert policy.get("dev") == 7 and policy.get("ci") == 7 and policy.get(ops_lab.SDK_ENVIRONMENT) == 1
    assert PLAN["retention_by"] == ("job" if "data-retention" not in entitlements() else PLAN["retention_by"])
