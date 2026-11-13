"""Module 13 verify checks against fakes shaped like the live API (server 4.77 / SDK 5.2): observations as camelCase
dicts (environment, release, userId, tags, promptName/promptVersion), BOOLEAN scores with their environment, the
project with retention_days=None, the org endpoint's 403 body, Metrics API rows (`count_count`, `avg_value`), a real
OTLP tap log (JSONL rows with batch/status/spans/trace_ids/outage), the SDK workers' results and erasure receipts."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from langfuse_lab import ops_lab, traffic, verify
from langfuse_lab.tests.test_verify_m11 import Enum, Row
from langfuse_lab.verify import FeatureUnavailable, NotYet, _run_once, expected_delivery, module_13_checks

RUN = "m13-test"
TAG = f"run:{RUN}"
T0 = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
PROD, STAGING = "support-api@production", "support-api@staging"
SUBJECT = traffic.ops_subject(RUN)
STRAGGLER = "straggler-0"

PLAN = {
    "edition": "oss", "retention_by": "job", "retention_default_days": None, "protected_labels": [],
    "organizations": {"ch09-eval-org": {"projects": ["support-prod"]}, "acme-nonprod": {"projects": ["support-nonprod"]}},
    "projects": {"support-prod": {"id": "ch09-eval-project", "environments": {"production": 30}},
                 "support-nonprod": {"id": None, "environments": {"staging": 14, "dev": 7, "sdk-lab": 1}}},
    "deployments": {
        PROD: {"project": "support-prod", "environment": "production", "tier": "production", "release": "v13.0",
               "keys": ["prod-ingest", "prod-prompts"], "needs": ["ingest", "create_scores", "read_prompts"]},
        STAGING: {"project": "support-nonprod", "environment": "staging", "tier": "non-production",
                  "release": "v13.1-rc1", "keys": ["staging-ingest"], "needs": ["ingest", "create_scores"]},
        "ops-jobs@production": {"project": "support-prod", "environment": "production", "tier": "production",
                                "keys": ["jobs"], "needs": ["read_traces", "delete_traces"]}},
    "keys": {"prod-ingest": {"project": "support-prod", "permission": "INGEST", "owner": "platform", "rotate_days": 90},
             "prod-prompts": {"project": "support-prod", "permission": "VIEWER", "owner": "platform", "rotate_days": 90},
             "staging-ingest": {"project": "support-nonprod", "permission": "INGEST", "owner": "platform",
                                "rotate_days": 90},
             "jobs": {"project": "support-prod", "permission": "ADMIN", "owner": "platform", "rotate_days": 30}},
    "teams": {"platform": {"roles": {"ch09-eval-org": "ADMIN", "acme-nonprod": "ADMIN"}, "needs": {}}},
}


def agent_world(*, dry=False, env_override=None, prompt_v_staging=3):
    """6 requests sent (3 users x 2 deployments); the subject's 2 were erased, 4 kept with a generation + score."""
    rows, obs, scores = [], [], {}
    for i, c in enumerate(traffic.ops_cases(run_id=RUN)):
        tid = f"agent-{i}"
        rows.append({"trace_id": tid, "user_id": c.user_id, "deployment": c.deployment})
        if c.user_id == SUBJECT:
            continue
        dep = PLAN["deployments"][c.deployment]
        env = (env_override or {}).get(c.deployment, dep["environment"])
        t = T0 + timedelta(seconds=10 * i)
        tags = [TAG, "ops-lab", *c.tags] + (["dry-run"] if dry else [])
        common = {"traceId": tid, "userId": c.user_id, "environment": env, "version": dep["release"], "release": "",
                  "tags": tags, "projectId": "ch09-eval-project", "traceName": "support-request"}
        obs.append({**common, "id": f"r-{tid}", "name": "support-request", "type": "AGENT", "isRootObservation": True,
                    "startTime": t, "level": Enum("DEFAULT"), "promptName": None, "promptVersion": None})
        version = prompt_v_staging if c.deployment == STAGING else 3
        obs.append({**common, "id": f"g-{tid}", "name": "AzureChatOpenAI", "type": "GENERATION",
                    "isRootObservation": False, "startTime": t + timedelta(seconds=1), "level": Enum("DEFAULT"),
                    "promptName": "support-agent-system", "promptVersion": version})
        ok = c.case_id != "login_reset" or c.deployment == PROD
        scores[tid] = [{"id": f"hc-{tid}", "name": "handoff_correct", "value": ok, "source": Enum("API"),
                        "dataType": "BOOLEAN", "timestamp": t + timedelta(seconds=3), "environment": env}]
    return rows, obs, scores


def sdk_world(tmp_path, *, outage_loses=True, hardened_loses=False, tuned_batch=6, killed_exports=False,
              job_finish=True, slow_outage=False):
    """The 7 SDK scenarios: worker results, the tap log, and what Langfuse stored."""
    log, obs, sent = [], [], {}
    for sc in ops_lab.SDK_SCENARIOS:
        name, n = sc["name"], sc["n"]
        ids = [f"{name}-{i:02d}" for i in range(n)]
        lat = [40.0 + i for i in range(n)] if name == "flush-per-request" else \
            [30.0 + i % 3 for i in range(n)] if slow_outage and name == "outage" else [0.5 + 0.01 * i for i in range(n)]
        sent[name] = {"trace_ids": ids, "latencies_ms": lat, "exit": sc["exit"], "spans_per_request": 3}
        if name == "killed-job" and not killed_exports:
            continue
        per = {"defaults": n, "tuned": tuned_batch // 3, "flush-per-request": 1, "outage": 2, "outage-hardened": 2,
               "killed-job": n, "job": n}[name]
        stored = set(ids)
        for b, start in enumerate(range(0, n, per)):
            chunk = ids[start:start + per]
            row = {"batch": f"{name}-b{b}", "spans": 3 * len(chunk), "trace_ids": chunk, "outage": False}
            if name in ("outage", "outage-hardened") and b == 3:
                for _ in range(3):
                    log.append({**row, "status": 503, "outage": True})
                if (name == "outage" and outage_loses) or (name == "outage-hardened" and hardened_loses):
                    stored -= set(chunk)
                    continue
            if name == "job" and not job_finish:
                stored -= set(chunk)
                continue
            log.append({**row, "status": 200})
        for t in ids:
            if t in stored:
                common = {"traceId": t, "environment": "sdk-lab", "tags": [TAG, "ops-lab", f"sdk:{name}"],
                          "userId": f"sdk-{RUN}", "startTime": T0 + timedelta(minutes=5)}
                obs.append({**common, "id": f"r-{t}", "name": "support-request", "type": "AGENT",
                            "isRootObservation": True})
                obs += [{**common, "id": f"{k}-{t}", "name": k, "type": "SPAN", "isRootObservation": False}
                        for k in ("guardrail", "kb-search")]
    path = tmp_path / "otlp_exports.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in log), encoding="utf-8")
    by = {}
    for o in obs:
        by[o["traceId"]] = by.get(o["traceId"], 0) + 1
    sdk = {name: {"sent": s, "report": expected_delivery(s, [r for r in log if set(r["trace_ids"]) & set(s["trace_ids"])],
                                                         {t: by.get(t, 0) for t in s["trace_ids"]}, 3)}
           for name, s in sent.items()}
    return sdk, path, obs


class FakeMetrics:
    """Counts roots and averages BOOLEAN scores, with the filters module 13 uses."""

    def __init__(self, obs, scores):
        self.obs, self.scores = obs, scores

    def metrics(self, query):
        q = json.loads(query)
        frm, to = (datetime.fromisoformat(q[k]) for k in ("fromTimestamp", "toTimestamp"))
        roots = {o["traceId"]: o for o in self.obs if o.get("isRootObservation")}
        if q["view"] == "observations":
            rows = [o for o in self.obs if frm <= o["startTime"] <= to]
        else:
            rows = [{**s, "tags": roots[t]["tags"], "value": float(s["value"])} for t, ss in self.scores.items()
                    for s in ss if frm <= s["timestamp"] <= to]

        def ok(r, f):
            v = r.get(f["column"])
            if f["type"] == "arrayOptions":
                return bool(set(v or []) & set(f["value"]))
            return v == f["value"]

        rows = [r for r in rows if all(ok(r, f) for f in q["filters"])]
        m = q["metrics"][0]
        if m["aggregation"] == "count":
            return NS(data=[{"count_count": len(rows)}])
        vals = [r["value"] for r in rows]
        return NS(data=[{"avg_value": sum(vals) / len(vals) if vals else None}])


class Fake:
    def __init__(self, obs, scores, *, retention_days=None, left_for_subject=()):
        self.obs, self.scores = obs, scores
        self.left = list(left_for_subject)

        def forbidden():
            err = Exception("403")
            err.status_code = 403
            err.body = {"error": 'Access denied. The "projects:read" permission is required to access this resource.'}
            raise err

        def get_many(fields=None, filter=None, limit=None, cursor=None, trace_id=None):
            flt = json.loads(filter) if filter else []
            if any(f["column"] == "userId" for f in flt):
                return NS(data=[Row(o) for o in self.left], meta=NS(cursor=None))
            return NS(data=[Row(o) for o in self.obs], meta=NS(cursor=None))

        self.api = NS(
            observations=NS(get_many=get_many),
            scores=NS(get_many=lambda trace_id, fields=None, limit=100: NS(
                data=[Row(s) for s in self.scores.get(trace_id, [])], meta=NS(cursor=None))),
            projects=NS(get=lambda: NS(data=[NS(id="ch09-eval-project", name="CH09", retention_days=retention_days)])),
            organizations=NS(get_organization_projects=forbidden),
            metrics=FakeMetrics(obs, scores),
        )


def erasure_receipts(rows, *, straggler_in_pass=2, extra=()):
    mine = [r["trace_id"] for r in rows if r["user_id"] == SUBJECT]
    passes = [{"user_id": SUBJECT, "trace_ids": sorted(mine + (["straggler-0"] if straggler_in_pass == 1 else [])
                                                       + list(extra)), "seconds": 128.0}]
    if straggler_in_pass == 2:
        passes.append({"user_id": SUBJECT, "trace_ids": [STRAGGLER], "seconds": 121.0})
    return {"subject": SUBJECT, "passes": passes, "straggler": [STRAGGLER], "final": []}


def retention_for(obs, plan=PLAN, days=15):
    now = T0 + timedelta(days=days)
    policy = {env: d for p in plan["projects"].values() for env, d in p["environments"].items()}
    policy["*"] = plan["retention_default_days"]
    expired = sorted(o["traceId"] for o in obs if o.get("isRootObservation")
                     and policy.get(o["environment"], policy["*"]) is not None
                     and (now - o["startTime"]).total_seconds() > policy[o["environment"]] * 86400)
    return {"now": now.isoformat(), "policy": policy, "expired": expired}


@pytest.fixture(autouse=True)
def no_staging_keys(monkeypatch):
    for k in ops_lab.STAGING_KEY_ENV:
        monkeypatch.delenv(k, raising=False)


def world(tmp_path, *, plan=None, agent=None, sdk_kw=None, erasure=None, retention=None, fake_kw=None, **kw):
    rows, aobs, scores = agent or agent_world()
    sdk, tap, sobs = sdk_world(tmp_path, **(sdk_kw or {}))
    obs = aobs + sobs
    client = Fake(obs, scores, **(fake_kw or {}))
    args = dict(plan=plan or PLAN, traffic=rows, sdk=sdk, tap_log=tap,
                erasure=erasure if erasure is not None else erasure_receipts(rows),
                retention=retention if retention is not None else retention_for(obs))
    args.update(kw)
    return {c.name: c for c in module_13_checks(TAG, client, **args)}, args


def result(checks, prefix):
    return _run_once(next(c for name, c in checks.items() if name.startswith(prefix)))


# ---------------------------------------------------------------- happy path
def test_a_correct_run_passes_with_the_enterprise_items_unavailable(tmp_path):
    checks, _ = world(tmp_path)
    got = {name: _run_once(c) for name, c in checks.items()}
    for name, r in got.items():
        print(r.status, name, r.detail)
    assert [r.status for r in got.values()] == ["pass"] * 8 + ["unavailable"] * 3
    org = result(checks, "organization API")
    assert "403" in org.detail and "projects:read" in org.detail and "admin-api" in org.detail
    assert "retention_days=None" in result(checks, "native data retention").detail
    assert "Ingestion" in result(checks, "staging project").detail
    assert "support-agent-system v3" in result(checks, "deployments").detail


# ---------------------------------------------------------------- traffic, separation, plan
def test_dry_traffic_is_refused_unless_allowed(tmp_path):
    checks, _ = world(tmp_path, agent=agent_world(dry=True))
    assert "dry-run" in result(checks, "deployments").detail
    checks, _ = world(tmp_path, agent=agent_world(dry=True), allow_dry=True)
    assert result(checks, "deployments").status == "pass"


def test_traffic_in_the_wrong_environment_and_prompts_not_shared(tmp_path):
    checks, _ = world(tmp_path, agent=agent_world(env_override={STAGING: "dev"}))
    r = result(checks, "deployments")
    assert r.status == "fail" and "the plan says 'staging'" in r.detail
    checks, _ = world(tmp_path, agent=agent_world(prompt_v_staging=4))
    assert "same prompt version" in result(checks, "deployments").detail


def test_separation_compares_the_metrics_api_with_stored_data(tmp_path):
    checks, _ = world(tmp_path)
    r = result(checks, "environment separation")
    assert r.status == "pass" and "production: 2 requests, pass rate 1" in r.detail and "staging: 2" in r.detail


def test_plan_must_match_the_live_stack(tmp_path):
    p = copy.deepcopy(PLAN)
    p["projects"]["support-prod"]["id"] = "some-other-project"
    p["retention_by"] = "native"
    p["deployments"][STAGING]["project"] = "support-prod"
    p["projects"]["support-prod"]["environments"]["staging"] = 14
    r = result(world(tmp_path, plan=p)[0], "the plan matches")
    assert r.status == "fail"
    assert "live project is 'ch09-eval-project'" in r.detail and "host production and non-production" in r.detail
    assert "native" in r.detail
    p = copy.deepcopy(PLAN)
    p["projects"]["support-nonprod"]["environments"].pop("sdk-lab")
    assert "['sdk-lab']" in result(world(tmp_path, plan=p)[0], "the plan matches").detail


# ---------------------------------------------------------------- SDK lab
def test_sdk_reports_must_match_the_tap_log(tmp_path):
    checks, args = world(tmp_path)
    args["sdk"]["tuned"]["report"]["batches"] = 3
    args["sdk"]["defaults"]["report"].pop("p95_ms")
    r = result(checks, "SDK batching")
    assert r.status == "fail" and "tuned: batches = 3" in r.detail and "report['p95_ms'] missing" in r.detail


def test_sdk_tuned_must_actually_batch_smaller(tmp_path):
    checks, _ = world(tmp_path, sdk_kw={"tuned_batch": 60})  # flush_at ignored: one big batch
    r = result(checks, "SDK batching")
    assert r.status == "fail" and "exceed flush_at" in r.detail


def test_outage_happy_and_failures(tmp_path):
    assert result(world(tmp_path)[0], "planted outage").status == "pass"
    checks, args = world(tmp_path)
    args["sdk"]["outage"]["report"]["lost"] = []
    assert "your report lists lost []" in result(checks, "planted outage").detail
    assert "re-run" in result(world(tmp_path, sdk_kw={"outage_loses": False})[0], "planted outage").detail
    assert "timeout" in result(world(tmp_path, sdk_kw={"hardened_loses": True})[0], "planted outage").detail
    assert "slowed down" in result(world(tmp_path, sdk_kw={"slow_outage": True})[0], "planted outage").detail


def test_killed_job_and_finish(tmp_path):
    r = result(world(tmp_path)[0], "planted killed job")
    assert r.status == "pass" and "0 exports" in r.detail
    assert "nothing should leave" in result(world(tmp_path, sdk_kw={"killed_exports": True})[0],
                                            "planted killed job").detail
    assert "finish()" in result(world(tmp_path, sdk_kw={"job_finish": False})[0], "planted killed job").detail


# ---------------------------------------------------------------- erasure
def test_erasure_waits_for_the_async_deletion(tmp_path):
    left = [{"traceId": STRAGGLER, "userId": SUBJECT, "id": "x"}]
    with pytest.raises(NotYet):
        world(tmp_path, fake_kw={"left_for_subject": left})[0][
            "GDPR erasure + planted straggler: nothing of the subject left, nobody else touched"].fn()


def test_erasure_must_catch_the_straggler_and_spare_everyone_else(tmp_path):
    rows = agent_world()[0]
    r = result(world(tmp_path, erasure=erasure_receipts(rows, straggler_in_pass=0))[0], "GDPR")
    assert r.status == "fail" and "no later pass deleted it" in r.detail
    r = result(world(tmp_path, erasure=erasure_receipts(rows, straggler_in_pass=1))[0], "GDPR")
    assert "AFTER the erasure" in r.detail
    other = next(x["trace_id"] for x in rows if x["user_id"] != SUBJECT)
    r = result(world(tmp_path, erasure=erasure_receipts(rows, extra=[other]))[0], "GDPR")
    assert "another user's trace" in r.detail
    bad = dict(erasure_receipts(rows), subject="cust_02")
    assert "this run's customer" in result(world(tmp_path, erasure=bad)[0], "GDPR").detail


# ---------------------------------------------------------------- retention
def test_retention_dry_run_must_follow_the_plan(tmp_path):
    checks, args = world(tmp_path)
    r = result(checks, "retention job")
    assert r.status == "pass" and "'staging': 2" in r.detail and "'production': 2" in r.detail
    wrong = dict(args["retention"], expired=args["retention"]["expired"][:-1])
    assert "missing" in result(world(tmp_path, retention=wrong)[0], "retention job").detail
    other = dict(args["retention"], policy={"*": 1})
    assert "not the plan's" in result(world(tmp_path, retention=other)[0], "retention job").detail


# ---------------------------------------------------------------- optional staging project
def test_staging_project_with_an_ingestion_key(tmp_path, monkeypatch):
    for k in ops_lab.STAGING_KEY_ENV:
        monkeypatch.setenv(k, "pk-or-sk")
    plan = copy.deepcopy(PLAN)
    plan["projects"]["support-nonprod"]["id"] = "staging-project"
    state = {"can_read": False}

    def denied(**kw):
        if state["can_read"]:
            return NS(data=[])
        err = Exception("403")
        err.status_code = 403
        raise err

    monkeypatch.setattr(verify, "_api_with", lambda pk, sk: NS(
        projects=NS(get=lambda: NS(data=[NS(id="staging-project", name="support-nonprod")])),
        observations=NS(get_many=denied)))
    rows, aobs, scores = agent_world()
    sdk, tap, sobs = sdk_world(tmp_path)
    with tap.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"batch": "st-b0", "status": 200, "spans": 9, "trace_ids": ["st-0", "st-1", "st-2"],
                            "outage": False}) + "\n")
    obs = aobs + sobs
    args = dict(plan=plan, traffic=rows, sdk=sdk, tap_log=tap, erasure=erasure_receipts(rows),
                retention=retention_for(obs, plan), staging={"trace_ids": ["st-0", "st-1", "st-2"]})
    check = next(c for c in module_13_checks(TAG, Fake(obs, scores), **args) if c.name.startswith("staging"))
    r = _run_once(check)
    assert r.status == "pass" and "cannot read (403)" in r.detail
    state["can_read"] = True
    assert "can READ" in _run_once(check).detail
    plan["projects"]["support-nonprod"]["id"] = None
    state["can_read"] = False
    assert "plan's staging project id is None" in _run_once(check).detail


def test_missing_inputs_say_what_to_pass(tmp_path):
    checks, _ = world(tmp_path, retention={}, erasure={})
    r = result(checks, "retention job")
    assert r.status == "fail" and "pass retention=" in r.detail
    with pytest.raises(FeatureUnavailable):
        checks["native data retention"].fn()
