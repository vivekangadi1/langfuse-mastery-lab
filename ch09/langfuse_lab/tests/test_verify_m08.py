"""Module 08 verify checks against fakes shaped like the live API (server 4.77 / SDK 5.2, probed 2026-11-12)."""
from types import SimpleNamespace as NS

from langfuse_lab.verify import FeatureUnavailable, NotYet, _kappa, module_08_checks

from .test_verify_modules import FakeObservations, obs

QID = "cmq1"
CFG = {"human_routing_ok": "c-human", "review_error_type": "c-err"}


def configs(prefix=""):
    return [
        {"id": CFG["human_routing_ok"], "name": prefix + "human_routing_ok", "dataType": "BOOLEAN", "isArchived": False,
         "categories": [{"value": 1.0, "label": "True"}, {"value": 0.0, "label": "False"}]},
        {"id": CFG["review_error_type"], "name": prefix + "review_error_type", "dataType": "CATEGORICAL",
         "isArchived": False, "categories": [{"value": float(i), "label": l} for i, l in enumerate(
             ["wrong_team", "missed_handoff", "unnecessary_handoff", "other"])]},
        {"id": "old", "name": prefix + "human_routing_ok", "dataType": "BOOLEAN", "isArchived": True},
    ]


def queue(prefix="", config_ids=("c-human", "c-err")):
    return {"id": QID, "name": prefix + "routing-review", "description": "d", "scoreConfigIds": list(config_ids)}


def item(tid, status="COMPLETED", object_type="TRACE", iid=None):
    return {"id": iid or f"it-{tid}", "queueId": QID, "objectId": tid, "objectType": object_type, "status": status,
            "completedAt": "2026-11-12T06:31:48Z" if status == "COMPLETED" else None}


def judge(tid, ok, *, source="API", name="routing_judge", ts="2026-11-12T06:00:00Z"):
    meta = {"evaluator": "stand-in:keyword-v1"} if source == "API" else {"evaluator_id": "ev-r"}
    return {"id": f"j-{name}-{tid}", "name": name, "value": ok, "source": source, "dataType": "BOOLEAN",
            "comment": "policy team x; agent chose y", "configId": None, "metadata": meta, "timestamp": ts,
            "subject": {"kind": "observation", "id": f"r-{tid}", "traceId": tid}, "authorUserId": None, "queueId": None}


def human(tid, ok, *, ui=False, queue_id=QID, config="c-human", name="human_routing_ok"):
    return {"id": f"h-{tid}", "name": name, "value": ok, "source": "ANNOTATION", "dataType": "BOOLEAN",
            "comment": "reviewed", "configId": config, "metadata": {}, "timestamp": "2026-11-12T07:00:00Z",
            "subject": {"kind": "trace", "id": tid}, "authorUserId": "u1" if ui else None, "queueId": queue_id}


def err(tid, label, name="review_error_type"):
    return {"id": f"e-{tid}", "name": name, "value": label, "source": "ANNOTATION", "dataType": "CATEGORICAL",
            "comment": None, "configId": "c-err", "metadata": {}, "timestamp": "2026-11-12T07:00:00Z",
            "subject": {"kind": "trace", "id": tid}, "authorUserId": None, "queueId": QID}


def comment(tid, content="looked at it"):
    return {"id": f"cm-{tid}", "projectId": "p", "objectType": "TRACE", "objectId": tid, "content": content,
            "authorUserId": None}


def root(tid, *tags, handoff="none"):
    # metadata as the live API returns it with the `metadata` field group: values JSON-encoded
    return obs(id=f"r-{tid}", trace_id=tid, type="AGENT", name="support-request", is_root_observation=True,
               tags=["run:x", *tags], metadata={"handoff_target": f'"{handoff}"'})


def run():
    """t1 refund (judge ok), t2 shipping (ok), t3 refund policy (judge FALSE ALARM), t4 planted (judge + human False),
    t5 returns (ok). Queue: t3, t4 flagged + t5 control. Human: t3 ok, t4 not ok (wrong_team), t5 ok."""
    items = [root("t1", "refund_double_charge"), root("t2", "shipping_status_no_handoff_needed"),
             root("t3", "kb_refund_policy"), root("t4", "reset_password_wrong_handoff", "wrong_handoff", handoff="billing_specialist"),
             root("t5", "returns_policy_unjudged")]
    verdicts = {"t1": True, "t2": True, "t3": False, "t4": False, "t5": True}
    scores = {t: [judge(t, v)] for t, v in verdicts.items()}
    scores["t3"].append(human("t3", True, ui=True))
    scores["t4"] += [human("t4", False), err("t4", "wrong_team")]
    scores["t5"].append(human("t5", True))
    queue_items = [item("t3"), item("t4"), item("t5"), item("other-run-trace", status="PENDING")]
    comments = {t: [comment(t)] for t in ("t3", "t4", "t5")}
    return {"items": items, "scores": scores, "configs": configs(), "queues": [queue(), {**queue(), "id": "x",
            "name": "someone-elses-queue"}], "queue_items": queue_items, "comments": comments}


REPORT = {"n": 3, "agree": 2, "rate": 2 / 3, "judge_flagged": 2, "human_flagged": 1, "both_flagged": 1,
          "judge_missed": 0, "judge_false_alarm": 1, "flag_precision": 0.5, "kappa": 0.4}


class Paged:
    """Page-based list endpoint (meta.total_pages); 2 rows per page to exercise paging."""
    def __init__(self, rows):
        self.rows = rows

    def __call__(self, page=1, limit=100):
        chunk = self.rows[(page - 1) * 2: page * 2]
        return NS(data=chunk, meta=NS(page=page, total_pages=max(1, -(-len(self.rows) // 2))))


class FakeQueues:
    def __init__(self, queues, items):
        self.list_queues = Paged(queues)
        self.items = items

    def list_queue_items(self, queue_id, page=1, limit=100, status=None):
        assert queue_id == QID
        return Paged(self.items)(page=page, limit=limit)


class FakeScores:
    def __init__(self, by_trace):
        self.by_trace = by_trace

    def get_many(self, **kw):
        assert kw.get("fields") == "details,subject,annotation"
        return NS(data=self.by_trace.get(kw.get("trace_id"), []), meta=NS(cursor=None))


class FakeConfigs:
    def __init__(self, rows):
        self.rows = rows

    def get(self, page=1, limit=100):
        return NS(data=self.rows, meta=NS(total_pages=1))


class FakeComments:
    def __init__(self, by_trace):
        self.by_trace = by_trace

    def get(self, object_type=None, object_id=None, limit=None, page=None):
        assert object_type == "TRACE"
        return NS(data=self.by_trace.get(object_id, []), meta=NS(total_pages=1))


def client(fx):
    return NS(api=NS(observations=FakeObservations(fx["items"]), scores=FakeScores(fx["scores"]),
                     score_configs=FakeConfigs(fx["configs"]), annotation_queues=FakeQueues(fx["queues"], fx["queue_items"]),
                     comments=FakeComments(fx["comments"])), flush=lambda: None)


def _call(check):
    try:
        return check.fn()
    except NotYet as exc:
        return ("not_yet", str(exc))
    except FeatureUnavailable as exc:
        return ("unavailable", str(exc))


def results(fx, **kw):
    kw.setdefault("report", REPORT)
    return {c.name: _call(c) for c in module_08_checks("run:x", client(fx), **kw)}


def passing(r):
    assignment = r.pop("queue assignment (read back)")
    assert assignment[0] == "unavailable" and "no list" in assignment[1]
    return all(v[0] is True for v in r.values())


def test_all_pass():
    r = results(run())
    detail = r["every queued trace reviewed (ANNOTATION score, COMPLETED)"][1]
    assert "1 scored in the UI" in detail
    assert "stand-in" in r["every trace has a judge verdict"][1]
    assert passing(r), r


def test_kappa():
    assert abs(_kappa([(False, True), (False, False), (True, True)]) - 0.4) < 1e-9
    assert _kappa([]) is None and _kappa([(True, True)]) is None


def test_live_eval_scores_also_count():
    fx = run()
    for t, rows in fx["scores"].items():
        rows[0] = judge(t, rows[0]["value"], source="EVAL")
    r = results(fx)
    assert "live (EVAL)" in r["every trace has a judge verdict"][1]
    assert passing(r), r


def test_plain_api_score_is_not_a_judge():
    fx = run()
    fx["scores"]["t1"][0]["metadata"] = {"evaluator": "code:v1"}
    assert results(fx)["every trace has a judge verdict"][0] == "not_yet"


def test_low_helpfulness_flags_in_live_mode():
    fx = run()
    fx["scores"]["t1"].append({**judge("t1", 0.2, source="EVAL", name="helpfulness_judge"), "dataType": "NUMERIC"})
    ok, detail = results(fx)["queue = judge-flagged traces + controls, no duplicates"]
    assert ok is False and "t1" in detail


def test_prefix_scopes_names():
    p = "zz_ref_m08_"
    fx = run()
    fx["configs"], fx["queues"] = configs(p), [queue(p)]
    for rows in fx["scores"].values():
        for s in rows:
            s["name"] = p + s["name"]
    assert passing(results(fx, prefix=p))
    assert results(fx)["annotation queue (scores with both configs)"][0] is False  # unprefixed: not found


def test_missing_and_duplicate_configs_fail():
    fx = run()
    fx["configs"] = configs() + [{**configs()[0], "id": "dup"}]
    ok, detail = results(fx)["review score configs defined once"]
    assert not ok and "duplicates" in detail
    fx["configs"] = [c for c in configs() if c["name"] != "review_error_type"]
    assert results(fx)["review score configs defined once"][0] is False


def test_queue_without_error_type_config_fails():
    fx = run()
    fx["queues"] = [queue(config_ids=("c-human",))]
    ok, detail = results(fx)["annotation queue (scores with both configs)"]
    assert not ok and "c-err" in detail


def test_duplicate_item_fails():
    fx = run()
    fx["queue_items"].append(item("t4", iid="it-t4-again"))
    ok, detail = results(fx)["queue = judge-flagged traces + controls, no duplicates"]
    assert not ok and "more than once" in detail


def test_flagged_trace_not_queued_fails_and_empty_queue_waits():
    fx = run()
    fx["queue_items"] = [i for i in fx["queue_items"] if i["objectId"] != "t3"]
    assert results(fx)["queue = judge-flagged traces + controls, no duplicates"][0] is False
    fx["queue_items"] = []
    assert results(fx)["queue = judge-flagged traces + controls, no duplicates"][0] == "not_yet"


def test_no_control_and_too_many_controls_fail():
    fx = run()
    fx["queue_items"] = [i for i in fx["queue_items"] if i["objectId"] != "t5"]
    ok, detail = results(fx)["queue = judge-flagged traces + controls, no duplicates"]
    assert not ok and "control" in detail
    fx = run()
    fx["queue_items"] += [item("t1"), item("t2")]
    ok, detail = results(fx)["queue = judge-flagged traces + controls, no duplicates"]
    assert not ok and "<= 2" in detail


def test_pending_item_waits():
    fx = run()
    fx["queue_items"][0] = item("t3", status="PENDING")
    status, detail = results(fx)["every queued trace reviewed (ANNOTATION score, COMPLETED)"]
    assert status == "not_yet" and "PENDING" in detail and "Process queue" in detail


def test_human_score_without_config_or_queue():
    fx = run()
    fx["scores"]["t5"][-1] = human("t5", True, config=None)
    assert results(fx)["every queued trace reviewed (ANNOTATION score, COMPLETED)"][0] == "not_yet"
    fx["scores"]["t5"][-1] = human("t5", True, queue_id=None)
    ok, detail = results(fx)["every queued trace reviewed (ANNOTATION score, COMPLETED)"]
    assert not ok and "queue_id" in detail


def test_missing_comment_fails():
    fx = run()
    fx["comments"]["t3"] = [comment("t3", content="  ")]
    ok, detail = results(fx)["reviewer comments on reviewed traces"]
    assert not ok and "t3" in detail


def test_missing_error_type_fails():
    fx = run()
    fx["scores"]["t4"] = [s for s in fx["scores"]["t4"] if s["name"] != "review_error_type"]
    assert results(fx)["error type on human-flagged traces"][0] is False


def test_report_mismatch_and_missing_report_fail():
    assert results(run(), report={**REPORT, "agree": 3})["human-vs-judge report matches the stored scores"][0] is False
    assert results(run(), report={**REPORT, "kappa": 0.5})["human-vs-judge report matches the stored scores"][0] is False
    ok, detail = results(run(), report=None)["human-vs-judge report matches the stored scores"]
    assert not ok and "report=REPORT" in detail


def test_planted_failures():
    fx = run()  # human says the planted trace was fine
    fx["scores"]["t4"][1] = human("t4", True)
    assert results(fx)["planted wrong_handoff caught: flagged, queued, confirmed"][0] is False
    fx = run()  # wrong error type: login -> billing is wrong_team
    fx["scores"]["t4"][2] = err("t4", "missed_handoff")
    ok, detail = results(fx)["planted wrong_handoff caught: flagged, queued, confirmed"]
    assert not ok and "wrong_team" in detail
    fx = run()  # seen live: the bad prompt sometimes hands off to nobody -> missed_handoff is the right label
    fx["items"][3] = root("t4", "reset_password_wrong_handoff", "wrong_handoff", handoff="none")
    fx["scores"]["t4"][2] = err("t4", "missed_handoff")
    ok, detail = results(fx)["planted wrong_handoff caught: flagged, queued, confirmed"]
    assert ok and "missed_handoff" in detail
    fx = run()  # judge passed it
    fx["scores"]["t4"][0] = judge("t4", True)
    ok, detail = results(fx)["planted wrong_handoff caught: flagged, queued, confirmed"]
    assert not ok and "passed the planted" in detail
