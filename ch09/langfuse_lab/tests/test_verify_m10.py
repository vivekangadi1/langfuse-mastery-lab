"""Module 10 verify checks against fakes shaped like the live API (server 4.77 / SDK 5.2, probed 2026-11-12)."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from langfuse_lab import regression
from langfuse_lab.verify import NotYet, module_10_checks

DS, DSID = "support-regression", "cmds1"
PIN = datetime(2026, 11, 12, 7, 0, 0, 211000, tzinfo=timezone.utc)
SHA = "0123456789abcdef0123456789abcdef01234567"
ITEMS = regression.seed_items(DS)
BY_CASE = {i["metadata"]["case_id"]: i["id"] for i in ITEMS}
LOGIN, LOCKED = BY_CASE["reset_password_wrong_handoff"], BY_CASE["locked_out_cust_03"]
KB, INVOICE = BY_CASE["kb_refund_policy"], BY_CASE["invoice_request"]
POLICY = {"reruns": 1, "max_rerun_items": 3, "max_regressions": 0, "max_drop": 0.2}


class NotFoundError(Exception):
    status_code = 404


def verdicts(fail=(), only=None):
    ids = only if only is not None else [i["id"] for i in ITEMS]
    return {i: i not in fail for i in ids}


def exp_rows(eid, v):
    out = []
    for n, (iid, ok) in enumerate(v.items()):
        item = next(i for i in ITEMS if i["id"] == iid)
        out.append({"id": f"o-{eid}-{n}", "traceId": f"t-{eid}-{n}", "experimentId": eid, "experimentItemId": iid,
                    "experimentItemVersion": PIN, "environment": "sdk-experiment",
                    "expectedOutput": json.dumps(item["expected_output"]), "experimentItemMetadata": item["metadata"],
                    "output": json.dumps({"handoff": "x", "answer": "ok."}),
                    "scores": [{"name": "handoff_correct", "value": ok, "dataType": "BOOLEAN", "source": "API",
                                "timestamp": "2026-11-12T07:10:00Z"}]})
    return out


def meta(role, label, version, *, cand="staging", run_tag="run:x", **extra):
    m = {"run_tag": run_tag, "trigger": "local", "git_sha": SHA, "branch": "langfuse-mastery", "pr": "",
         "ci_run": f"m10-{cand}", "candidate_label": cand, "role": role, "prompt_label": label,
         "prompt_version": str(version), "dataset_version": PIN.isoformat(), "dry_run": "false",
         "agent_model": "gpt-4.1-mini"}
    m.update({k: str(v) for k, v in extra.items()})
    return m


def exp(eid, m, start="2026-11-12T07:05:00Z"):
    return {"id": eid, "name": f"ci {m['candidate_label']} · {m['role']} {m['prompt_label']} v{m['prompt_version']} · "
                                f"{SHA[:7]} · {m['ci_run']}", "startTime": start, "datasetId": DSID, "metadata": m,
            "scores": []}


def gate(value, regressed, flaky=(), exit_code=None, comment="1 regressed item(s) > 0 allowed", policy=POLICY):
    return [{"id": f"g-{value}", "name": "ci_gate", "value": value, "dataType": "CATEGORICAL", "source": "API",
             "timestamp": "2026-11-12T07:20:00Z", "comment": comment,
             "metadata": {"git_sha": SHA, "regressed": list(regressed), "flaky": list(flaky),
                          "exit_code": {"ship": 0, "block": 1, "error": 2}[value] if exit_code is None else exit_code,
                          "policy": policy, "reasons": [comment] if value != "ship" else []},
             "subject": {"kind": "experiment", "id": "x"}}]


def world():
    suspects = sorted([LOGIN, LOCKED])
    w = {"labels": {"production": 1, "staging": 2, "candidate": 3}, "dataset": True, "experiments": [], "items": {},
         "gate": {}}

    def add(eid, m, v, start="2026-11-12T07:05:00Z"):
        w["experiments"].append(exp(eid, m, start))
        w["items"][eid] = exp_rows(eid, v)

    add("e-base", meta("baseline", "production", 1, baseline_key="k"), verdicts(fail=[INVOICE]))
    # staging: both login items fail, and fail again on the re-run, while production passes them again
    add("e-stg", meta("candidate", "staging", 2, baseline_experiment_id="e-base", baseline_reused="false"),
        verdicts(fail=[INVOICE, LOGIN, LOCKED]))
    add("e-stg-rb", meta("rerun", "production", 1, rerun_of="e-stg", side="baseline", attempt=1),
        verdicts(only=suspects))
    add("e-stg-rc", meta("rerun", "staging", 2, rerun_of="e-stg", side="candidate", attempt=1),
        verdicts(fail=suspects, only=suspects))
    w["gate"]["e-stg"] = gate("block", suspects)
    # clean candidate: the kb item fails once (noise), passes on the re-run: flaky, ship. The invoice improves.
    add("e-cln", meta("candidate", "candidate", 3, cand="candidate", baseline_experiment_id="e-base",
                      baseline_reused="true"), verdicts(fail=[KB]), start="2026-11-12T07:15:00Z")
    add("e-cln-rb", meta("rerun", "production", 1, cand="candidate", rerun_of="e-cln", side="baseline", attempt=1),
        verdicts(only=[KB]))
    add("e-cln-rc", meta("rerun", "candidate", 3, cand="candidate", rerun_of="e-cln", side="candidate", attempt=1),
        verdicts(only=[KB]))
    w["gate"]["e-cln"] = gate("ship", [], flaky=[KB], comment="")
    # noise the checks must ignore: a dry run with the same tag, another run's candidate
    add("e-dry", meta("candidate", "staging", 2, dry_run="true", baseline_experiment_id="e-base"),
        verdicts(fail=[LOGIN]), start="2026-11-12T07:30:00Z")
    add("e-other", meta("candidate", "staging", 2, run_tag="run:other", baseline_experiment_id="e-base"),
        verdicts(), start="2026-11-12T07:31:00Z")
    return w


def fake_client(w):
    def prompt_get(name, label=None, version=None):
        assert name == "support-agent-system"
        if label not in w["labels"]:
            raise NotFoundError(label)
        return NS(version=w["labels"][label])

    def ds_get(name):
        if name != DS or not w["dataset"]:
            raise NotFoundError(name)
        return {"id": DSID, "name": DS}

    def exp_list(from_start_time, dataset_id, fields, limit=100, cursor=None):
        assert dataset_id == DSID and fields == "core,metadata,scores"
        return NS(data=w["experiments"], meta=NS(cursor=None))

    def exp_items(from_start_time, fields, experiment_id, limit=100, cursor=None):
        assert "scores" in fields and "dataset" in fields
        return NS(data=w["items"].get(experiment_id, []), meta=NS(cursor=None))

    def get_scores(fields, limit=100, experiment_id=None, **_):
        assert fields == "details,subject" and experiment_id
        return NS(data=w["gate"].get(experiment_id, []), meta=NS(cursor=None))

    api = NS(prompts=NS(get=prompt_get), datasets=NS(get=ds_get), scores=NS(get_many=get_scores),
             experiments=NS(list=exp_list, list_items=exp_items))
    return NS(api=api)


def results(w=None, **kw):
    out = {}
    for c in module_10_checks("run:x", fake_client(w or world()), **kw):
        try:
            out[c.name] = c.fn()
        except (NotYet, AssertionError) as exc:
            out[c.name] = ("notyet", str(exc))
    return out


def failing(out):
    return {k: v for k, v in out.items() if v[0] is not True}


def find(w, eid):
    return next(e for e in w["experiments"] if e["id"] == eid)


def test_passing_fixture():
    out = results()
    assert failing(out) == {}, failing(out)
    assert "cached" in out["baseline = production on the same pinned dataset"][1]
    assert "reset_password_wrong_handoff" in out["planted: staging blocked, regressed login items named"][1]
    assert "1 flaky" in out["clean candidate shipped"][1]


def test_missing_dataset_and_missing_gate_runs_are_notyet():
    w = world()
    w["dataset"] = False
    assert set(v[0] for v in results(w).values()) == {"notyet"}
    w = world()
    w["experiments"] = [e for e in w["experiments"] if e["id"] != "e-cln"]
    assert results(w)["clean candidate shipped"][0] == "notyet"


def test_metadata_required():
    w = world()
    del find(w, "e-stg")["metadata"]["git_sha"]
    assert results(w)["gate runs carry the CI metadata"][0] is False
    w = world()
    find(w, "e-stg")["metadata"]["prompt_version"] = "1"
    detail = results(w)["gate runs carry the CI metadata"]
    assert detail[0] is False and "prompt_version" in detail[1]
    w = world()
    find(w, "e-cln")["name"] = "ci candidate run"
    assert results(w)["gate runs carry the CI metadata"][0] is False


def test_latest_candidate_per_label_counts():
    w = world()  # an earlier, broken staging gate in the same RUN_TAG is superseded by the later one
    w["experiments"].append(exp("e-stg-old", meta("candidate", "staging", 2, baseline_experiment_id="nope"),
                                start="2026-11-12T06:00:00Z"))
    assert failing(results(w)) == {}


def test_baseline_must_be_production_on_the_same_pin():
    w = world()
    find(w, "e-base")["metadata"]["prompt_version"] = "2"
    assert results(w)["baseline = production on the same pinned dataset"][0] is False
    w = world()
    find(w, "e-base")["metadata"]["dry_run"] = "true"
    detail = results(w)["baseline = production on the same pinned dataset"]
    assert detail[0] is False and "DRY" in detail[1]
    w = world()
    for it in w["items"]["e-cln"]:
        it["experimentItemVersion"] = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
    assert results(w)["baseline = production on the same pinned dataset"][0] is False
    w = world()
    w["items"]["e-stg"].append(dict(w["items"]["e-stg"][0]))
    detail = results(w)["baseline = production on the same pinned dataset"]
    assert detail[0] is False and "twice" in detail[1]


def test_reruns_only_suspects_on_both_sides():
    w = world()
    w["items"]["e-stg-rc"] = exp_rows("e-stg-rc", verdicts(fail=[LOGIN, LOCKED]))  # re-ran everything
    detail = results(w)["only the suspects were re-run, on both sides"]
    assert detail[0] is False and "ONLY" in detail[1]
    w = world()
    w["experiments"] = [e for e in w["experiments"] if e["id"] != "e-stg-rb"]
    assert results(w)["only the suspects were re-run, on both sides"][0] is False


def test_recorded_decision_must_match_recomputation():
    w = world()
    w["gate"]["e-cln"] = gate("block", [KB])
    detail = results(w)["gate decision recorded on the candidate experiment"]
    assert detail[0] is False and "flaky" in detail[1]
    w = world()
    w["gate"]["e-stg"] = gate("block", [LOGIN, LOCKED], exit_code=0)
    assert results(w)["gate decision recorded on the candidate experiment"][0] is False
    w = world()
    w["gate"]["e-stg"] = gate("block", [LOGIN, LOCKED], comment="")
    assert results(w)["gate decision recorded on the candidate experiment"][0] is False
    w = world()
    w["gate"]["e-stg"] = []
    assert results(w)["gate decision recorded on the candidate experiment"][0] == "notyet"


def test_flaky_login_is_not_confirmed():
    w = world()  # production fails the login items on the re-run too: noise, not a regression
    w["items"]["e-stg-rb"] = exp_rows("e-stg-rb", verdicts(fail=[LOGIN, LOCKED], only=sorted([LOGIN, LOCKED])))
    w["gate"]["e-stg"] = gate("ship", [], flaky=sorted([LOGIN, LOCKED]), comment="")
    out = results(w)
    assert out["gate decision recorded on the candidate experiment"][0] is True
    assert out["planted: staging blocked, regressed login items named"][0] is False


def test_planted_must_block_and_name_items():
    w = world()
    w["gate"]["e-stg"] = gate("block", [LOCKED])
    assert results(w)["planted: staging blocked, regressed login items named"][0] is False


def test_clean_candidate_must_ship_and_be_a_new_version():
    w = world()
    w["labels"]["candidate"] = 1
    find(w, "e-cln")["metadata"]["prompt_version"] = "1"
    assert results(w)["clean candidate shipped"][0] is False
    w = world()
    w["items"]["e-cln-rc"] = exp_rows("e-cln-rc", verdicts(fail=[KB], only=[KB]))  # a real (false?) block
    w["gate"]["e-cln"] = gate("block", [KB])
    out = results(w)
    assert out["gate decision recorded on the candidate experiment"][0] is True
    assert out["clean candidate shipped"][0] is False


@pytest.mark.parametrize("drop,decision", [(0.0, "block"), (0.5, "ship")])
def test_recompute_uses_the_recorded_max_drop(drop, decision):
    w = world()  # without the invoice improvement, the flaky kb item lowers the mean pass rate by 0.5 / 7 = 0.07
    w["items"]["e-cln"] = exp_rows("e-cln", verdicts(fail=[KB, INVOICE]))
    policy = {**POLICY, "max_drop": drop}
    w["gate"]["e-cln"] = gate(decision, [], flaky=[KB], policy=policy, comment="" if decision == "ship" else "drop")
    assert results(w)["gate decision recorded on the candidate experiment"][0] is True
