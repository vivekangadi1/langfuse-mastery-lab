"""Module 09 verify checks against fakes shaped like the live API (server 4.77 / SDK 5.2, probed 2026-11-12)."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from langfuse_lab import regression
from langfuse_lab.verify import FeatureUnavailable, NotYet, module_09_checks

from .test_verify_modules import obs

DS = "support-regression"
DSID = "cmds1"
CFG = "c-hc"
V1 = datetime(2026, 11, 12, 7, 0, 0, tzinfo=timezone.utc)   # first write of the invoice item
V2 = V1 + timedelta(seconds=5)                               # the label fix (pinned dataset version)
SOURCE = {  # trace id: (message, expected, actual, planted)
    "t1": ("I was charged twice for order 8129. Please fix it.", "billing_specialist", "billing_specialist", False),
    "t2": ("Where is my order 9912 and when should it arrive?", "none", "none", False),
    "t3": ("What is your refund policy?", "none", "none", False),
    "t4": ("I cannot log in after changing my password.", "account_security", "billing_specialist", True),
}
CORRECTION = "I checked your account and routed this to account security to restore access."


class NotFoundError(Exception):
    status_code = 404


def iid(msg):
    return regression.dataset_item_id(DS, msg)


def root(tid):
    msg, _, actual, planted = SOURCE[tid]
    return obs(id=f"r-{tid}", trace_id=tid, type="AGENT", name="support-request", is_root_observation=True,
               input=json.dumps(msg), tags=["run:x", *(["wrong_handoff"] if planted else [])],
               metadata={"handoff_target": json.dumps(actual)})


def source_scores(tid):
    _, expected, actual, _ = SOURCE[tid]
    rows = [{"id": f"hc-{tid}", "name": "handoff_correct", "value": expected == actual, "dataType": "BOOLEAN",
             "source": "API", "configId": CFG, "timestamp": "2026-11-12T06:00:00Z",
             "metadata": {"evaluator": "code:v1", "expected": expected, "actual": actual}}]
    if expected != actual:
        rows.append({"id": f"c-{tid}", "name": "output", "value": CORRECTION, "dataType": "CORRECTION",
                     "source": "API", "configId": None, "timestamp": "2026-11-12T06:00:00Z", "metadata": {}})
    return rows


def ds_item(msg, handoff, *, label, case_id, trace=None, status="ACTIVE", version=V2, reference=None):
    return {"id": iid(msg), "status": status, "input": {"message": msg},
            "expectedOutput": {"handoff": handoff, "reference_answer": reference},
            "metadata": {"case_id": case_id, "source": "trace" if trace else "curated", "label": label},
            "sourceTraceId": trace, "sourceObservationId": f"r-{trace}" if trace else None,
            "datasetId": DSID, "datasetName": DS, "version": version}


def dataset_items():
    out = []
    for tid, (msg, expected, actual, _) in SOURCE.items():
        failed = expected != actual
        out.append(ds_item(msg, expected, label="confirmed_failure" if failed else "golden", case_id=tid, trace=tid,
                           version=V1, reference=CORRECTION if failed else None))
    locked = regression.curated("locked_out_cust_03")
    out.append(ds_item(locked.message, "account_security", label="curated", case_id=locked.case_id, version=V1))
    inv = regression.curated("invoice_request")
    out.append(ds_item(inv.message, "billing_specialist", label="curated", case_id=inv.case_id, version=V2))
    return out


def previous_items():
    """dataset_items.list(version=V2 - 1ms): the invoice item still expects 'none'."""
    out = [dict(i) for i in dataset_items()]
    inv = next(i for i in out if i["metadata"]["case_id"] == "invoice_request")
    inv.update(expectedOutput={"handoff": "none", "reference_answer": None}, version=V1)
    return out


def retired_item(status="ARCHIVED"):
    r = regression.curated(regression.RETIRED)
    return ds_item(r.message, "none", label="curated", case_id=r.case_id, status=status, version=V2)


def score(name, value, *, data_type="BOOLEAN", config=None):
    return {"id": f"s-{name}", "name": name, "value": value, "dataType": data_type, "source": "API",
            "timestamp": "2026-11-12T07:10:00Z", "configId": config}


def exp_item(exp_id, item, handoff, *, n, version=V2, hc_value=None, configured=True, quality=1.0):
    ok = handoff == item["expectedOutput"]["handoff"] if hc_value is None else hc_value
    return {"id": f"o-{exp_id}-{n}", "traceId": f"x-{exp_id}-{n}", "environment": "sdk-experiment",
            "experimentId": exp_id, "experimentName": exp_id, "experimentItemId": item["id"],
            "experimentDatasetId": DSID, "experimentItemVersion": version,
            "input": json.dumps(item["input"]), "output": json.dumps({"handoff": handoff, "answer": "ok."}),
            "expectedOutput": json.dumps(item["expectedOutput"]),
            "experimentItemMetadata": item["metadata"], "metadata": {},
            "scores": [score("handoff_correct", ok, config=CFG if configured else None), score("concise", True),
                       score("quality", quality, data_type="NUMERIC")]}


def experiment(exp_id, label, version, items, *, trigger="notebook", run_tag="run:x", accuracy=None):
    vals = [it["scores"][0]["value"] for it in items]
    acc = (sum(vals) / len(vals) if vals else 0.0) if accuracy is None else accuracy
    return {"id": exp_id, "name": f"{label} v{version} · x", "datasetId": DSID, "itemCount": len(items),
            "metadata": {"run_tag": run_tag, "prompt_label": label, "prompt_version": str(version), "trigger": trigger},
            "scores": [score("handoff_accuracy", acc, data_type="NUMERIC"),
                       score("quality_avg", 0.9, data_type="NUMERIC")]}


def world():
    items = dataset_items()
    by_case = {i["metadata"]["case_id"]: i for i in items}
    base_handoffs = {i["id"]: i["expectedOutput"]["handoff"] for i in items}  # v1 gets everything right
    cand_handoffs = dict(base_handoffs)
    cand_handoffs[by_case["t4"]["id"]] = "billing_specialist"           # planted: login -> billing
    cand_handoffs[by_case["locked_out_cust_03"]["id"]] = "none"         # ... or to nobody
    exp_items = {
        "e-base": [exp_item("e-base", i, base_handoffs[i["id"]], n=n) for n, i in enumerate(items)],
        "e-cand": [exp_item("e-cand", i, cand_handoffs[i["id"]], n=n) for n, i in enumerate(items)],
        "e-remote": [exp_item("e-remote", i, cand_handoffs[i["id"]], n=n) for n, i in enumerate(items[:2])],
    }
    exps = [experiment("e-base", "production", 1, exp_items["e-base"]),
            experiment("e-cand", "staging", 2, exp_items["e-cand"]),
            experiment("e-remote", "staging", 2, exp_items["e-remote"], trigger="remote"),
            experiment("e-other", "production", 1, [], run_tag="run:someone-else")]
    generations = {it["traceId"]: [obs(id=f"g-{it['traceId']}", trace_id=it["traceId"], type="GENERATION",
                                       prompt_name="support-agent-system",
                                       prompt_version=1 if eid == "e-base" else 2)]
                   for eid, rows in exp_items.items() for it in rows}
    return {"roots": [root(t) for t in SOURCE], "src_scores": {t: source_scores(t) for t in SOURCE},
            "items": items, "previous": previous_items(), "retired": retired_item(), "experiments": exps,
            "exp_items": exp_items, "generations": generations, "ui_items": [],
            "labels": {"production": 1, "staging": 2}, "dataset": True,
            "configs": [{"id": CFG, "name": "handoff_correct", "dataType": "BOOLEAN", "isArchived": False}]}


def regressed_ids():
    items = {i["metadata"]["case_id"]: i["id"] for i in dataset_items()}
    return sorted([items["t4"], items["locked_out_cust_03"]])


COMPARISON = {"n": 6, "baseline_rate": 1.0, "candidate_rate": 4 / 6, "regressed": regressed_ids(), "improved": []}


class Paged:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self, page=1, limit=100, **_):
        chunk = self.rows[(page - 1) * 2: page * 2]
        return NS(data=chunk, meta=NS(page=page, total_pages=max(1, -(-len(self.rows) // 2))))


def fake_client(w):
    def get_many_obs(**kw):
        if kw.get("trace_id"):
            tid = kw["trace_id"]
            return NS(data=w["generations"].get(tid, []) or [r for r in w["roots"] if r.trace_id == tid])
        return NS(data=w["roots"], meta=NS(cursor=None))

    def get_scores(**kw):
        assert kw["fields"] == "details,subject"
        tid = kw["trace_id"]
        if tid in w["src_scores"]:
            return NS(data=w["src_scores"][tid], meta=NS(cursor=None))
        rows = [it for items in w["exp_items"].values() for it in items if it["traceId"] == tid]
        return NS(data=rows[0]["scores"] if rows else [], meta=NS(cursor=None))

    def prompt_get(name, label=None, version=None):
        assert name == "support-agent-system"
        if label not in w["labels"]:
            raise NotFoundError(label)
        return NS(version=w["labels"][label])

    def ds_get(name):
        if name != DS or not w["dataset"]:
            raise NotFoundError(name)
        return {"id": DSID, "name": DS, "description": "d", "metadata": {}}

    def items_list(dataset_name, version=None, page=1, limit=100):
        assert dataset_name == DS
        rows = w["previous"] if version is not None and version < V2 else w["items"]
        return Paged(rows)(page=page, limit=limit)

    def items_get(item_id):
        if w["retired"] is None or item_id != w["retired"]["id"]:
            raise NotFoundError(item_id)
        return w["retired"]

    def exp_list(from_start_time, dataset_id, fields, limit=100, cursor=None):
        assert dataset_id == DSID and fields == "core,metadata,scores"
        return NS(data=w["experiments"], meta=NS(cursor=None))

    def exp_items(from_start_time, fields, limit=100, cursor=None, experiment_id=None, dataset_id=None):
        if experiment_id:  # like the live API: item scores without configId / comment / metadata
            rows = [{**it, "scores": [{k: v for k, v in sc.items() if k != "configId"} for sc in it["scores"]]}
                    for it in w["exp_items"].get(experiment_id, [])]
            return NS(data=rows, meta=NS(cursor=None))
        return NS(data=w["ui_items"], meta=NS(cursor=None))

    api = NS(observations=NS(get_many=get_many_obs), scores=NS(get_many=get_scores),
             score_configs=NS(get=Paged(w["configs"])), prompts=NS(get=prompt_get),
             datasets=NS(get=ds_get), dataset_items=NS(list=items_list, get=items_get),
             experiments=NS(list=exp_list, list_items=exp_items))
    return NS(api=api)


def results(w=None, **kw):
    kw.setdefault("comparison", COMPARISON)
    kw.setdefault("decision", ("block", ["2 regressed"]))
    checks = module_09_checks("run:x", fake_client(w or world()), **kw)
    out = {}
    for c in checks:
        try:
            out[c.name] = c.fn()
        except (NotYet, AssertionError) as exc:
            out[c.name] = ("notyet", str(exc))
        except FeatureUnavailable as exc:
            out[c.name] = ("unavailable", str(exc))
    return out


def failing(out):
    return {k: v for k, v in out.items() if v[0] is not True and v[0] != "unavailable"}


def test_passing_fixture():
    out = results()
    assert failing(out) == {}, failing(out)
    assert out["remote-run URL configured in the UI (read back)"][0] == "unavailable"
    assert out["UI prompt experiment (optional)"][0] == "unavailable"
    assert "locked_out_cust_03 -> none" in out["planted: staging regresses login routing, gate blocks"][1]


def test_ui_prompt_experiment_passes_when_present():
    w = world()
    w["ui_items"] = [{"id": "u1", "environment": "langfuse-prompt-experiment", "experimentName": "ui run"}]
    assert results(w)["UI prompt experiment (optional)"][0] is True


def test_missing_dataset_is_notyet():
    w = world()
    w["dataset"] = False
    out = results(w)
    assert out["regression dataset with labelled items"][0] == "notyet"


def test_duplicate_requests_fail():
    w = world()
    dup = dict(w["items"][0], id="random-id")
    w["items"] = w["items"] + [dup]
    assert results(w)["regression dataset with labelled items"][0] is False


def test_trace_item_with_wrong_label_fails():
    w = world()
    w["items"][3] = dict(w["items"][3], metadata={**w["items"][3]["metadata"], "label": "golden"})
    detail = results(w)["items from the run's labelled traces"]
    assert detail[0] is False and "confirmed_failure" in detail[1]


def test_trace_item_without_source_trace_fails():
    w = world()
    w["items"][0] = dict(w["items"][0], sourceTraceId=None)
    assert results(w)["items from the run's labelled traces"][0] is False


def test_label_fix_missing_fails():
    w = world()
    w["items"] = [dict(i, expectedOutput={"handoff": "none"}) if i["metadata"]["case_id"] == "invoice_request" else i
                  for i in w["items"]]
    detail = results(w)["dataset versioning: label fix + archived item"]
    assert detail[0] is False and "revise_item" in detail[1]


def test_retired_item_not_archived_fails():
    w = world()
    w["retired"] = retired_item(status="ACTIVE")
    assert results(w)["dataset versioning: label fix + archived item"][0] is False


def test_unpinned_or_duplicated_runs_fail():
    w = world()
    for it in w["exp_items"]["e-base"]:
        it["experimentItemVersion"] = None
    assert results(w)["production vs staging on the pinned dataset"][0] is False
    w = world()
    w["exp_items"]["e-cand"].append(dict(w["exp_items"]["e-cand"][0]))
    detail = results(w)["production vs staging on the pinned dataset"]
    assert detail[0] is False and "RUN_TAG" in detail[1]


def test_wrong_prompt_version_metadata_fails():
    w = world()
    w["labels"]["staging"] = 3
    assert results(w)["production vs staging on the pinned dataset"][0] is False


def test_unlinked_generation_fails():
    w = world()
    tid = w["exp_items"]["e-cand"][0]["traceId"]
    w["generations"][tid] = [obs(id="g", trace_id=tid, type="GENERATION", prompt_name="", prompt_version=None)]
    assert results(w)["experiment generations linked to the variant's prompt"][0] is False


def test_evaluator_without_config_or_wrong_value_fails():
    w = world()
    w["exp_items"]["e-base"] = [exp_item("e-base", i, i["expectedOutput"]["handoff"], n=n, configured=False)
                                for n, i in enumerate(dataset_items())]
    assert results(w)["item + composite evaluators on every item"][0] is False
    w = world()
    w["exp_items"]["e-base"][0]["scores"][2] = score("quality", 4.0, data_type="NUMERIC")
    assert results(w)["item + composite evaluators on every item"][0] is False


def test_run_evaluator_mismatch_fails():
    w = world()
    w["experiments"][1] = experiment("e-cand", "staging", 2, w["exp_items"]["e-cand"], accuracy=0.9)
    assert results(w)["run evaluators match the item scores"][0] is False


def test_comparison_must_match():
    assert results(comparison=None)["comparison matches the stored scores"][0] is False
    wrong = {**COMPARISON, "regressed": regressed_ids()[:1]}
    assert results(comparison=wrong)["comparison matches the stored scores"][0] is False


def test_planted_requires_block_and_a_login_regression():
    out = results(decision="ship")
    assert out["planted: staging regresses login routing, gate blocks"][0] is False
    w = world()  # staging identical to production: no regression measured
    w["exp_items"]["e-cand"] = [exp_item("e-cand", i, i["expectedOutput"]["handoff"], n=n)
                                for n, i in enumerate(dataset_items())]
    w["experiments"][1] = experiment("e-cand", "staging", 2, w["exp_items"]["e-cand"])
    detail = results(w, comparison=None)["planted: staging regresses login routing, gate blocks"]
    assert detail[0] is False and "no login item" in detail[1]


def test_remote_trigger_required():
    w = world()
    w["experiments"] = [e for e in w["experiments"] if e["id"] != "e-remote"]
    assert results(w)["remote trigger ran an experiment"][0] is False


@pytest.mark.parametrize("label", ["production", "staging"])
def test_missing_variant_is_notyet(label):
    w = world()
    w["experiments"] = [e for e in w["experiments"] if e["metadata"]["prompt_label"] != label]
    assert results(w)["production vs staging on the pinned dataset"][0] == "notyet"


def test_planted_passes_when_averages_tie():
    """Validation run 2: equal averages, but a login item regressed: the gate (and the check) go by items."""
    w = world()
    wrong = {"invoice_request": "none", "t2": "billing_specialist"}  # production misses 2 items staging gets right
    w["exp_items"]["e-base"] = [exp_item("e-base", i, wrong.get(i["metadata"]["case_id"], i["expectedOutput"]["handoff"]),
                                         n=n) for n, i in enumerate(dataset_items())]
    w["experiments"][0] = experiment("e-base", "production", 1, w["exp_items"]["e-base"])
    ok, detail = results(w, comparison=None)["planted: staging regresses login routing, gate blocks"]
    assert ok is True and "login item" in detail and "hidden" in detail
