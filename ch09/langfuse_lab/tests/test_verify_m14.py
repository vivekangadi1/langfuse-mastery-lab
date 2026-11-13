"""Module 14 verify checks against a fake world shaped like the live API (server 4.77 / SDK 5.2): camelCase observation
rows (input as a JSON string, version prompt-v<n>, tags), BOOLEAN judge scores (stand-in source API) and ANNOTATION
labels with configId/queueId, prompt versions with their own id + labels, PROMPT comments as the audit log, queue
items with completedAt, dataset items with sourceTraceId/version, experiments + items (expectedOutput as a JSON string,
handoff_correct item scores) + run-level ci_gate scores, signed agent-alert deliveries, tick reports, the learner's
queue rows / items / releases and an evidence.json written by loop_spec.write_report."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from langfuse_lab import loop_spec, prompts, regression, traffic
from langfuse_lab.verify import FeatureUnavailable, NotYet, _run_once, module_14_checks

TAG = "run:m14-test"
INC = loop_spec.incident_id("m14-test")
SECRET = "lf-whsec_loop"
NAMES = loop_spec.names("")
T0 = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
WIN = {"baseline": (T0, T0 + timedelta(seconds=60)), "incident": (T0 + timedelta(seconds=120), T0 + timedelta(seconds=180)),
       "recovered": (T0 + timedelta(seconds=600), T0 + timedelta(seconds=660))}
GOOD, BAD, QUICK, FIX = 1, 2, 4, 5
SERVED = {"baseline": GOOD, "incident": BAD, "recovered": FIX}
UI = "http://localhost:3000/project/p"
POLICY = {"reruns": 1, "max_rerun_items": 3, "max_regressions": 0, "max_drop": 0.1}


def at(seconds) -> datetime:
    return T0 + timedelta(seconds=seconds)


def iso(d) -> str:
    return d.isoformat()


class Row:
    def __init__(self, d):
        self._d = d

    def dict(self):
        return dict(self._d)


class NotFoundError(Exception):
    status_code = 404


MSG = {c: m for c, _u, m, _e in traffic.LOOP_DEMO}
EXPECTED = {c: e for c, _u, _m, e in traffic.LOOP_DEMO}
LOGIN = traffic.LOOP_INCIDENT_CASES
INCIDENT_ITEM = {c: regression.dataset_item_id(NAMES["dataset"], MSG[c]) for c in LOGIN}
SEED = [it["id"] for it in regression.seed_items(NAMES["dataset"])]
SEED_DC = regression.dataset_item_id(NAMES["dataset"], "I was charged twice for order 8129. Please fix it.")


def signed_row(body: dict, received, *, status=200, secret=SECRET):
    text = json.dumps(body, separators=(",", ":"), sort_keys=True)
    ts = int(received.timestamp())
    return {"path": loop_spec.ALERT_PATH, "status": status, "duplicate": False, "planted_outage": False,
            "signature_ok": True, "received_at": iso(received), "body": text,
            "headers": {"x-langfuse-signature": regression.sign(text, secret, ts)}}


def build(*, dry=False, judge_env="production", incident_misroutes=True, quick_ships=False, quick_promoted=False,
          drop_flagged_from_queue=False, pending=False, wrong_expected=False, alert_value=None, extra_queue_trace=False,
          resolved=True, evidence_alert_id=None, skip_promote_audit=False):
    obs, scores = [], {}
    for w, (start, _) in WIN.items():
        for i, case in enumerate(("login_reset", "locked_out", "double_charge")):
            tid = f"{w}-{case}"
            wrong = w == "incident" and case in LOGIN and incident_misroutes
            handoff = "billing_specialist" if wrong else EXPECTED[case]
            t = start + timedelta(seconds=5 + 10 * i)
            obs.append({"id": f"o-{tid}", "traceId": tid, "name": loop_spec.ROOT_NAME, "isRootObservation": True,
                        "startTime": t, "version": f"prompt-v{SERVED[w]}", "environment": "production",
                        "input": json.dumps(MSG[case]), "metadata": {"handoff_target": handoff},
                        "tags": [TAG, NAMES["tag"], case, f"window:{w}", *(["dry-run"] if dry else [])]})
            scores[tid] = [{"id": f"j-{tid}", "name": NAMES["judge"], "value": not wrong, "source": "API",
                            "dataType": "BOOLEAN", "timestamp": t + timedelta(seconds=2), "environment": judge_env,
                            "metadata": {"evaluator": "stand-in:keyword-v1"}, "comment": "[stand-in]"}]
    configs = [{"id": f"c-{r}", "name": NAMES[r], "isArchived": False} for r in ("human", "error_type", "expected")]
    queue = {"id": "q1", "name": NAMES["queue"], "scoreConfigIds": [c["id"] for c in configs]}
    queued_cases = [c for c in ("login_reset", "locked_out") if not drop_flagged_from_queue or c != "locked_out"]
    queued_cases.append("double_charge")
    q_items = []
    for k, case in enumerate(queued_cases):
        tid = f"incident-{case}"
        q_items.append({"id": f"qi{k}", "objectId": tid, "objectType": "TRACE",
                        "status": "PENDING" if pending and k == 0 else "COMPLETED",
                        "createdAt": at(200), "completedAt": None if pending and k == 0 else at(290 + k)})
        ok = case not in LOGIN or not incident_misroutes
        labels = [("human", 1.0 if ok else 0.0)]
        if not ok:
            labels += [("expected", EXPECTED[case]), ("error_type", "wrong_team")]
        for role, value in labels:
            scores[tid].append({"id": f"{tid}-{role}", "name": NAMES[role], "value": value, "source": "ANNOTATION",
                                "configId": f"c-{role}", "queueId": "q1", "timestamp": at(285 + k),
                                "environment": "production", "authorUserId": None})
    if extra_queue_trace:
        q_items.append({"id": "qx", "objectId": "baseline-login_reset", "objectType": "TRACE", "status": "COMPLETED",
                        "createdAt": at(200), "completedAt": at(299)})
    ds = {"id": "ds1", "name": NAMES["dataset"]}
    d_items = {}
    for case in LOGIN:
        if not incident_misroutes:
            continue
        iid = INCIDENT_ITEM[case]
        d_items[iid] = {"id": iid, "status": "ACTIVE", "sourceTraceId": f"incident-{case}",
                        "expectedOutput": {"handoff": "shipping_specialist" if wrong_expected else EXPECTED[case],
                                           "reference_answer": None},
                        "metadata": {"incident": INC, "source": "incident", "case_id": f"{INC}:x"},
                        "createdAt": at(320), "version": at(320)}
    pin = at(320)
    item_ids = SEED + list(INCIDENT_ITEM.values())
    expect = {i: "billing_specialist" if i == SEED_DC else "none" for i in SEED}
    expect.update({INCIDENT_ITEM[c]: "account_security" for c in LOGIN})

    def exp(eid, role, label, version, start, extra=None):
        return {"id": eid, "name": f"ci hotfix · {role} {label} v{version} · abc1234 · run", "startTime": start,
                "itemCount": len(item_ids), "datasetId": "ds1",
                "metadata": {"run_tag": TAG, "role": role, "prompt_label": label, "prompt_version": str(version),
                             "dataset_version": iso(pin), "dry_run": "true" if dry else "false", **(extra or {})}}

    exps = [exp("eb", "baseline", "production", GOOD, at(330)),
            exp("eq", "candidate", "hotfix", QUICK, at(345), {"baseline_experiment_id": "eb"}),
            exp("rqb", "rerun", "production", GOOD, at(380), {"rerun_of": "eq", "side": "baseline", "attempt": "1"}),
            exp("rqc", "rerun", "hotfix", QUICK, at(385), {"rerun_of": "eq", "side": "candidate", "attempt": "1"}),
            exp("ef", "candidate", "hotfix", FIX, at(460), {"baseline_experiment_id": "eb"})]

    def rows(eid, fail=(), only=None):
        out = []
        for i in item_ids if only is None else only:
            out.append({"id": f"{eid}-{i}", "experimentItemId": i, "expectedOutput": json.dumps({"handoff": expect[i]}),
                        "scores": [{"name": NAMES["config"], "value": i not in fail, "timestamp": iso(at(340))}]})
        return out

    quick_fail = () if quick_ships else (SEED_DC,)
    exp_items = {"eb": rows("eb"), "eq": rows("eq", quick_fail), "rqb": rows("rqb", only=[SEED_DC]),
                 "rqc": rows("rqc", quick_fail, only=[SEED_DC]), "ef": rows("ef")}
    if quick_ships:
        exp_items["rqb"] = exp_items["rqc"] = []
        exps = [e for e in exps if e["id"] not in ("rqb", "rqc")]
    gate = {"eq": [{"name": "ci_gate", "value": "ship" if quick_ships else "block", "timestamp": at(400),
                    "comment": "1 regressed item(s) > 0 allowed", "metadata": {"policy": POLICY}}],
            "ef": [{"name": "ci_gate", "value": "ship", "timestamp": at(500), "comment": "no confirmed regressions",
                    "metadata": {"policy": POLICY}}]}
    labels = {GOOD: [], BAD: ["staging"], 3: ["candidate"], QUICK: ["production"] if quick_promoted else [],
              FIX: ["hotfix", "latest"] + ([] if quick_promoted else ["production", "live"])}
    templates = {GOOD: prompts.SYSTEM_PROMPT_TEMPLATE, BAD: prompts.WRONG_HANDOFF_TEMPLATE,
                 3: prompts.CLEAN_CANDIDATE_TEMPLATE, QUICK: prompts.QUICK_FIX_TEMPLATE, FIX: prompts.LOOP_FIX_TEMPLATE}
    versions = {v: {"id": f"pv{v}", "prompt": t, "labels": labels[v], "createdAt": at(-60)} for v, t in templates.items()}
    audit = [(GOOD, "start", "production + live -> v1: start", at(-10)), (BAD, "deploy", "live -> v2: no gate", at(100)),
             (QUICK, "candidate", "hotfix -> v4", at(340)), (FIX, "candidate", "hotfix -> v5", at(450))]
    if not skip_promote_audit:
        audit.append((FIX, "promote", "production + live -> v5", at(520)))
    comments = {}
    for v, action, text, when in audit:
        comments.setdefault(f"pv{v}", []).append({"id": f"cm-{action}-{v}", "content": loop_spec.audit_text(TAG, action, text),
                                                  "createdAt": when})

    def win(w):
        return {"from": iso(WIN[w][0]), "to": iso(WIN[w][1])}

    fp = "fp-judge"
    firing = {"type": "agent-alert", "id": f"{fp}:firing:1", "status": "firing", "rule": loop_spec.JUDGE_RULE,
              "metric": f"score.{NAMES['judge']}.API", "baseline": {"value": 1.0, **win("baseline")},
              "current": {"value": (1 / 3) if alert_value is None else alert_value, **win("incident")},
              "version": "prompt-v2", "fingerprint": fp, "scope": {"environment": "production", "tags": [TAG]},
              "title": "[FIRING critical] judge-routing-drop"}
    res = {**firing, "id": f"{fp}:resolved:2", "status": "resolved", "current": {"value": 1.0, **win("recovered")},
           "title": "[RESOLVED critical] judge-routing-drop"}
    deliveries = [signed_row(firing, at(190))] + ([signed_row(res, at(670))] if resolved else [])
    ticks = {"fire": {"baseline": win("baseline"), "current": win("incident"), "alerts": [{"rule": loop_spec.JUDGE_RULE}],
                      "sent": [{"payload": firing, "delivery": {"ok": True}}]},
             "resolve": {"baseline": win("baseline"), "current": win("recovered"), "alerts": [],
                         "sent": [{"payload": res, "delivery": {"ok": True}}] if resolved else []}}
    queued = [{"trace_id": i["objectId"], "reason": "control" if "double" in i["objectId"] else "routing_judge=False"}
              for i in q_items]
    items = [{"id": i} for i in d_items]
    releases = {"quick": {"version": QUICK, "exit_code": 0 if quick_ships else 1, "promoted": quick_promoted},
                "fix": {"version": FIX, "exit_code": 0, "promoted": True}}
    world = dict(obs=obs, scores=scores, configs=configs, queue=queue, q_items=q_items, ds=ds, d_items=d_items,
                 exps=exps, exp_items=exp_items, gate=gate, versions=versions, comments=comments)
    events = evidence_events(world, firing, res, alert_id=evidence_alert_id)
    return world, dict(deliveries=deliveries, ticks=ticks, queued=queued, items=items, pin=pin, releases=releases,
                       events=events)


def evidence_events(w, firing, res, *, alert_id=None):
    def ev(stage, when, numbers, refs=None, links=None):
        return {"stage": stage, "at": iso(when), "title": stage, "numbers": numbers, "refs": refs or {},
                "links": links or {}}

    flagged = ["incident-login_reset", "incident-locked_out"]
    out = [ev("baseline", at(5), {"requests": 3, "judge_pass_rate": 1.0, "version": "prompt-v1"}, links={"prompt": UI}),
           ev("deploy", at(100), {"from_version": GOOD, "to_version": BAD}, links={"prompt": UI}),
           ev("judge", at(147), {"requests": 3, "judge_pass_rate": 1 / 3, "flagged": 2}, refs={"trace_ids": flagged},
              links={t: f"{UI}/traces/{t}" for t in flagged}),
           ev("alert", at(190), {"rule": firing["rule"], "metric": firing["metric"], "baseline": 1.0, "current": 1 / 3,
                                 "version": "prompt-v2"}, refs={"id": alert_id or firing["id"]}),
           ev("review", at(292), {"queued": len(w["q_items"]), "flagged": 2, "controls": 1, "confirmed_failures": 2,
                                  "false_alarms": 0}, refs={"queue_id": "q1", "trace_ids": []}, links={"queue": UI}),
           ev("dataset", at(320), {"items": 2, "pinned_version": iso(at(320))},
              refs={"dataset_id": "ds1", "item_ids": list(w["d_items"])}, links={"dataset": UI}),
           ev("promote", at(520), {"version": FIX, "labels": ["production", "live"]}, links={"prompt": UI}),
           ev("recovered", at(605), {"requests": 3, "judge_pass_rate": 1.0, "version": "prompt-v5"}, links={"prompt": UI}),
           ev("resolved", at(670), {"rule": res["rule"], "current": 1.0}, refs={"id": res["id"]})]
    for kind, eid, version, fix_at, decision in (("quick", "eq", QUICK, 340, "block"), ("fix", "ef", FIX, 450, "ship")):
        out += [ev("fix", at(fix_at), {"kind": kind, "version": version}, links={"prompt": UI}),
                ev("experiment", at(fix_at + 5), {"kind": kind, "candidate_version": version, "baseline_version": GOOD,
                                                  "items": 9},
                   refs={"candidate_experiment_id": eid, "baseline_experiment_id": "eb"},
                   links={"candidate": f"{UI}/x?baseline={eid}", "baseline": f"{UI}/x?baseline=eb"}),
                ev("gate", at(400 if kind == "quick" else 500), {"kind": kind, "version": version, "decision": decision,
                                                                 "exit_code": 0 if decision == "ship" else 1,
                                                                 "regressed": []}, refs={"experiment_id": eid})]
    return out


class Fake:
    def __init__(self, w):
        def scores_get_many(fields=None, limit=100, trace_id=None, experiment_id=None):
            data = w["scores"].get(trace_id, []) if trace_id else w["gate"].get(experiment_id, [])
            return NS(data=[Row(s) for s in data], meta=NS(cursor=None))

        def ds_get(name):
            if name != w["ds"]["name"]:
                raise NotFoundError(name)
            return Row(w["ds"])

        def item_get(iid):
            if iid not in w["d_items"]:
                raise NotFoundError(iid)
            return Row(w["d_items"][iid])

        def exp_list(from_start_time=None, dataset_id=None, fields=None, limit=100, cursor=None):
            return NS(data=[Row(e) for e in w["exps"]], meta=NS(cursor=None))

        def exp_items(from_start_time=None, experiment_id=None, fields=None, limit=100, cursor=None):
            return NS(data=[Row(i) for i in w["exp_items"].get(experiment_id, [])], meta=NS(cursor=None))

        self.api = NS(
            observations=NS(get_many=lambda **kw: NS(data=[Row(o) for o in w["obs"]], meta=NS(cursor=None))),
            scores=NS(get_many=scores_get_many),
            prompts=NS(list=lambda name=None, limit=100: NS(data=[NS(name=name, versions=sorted(w["versions"]))]),
                       get=lambda name, version=None: Row(w["versions"][version])),
            comments=NS(get=lambda object_type=None, object_id=None, limit=100: NS(
                data=[Row(c) for c in w["comments"].get(object_id, [])])),
            annotation_queues=NS(
                list_queues=lambda page=1, limit=100: NS(data=[Row(w["queue"])], meta=NS(total_pages=1)),
                list_queue_items=lambda queue_id, page=1, limit=100: NS(data=[Row(i) for i in w["q_items"]],
                                                                       meta=NS(total_pages=1))),
            datasets=NS(get=ds_get),
            dataset_items=NS(get=item_get),
            experiments=NS(list=exp_list, list_items=exp_items),
            score_configs=NS(get=lambda page=1, limit=100: NS(data=[Row(c) for c in w["configs"]], meta=NS(total_pages=1))),
        )


def world(tmp_path, *, evidence_problems=(), durations=None, **kw):
    w, args = build(**kw)
    events = args.pop("events")
    evidence = {"incident": INC, "run_tag": TAG, "prompt": NAMES["prompt"], "environment": "production",
                "judge_mode": "stand-in", "events": events, "problems": list(evidence_problems),
                "durations": durations or {"time_to_detect_s": 90.0, "time_to_review_s": 102.0, "time_to_fix_s": 330.0,
                                           "time_to_recover_s": 570.0}}
    paths = loop_spec.write_report(evidence, tmp_path / "out")
    checks = module_14_checks(TAG, Fake(w), alert_secret=SECRET, evidence=paths["evidence"], **args)
    return {c.name: c for c in checks}, w, args


def result(checks, prefix):
    return _run_once(next(c for name, c in checks.items() if name.startswith(prefix)))


@pytest.fixture(autouse=True)
def blocked_stack(monkeypatch):
    from langfuse_lab import alert_spec
    monkeypatch.setattr(alert_spec, "native_webhooks_ready", lambda *a, **k: (False, "no whitelist"))


# ---------------------------------------------------------------- happy path
def test_a_correct_loop_passes_with_native_webhooks_unavailable(tmp_path):
    checks, _, _ = world(tmp_path)
    got = {name: _run_once(c) for name, c in checks.items()}
    for name, r in got.items():
        print(r.status, name, r.detail)
    assert [r.status for r in got.values()] == ["pass"] * 11 + ["unavailable"]
    assert "BLOCKED" in result(checks, "planted").detail and "billing_specialist" in result(checks, "planted").detail
    assert "v1 (approved)" in result(checks, "traffic").detail
    assert "stand-in" in result(checks, "live traffic judged").detail


def test_dry_traffic_is_refused_unless_allowed(tmp_path):
    checks, _, _ = world(tmp_path, dry=True)
    r = result(checks, "traffic")
    assert r.status == "fail" and "dry-run" in r.detail
    w, args = build(dry=True)
    args.pop("events")
    checks = {c.name: c for c in module_14_checks(TAG, Fake(w), alert_secret=SECRET, allow_dry=True, **args)}
    assert result(checks, "traffic").status == "pass"


# ---------------------------------------------------------------- judge, alert
def test_judge_scores_outside_the_environment_cannot_page(tmp_path):
    checks, _, _ = world(tmp_path, judge_env="default")
    r = result(checks, "live traffic judged")
    assert r.status == "fail" and "environment" in r.detail and "default" in r.detail


def test_a_deploy_the_judge_never_flags_is_not_a_pass(tmp_path):
    checks, _, _ = world(tmp_path, incident_misroutes=False)
    r = result(checks, "live traffic judged")
    assert r.status == "fail" and "flagged nothing" in r.detail


def test_alert_numbers_must_match_the_stored_judge_pass_rates(tmp_path):
    checks, _, _ = world(tmp_path, alert_value=0.5)
    r = result(checks, "the alert fires")
    assert r.status == "fail" and "stored judge pass rate of incident" in r.detail


def test_a_forged_alert_signature_fails(tmp_path):
    w, args = build()
    args.pop("events")
    checks = {c.name: c for c in module_14_checks(TAG, Fake(w), alert_secret="lf-whsec_other", **args)}
    assert "signature" in result(checks, "the alert fires").detail


# ---------------------------------------------------------------- queue, review, dataset
def test_every_judge_flag_must_be_queued_and_nothing_outside_the_window(tmp_path):
    checks, _, _ = world(tmp_path, drop_flagged_from_queue=True)
    r = result(checks, "review queue")
    assert r.status == "fail" and "not queued" in r.detail
    checks, _, _ = world(tmp_path, extra_queue_trace=True)
    assert "outside the alert's window" in result(checks, "review queue").detail


def test_a_pending_review_is_waited_for(tmp_path):
    checks, _, _ = world(tmp_path, pending=True)
    r = result(checks, "human review")
    assert r.status == "fail" and r.retryable and "PENDING" in r.detail


def test_dataset_items_must_carry_the_humans_expected_team(tmp_path):
    checks, _, _ = world(tmp_path, wrong_expected=True)
    r = result(checks, "confirmed failures")
    assert r.status == "fail" and "the human said" in r.detail


# ---------------------------------------------------------------- the planted quick fix, the fix, promotion
def test_planted_a_quick_fix_that_ships_or_is_promoted_fails(tmp_path):
    checks, _, _ = world(tmp_path, quick_ships=True)
    r = result(checks, "planted")
    assert r.status == "fail" and "BLOCKED" in r.detail
    checks, _, _ = world(tmp_path, quick_promoted=True)
    r = result(checks, "planted")
    assert r.status == "fail" and "promoted" in r.detail


def test_the_promotion_needs_its_audit_comment(tmp_path):
    checks, _, _ = world(tmp_path, skip_promote_audit=True)
    r = result(checks, "promotion")
    assert r.status == "fail" and "'promote' audit" in r.detail


def test_no_resolved_alert_fails_recovered(tmp_path):
    checks, _, _ = world(tmp_path, resolved=False)
    r = result(checks, "recovered")
    assert r.status == "fail" and "0 accepted resolved" in r.detail


# ---------------------------------------------------------------- evidence
def test_evidence_must_match_the_stored_data(tmp_path):
    checks, _, _ = world(tmp_path, evidence_alert_id="not-the-delivery")
    r = result(checks, "evidence trail")
    assert r.status == "fail" and "alert: id not-the-delivery" in r.detail
    checks, _, _ = world(tmp_path, durations={"time_to_detect_s": 5.0, "time_to_review_s": 1.0, "time_to_fix_s": 1.0,
                                              "time_to_recover_s": 1.0})
    assert "time_to_detect_s" in result(checks, "evidence trail").detail
    checks, _, _ = world(tmp_path, evidence_problems=["review before alert"])
    assert "the trail itself reports: review before alert" in result(checks, "evidence trail").detail


def test_missing_inputs_say_what_to_pass(tmp_path):
    w, args = build()
    args.pop("events")
    args.update(ticks=None)
    checks = {c.name: c for c in module_14_checks(TAG, Fake(w), alert_secret=SECRET, evidence=None, **args)}
    assert "pass ticks=" in result(checks, "traffic").detail
    assert "pass evidence=" in result(checks, "evidence trail").detail


def test_native_prompt_webhooks_are_unavailable_until_whitelisted(tmp_path):
    checks, _, _ = world(tmp_path)
    r = result(checks, "native prompt webhook")
    assert r.status == "unavailable" and "LANGFUSE_WEBHOOK_WHITELISTED_HOST" in r.detail
