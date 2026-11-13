"""Module 07 verify checks against fakes shaped like the live API (server 4.77 / SDK 5.2, probed 2026-11-12)."""
from types import SimpleNamespace as NS

import pytest

from langfuse_lab.verify import FeatureUnavailable, NotYet, check_module_07, module_07_checks

from .test_verify_modules import FakeObservations, obs

LIVE = "live-eval"
EVAL_IDS = {"routing_judge": "ev-r", "helpfulness_judge": "ev-h"}


def connection():
    return {"id": "conn1", "provider": "azure-openai", "adapter": "azure", "displaySecretKey": "...Z02w",
            "baseURL": "https://res.cognitiveservices.azure.com/openai", "customModels": ["gpt-5.2-chat", "gpt-4.1-mini"],
            "withDefaultModels": False, "extraHeaderKeys": [], "config": None}


def evaluator(name, dtype, *, mapping=None, status="active", model="gpt-5.2-chat", **out):
    return {"type": "llm_as_judge", "id": EVAL_IDS.get(name, f"ev-{name}"), "name": name, "status": status,
            "pausedMessage": None if status == "active" else "LLM connection missing", "version": 1,
            "prompt": [{"role": "user", "content": "..."}], "variables": ["input", "handoff", "answer"],
            "variableMapping": mapping if mapping is not None else [
                {"variable": "input", "source": "input"},
                {"variable": "handoff", "source": "metadata", "jsonPath": "$.handoff_target"},
                {"variable": "answer", "source": "output"}],
            "modelConfig": {"provider": "azure-openai", "model": model},
            "outputDefinition": {"dataType": dtype, **out}, "evaluationRuleAssignments": [{"evaluationRuleId": "rule1"}]}


def evaluators():
    return [evaluator("routing_judge", "BOOLEAN"),
            evaluator("helpfulness_judge", "NUMERIC", mapping=[{"variable": "input", "source": "input"},
                                                               {"variable": "output", "source": "output"}],
                      minValue=0.0, maxValue=1.0),
            evaluator("zz_probe_routing", "BOOLEAN")]  # someone else's evaluator: ignored


def rule(**kw):
    base = {"id": "rule1", "name": "support-live-eval", "enabled": True, "sampling": 1.0,
            "filter": [{"type": "arrayOptions", "column": "tags", "operator": "any of", "value": [LIVE]},
                       {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
                       {"type": "stringOptions", "column": "name", "operator": "any of", "value": ["support-request"]}],
            "evaluatorAssignments": [{"evaluatorId": "ev-r", "variableMapping": None},
                                     {"evaluatorId": "ev-h", "variableMapping": None}]}
    base.update(kw)
    return base


def judge(name, value, root_id, trace_id, *, comment="Policy team vs actual team.", evaluator_id=None):
    return {"id": f"{name}-{trace_id}", "name": name, "value": value, "source": "EVAL",
            "dataType": "BOOLEAN" if name == "routing_judge" else "NUMERIC", "comment": comment, "configId": None,
            "subject": {"kind": "observation", "id": root_id, "traceId": trace_id},
            "metadata": {"evaluator_id": evaluator_id or EVAL_IDS[name], "evaluation_rule_id": "rule1",
                         "job_execution_id": "j", "target_observation_id": root_id, "target_trace_id": trace_id}}


def code(value, trace_id, *, config=True, evaluator="code:v1"):
    return {"id": f"hc-{trace_id}", "name": "handoff_correct", "value": value, "source": "API", "dataType": "BOOLEAN",
            "comment": None, "configId": "c-hc" if config else None, "subject": {"kind": "trace", "id": trace_id},
            "metadata": {"evaluator": evaluator} if evaluator else {}}


def root(tid, *tags, live=True):
    return obs(id=f"r-{tid}", trace_id=tid, type="AGENT", name="support-request", is_root_observation=True,
               tags=["run:x", *tags, *([LIVE] if live else [])])


def run():
    """t1..t3 clean + t4 planted (all opted in), t5 not opted in."""
    items = [root("t1", "refund_double_charge"), root("t2", "shipping_status_no_handoff_needed"),
             root("t3", "kb_refund_policy"), root("t4", "reset_password_wrong_handoff", "wrong_handoff"),
             root("t5", "returns_policy_unjudged", live=False)]
    verdicts = {"t1": True, "t2": True, "t3": True, "t4": False}
    scores = {t: [judge("routing_judge", v, f"r-{t}", t), judge("helpfulness_judge", 0.8 if v else 0.1, f"r-{t}", t),
                  code(v, t)] for t, v in verdicts.items()}
    scores["t5"] = [code(True, "t5")]
    return items, scores, [connection()], evaluators(), [rule()]


AGREEMENT = {"n": 4, "agree": 4, "rate": 1.0, "judge_flagged": 1, "code_flagged": 1, "both_flagged": 1,
             "judge_missed": 0, "judge_false_alarm": 0}


class FakeScores:
    def __init__(self, by_trace):
        self.by_trace = by_trace

    def get_many(self, **kw):
        assert kw.get("fields") == "details,subject"
        return NS(data=self.by_trace.get(kw.get("trace_id"), []), meta=NS(cursor=None))


class CursorList:
    """v2 list endpoints: `list(limit=, cursor=)` with meta.cursor; serve 2 items per page to exercise paging."""
    def __init__(self, rows):
        self.rows = rows

    def list(self, limit=100, cursor=None):
        start = int(cursor or 0)
        page = self.rows[start:start + 2]
        nxt = str(start + 2) if start + 2 < len(self.rows) else None
        return NS(data=page, meta=NS(cursor=nxt))


class FakeConnections:
    def __init__(self, rows):
        self.rows = rows

    def list(self, page=1, limit=100):
        return NS(data=self.rows, meta=NS(total_pages=1))


def client(items, scores, conns, evals, rules):
    return NS(api=NS(observations=FakeObservations(items), scores=FakeScores(scores),
                     llm_connections=FakeConnections(conns), evaluators=CursorList(evals),
                     evaluation_rules=CursorList(rules)), flush=lambda: None)


def _call(check):
    try:
        return check.fn()
    except NotYet as exc:
        return ("not_yet", str(exc))
    except FeatureUnavailable as exc:
        return ("unavailable", str(exc))


def results(fx, **kw):
    kw.setdefault("agreement", AGREEMENT)
    return {c.name: _call(c) for c in module_07_checks("run:x", client(*fx), **kw)}


def test_all_pass_and_code_evaluator_unavailable():
    r = results(run())
    code_eval = r.pop("code evaluator (server-side)")
    assert all(v[0] is True for v in r.values()), r
    assert code_eval[0] == "unavailable" and "LANGFUSE_CODE_EVAL_DISPATCHER" in code_eval[1]


def test_prefix_scopes_every_name():
    items, scores, conns, evals, rules = run()
    p = "zz_ref_m07_"
    items = [obs(**{**o.__dict__, "tags": [p + t if t == LIVE else t for t in o.tags]}) for o in items]
    for rows in scores.values():
        for s in rows:
            s["name"] = p + s["name"]
    conns[0]["provider"] = p + "azure-openai"
    for e in evals:
        e["name"], e["modelConfig"]["provider"] = p + e["name"], p + "azure-openai"
    rules[0]["name"] = p + "support-live-eval"
    rules[0]["filter"][0]["value"] = [p + LIVE]
    r = results((items, scores, conns, evals, rules), prefix=p, live_tag=p + LIVE)
    r.pop("code evaluator (server-side)")
    assert all(v[0] is True for v in r.values()), r


def test_missing_connection_and_wrong_adapter_fail():
    fx = list(run())
    fx[2] = []
    assert results(fx)["LLM connection (Azure)"][0] is False
    fx[2] = [{**connection(), "adapter": "openai"}]
    assert results(fx)["LLM connection (Azure)"][0] is False


def test_duplicate_evaluator_fails_with_timeout_hint():
    fx = list(run())
    fx[3] = evaluators() + [{**evaluators()[0], "id": "ev-r2"}]
    ok, detail = results(fx)["judge evaluators defined once + active"]
    assert not ok and "timed out" in detail


def test_paused_evaluator_and_foreign_model_fail():
    fx = list(run())
    evals = evaluators()
    evals[0] = evaluator("routing_judge", "BOOLEAN", status="paused")
    evals[1] = {**evals[1], "modelConfig": {"provider": "azure-openai", "model": "gpt-4o"}}
    fx[3] = evals
    ok, detail = results(fx)["judge evaluators defined once + active"]
    assert not ok and "paused" in detail and "gpt-4o" in detail


def test_routing_judge_without_metadata_mapping_fails():
    fx = list(run())
    evals = evaluators()
    evals[0] = evaluator("routing_judge", "BOOLEAN", mapping=[{"variable": "input", "source": "input"}])
    fx[3] = evals
    ok, detail = results(fx)["judge evaluators defined once + active"]
    assert not ok and "jsonPath" in detail


def test_rule_with_tracename_and_no_root_filter_fails():
    fx = list(run())
    fx[4] = [rule(filter=[{"type": "arrayOptions", "column": "tags", "operator": "any of", "value": [LIVE]},
                          {"type": "stringOptions", "column": "traceName", "operator": "any of",
                           "value": ["support-request"]}])]
    ok, detail = results(fx)["evaluation rule (filters, sampling, assignments)"]
    assert not ok and "isRootObservation" in detail and "traceName" in detail and "name any of" in detail


def test_rule_sampling_disabled_and_unassigned_fail():
    fx = list(run())
    fx[4] = [rule(enabled=False, sampling=0.5, evaluatorAssignments=[{"evaluatorId": "ev-r"}])]
    ok, detail = results(fx)["evaluation rule (filters, sampling, assignments)"]
    assert not ok and "disabled" in detail and "sampling" in detail and "helpfulness_judge is not assigned" in detail


def test_missing_judge_score_is_not_yet():
    items, scores, *rest = run()
    scores["t2"] = [s for s in scores["t2"] if s["name"] != "helpfulness_judge"]
    r = results((items, scores, *rest))
    assert r["every opted-in trace judged on its root observation"][0] == "not_yet"


def test_judge_score_on_wrong_observation_fails():
    items, scores, *rest = run()
    scores["t1"][0]["subject"]["id"] = "child-span"
    ok, detail = results((items, scores, *rest))["every opted-in trace judged on its root observation"]
    assert not ok and "r-t1" in detail


def test_unjudged_trace_scored_fails_and_waits_for_judged_first():
    items, scores, *rest = run()
    scores["t5"].append(judge("routing_judge", True, "r-t5", "t5"))
    ok, detail = results((items, scores, *rest))["rule filter leaves other traffic alone"]
    assert not ok and "t5" in detail
    items, scores, *rest = run()
    scores["t1"] = [s for s in scores["t1"] if s["name"] != "routing_judge"]
    assert results((items, scores, *rest))["rule filter leaves other traffic alone"][0] == "not_yet"


def test_calibration_mismatch_and_missing_report_fail():
    fx = run()
    ok, detail = results(fx, agreement={**AGREEMENT, "agree": 3})["judge calibrated against the code score"]
    assert not ok and "recomputed" in detail
    ok, detail = results(fx, agreement=None)["judge calibrated against the code score"]
    assert not ok and "agreement=REPORT" in detail


def test_calibration_counts_disagreement():
    items, scores, *rest = run()
    scores["t2"][0]["value"] = False  # judge flags a clean trace
    report = {**AGREEMENT, "agree": 3, "judge_flagged": 2}
    assert results((items, scores, *rest), agreement=report)["judge calibrated against the code score"][0] is True


def test_code_score_without_config_or_provenance_fails():
    items, scores, *rest = run()
    scores["t3"][2] = code(True, "t3", config=False, evaluator=None)
    ok, detail = results((items, scores, *rest))["judge calibrated against the code score"]
    assert not ok and "send_score" in detail


def test_planted_passed_by_judge_fails():
    items, scores, *rest = run()
    scores["t4"][0]["value"] = True
    ok, detail = results((items, scores, *rest))["planted wrong_handoff caught by the routing judge"]
    assert not ok and "t4" in detail


@pytest.mark.parametrize("value", [False, 0, 0.0, "false"])
def test_planted_false_in_any_shape(value):
    items, scores, *rest = run()
    scores["t4"][0]["value"] = value
    assert results((items, scores, *rest))["planted wrong_handoff caught by the routing judge"][0] is True


def test_clean_trace_judged_false_is_noted():
    items, scores, *rest = run()
    scores["t2"][0]["value"] = False
    ok, detail = results((items, scores, *rest))["planted wrong_handoff caught by the routing judge"]
    assert ok and "t2" in detail


def test_planted_not_opted_in_fails():
    items, scores, *rest = run()
    items[3] = root("t4", "reset_password_wrong_handoff", "wrong_handoff", live=False)
    assert results((items, scores, *rest))["planted wrong_handoff caught by the routing judge"][0] is False


def test_code_evaluator_present_and_scoring_passes():
    items, scores, conns, evals, rules = run()
    evals.append({"type": "code", "id": "ev-c", "name": "handoff_code", "status": "active",
                  "sourceCode": "function evaluate(ctx) {}", "sourceCodeLanguage": "TYPESCRIPT"})
    rules[0]["evaluatorAssignments"].append({"evaluatorId": "ev-c", "variableMapping": None})
    scores["t1"].append(judge("routing_judge", True, "r-t1", "t1", evaluator_id="ev-c") | {"name": "handoff_code_ok"})
    assert results((items, scores, conns, evals, rules))["code evaluator (server-side)"][0] is True


def test_check_module_07_prints_passed(capsys):
    assert check_module_07("run:x", client=client(*run()), agreement=AGREEMENT, retries=1)
    out = capsys.readouterr().out
    assert "MODULE PASSED" in out and "⚠️ code evaluator" in out
