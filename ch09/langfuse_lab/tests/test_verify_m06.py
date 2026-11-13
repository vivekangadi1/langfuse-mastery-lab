"""Module 06 verify checks against fakes shaped like the live API (server 4.77, fields="details,subject")."""
from types import SimpleNamespace as NS

import pytest

from langfuse_lab.verify import NotYet, check_module_06, module_06_checks

from .test_verify_modules import FakeObservations, obs

SESSION = "s-csat"
CFG = {"handoff_correct": "c-hc", "user_feedback": "c-fb", "kb_hit": "c-kb", "csat": "c-cs"}


def configs():
    return [
        {"id": "c-hc", "name": "handoff_correct", "dataType": "BOOLEAN", "isArchived": False,
         "minValue": None, "maxValue": None, "categories": None},
        {"id": "c-fb", "name": "user_feedback", "dataType": "CATEGORICAL", "isArchived": False,
         "minValue": None, "maxValue": None,
         "categories": [{"label": "thumbs_up", "value": 1.0}, {"label": "thumbs_down", "value": 0.0}]},
        {"id": "c-kb", "name": "kb_hit", "dataType": "NUMERIC", "isArchived": False, "minValue": 0.0, "maxValue": 1.0,
         "categories": None},
        {"id": "c-cs", "name": "csat", "dataType": "NUMERIC", "isArchived": False, "minValue": 1.0, "maxValue": 5.0,
         "categories": None},
        {"id": "c-old", "name": "kb_hit", "dataType": "NUMERIC", "isArchived": True, "minValue": 0.0, "maxValue": 1.0,
         "categories": None},  # archived duplicate: must be ignored
    ]


def score(name, value, *, kind="trace", target="t1", trace=None, dtype=None, comment=None, evaluator="code:v1",
          config=True):
    subject = {"kind": kind, "id": target, **({"traceId": trace} if trace else {})}
    return {"id": f"{name}-{target}", "name": name, "value": value, "dataType": dtype or "NUMERIC", "comment": comment,
            "configId": CFG.get(name) if config else None, "subject": subject,
            "metadata": {"evaluator": evaluator} if evaluator else {}}


def root(tid, *, session=None, tags=()):
    return obs(id=f"r-{tid}", trace_id=tid, type="AGENT", is_root_observation=True, session_id=session or tid,
               tags=["run:x", *tags])


def run():
    """4 cases (t4 planted) + 2 session turns. t3 retrieves; its kb-search span is kb3."""
    items = [root("t1"), root("t2"), root("t3"), root("t4", tags=["wrong_handoff"]),
             root("t5", session=SESSION), root("t6", session=SESSION),
             obs(id="kb3", trace_id="t3", type="RETRIEVER", name="kb-search")]
    scores = {
        "t1": [score("handoff_correct", True, target="t1", dtype="BOOLEAN"),
               score("user_feedback", "thumbs_up", target="t1", dtype="CATEGORICAL", evaluator=None)],
        "t2": [score("handoff_correct", True, target="t2", dtype="BOOLEAN")],
        "t3": [score("handoff_correct", True, target="t3", dtype="BOOLEAN"),
               score("kb_hit", 1.0, kind="observation", target="kb3", trace="t3")],
        "t4": [score("handoff_correct", False, target="t4", dtype="BOOLEAN", comment="expected account_security"),
               score("user_feedback", "thumbs_down", target="t4", dtype="CATEGORICAL", comment="wrong team",
                     evaluator=None),
               score("output", "I routed this to account security.", target="t4", dtype="CORRECTION",
                     evaluator=None, config=False)],
    }
    session = [score("csat", 4.0, kind="session", target=SESSION, evaluator=None, config=False)]
    return items, scores, session, configs()


class FakeScores:
    def __init__(self, by_trace, session):
        self.by_trace, self.session = by_trace, session

    def get_many(self, **kw):
        assert kw.get("fields") == "details,subject"
        if kw.get("session_id"):
            return NS(data=self.session if kw["session_id"] == SESSION else [], meta=NS(cursor=None))
        return NS(data=self.by_trace.get(kw.get("trace_id"), []), meta=NS(cursor=None))


class FakeConfigs:
    def __init__(self, rows):
        self.rows = rows

    def get(self, page=1, limit=100):
        return NS(data=self.rows, meta=NS(total_pages=1))


def client(items, scores, session, cfgs):
    return NS(api=NS(observations=FakeObservations(items), scores=FakeScores(scores, session),
                     score_configs=FakeConfigs(cfgs)), flush=lambda: None)


def results(items, scores, session, cfgs, **kw):
    return {c.name: _call(c) for c in module_06_checks("run:x", client(items, scores, session, cfgs),
                                                       session_id=SESSION, **kw)}


def _call(check):
    try:
        return check.fn()
    except NotYet as exc:
        return ("not_yet", str(exc))


def test_all_pass():
    r = results(*run())
    assert all(v[0] is True for v in r.values()), r
    assert "kb3" in r["observation-level score on kb-search"][1]


def test_duplicate_active_config_fails():
    items, scores, session, cfgs = run()
    cfgs.append({**cfgs[0], "id": "c-hc2"})
    ok, detail = results(items, scores, session, cfgs)["score configs defined once"]
    assert not ok and "duplicates" in detail


def test_wrong_config_range_and_categories_fail():
    items, scores, session, cfgs = run()
    cfgs[3] = {**cfgs[3], "maxValue": 10.0}
    cfgs[1] = {**cfgs[1], "categories": [{"label": "up", "value": 1}]}
    ok, detail = results(items, scores, session, cfgs)["score configs defined once"]
    assert not ok and "csat: range" in detail and "user_feedback: categories" in detail


def test_missing_trace_score_is_not_yet():
    items, scores, session, cfgs = run()
    scores["t2"] = []
    assert results(items, scores, session, cfgs)["every case has a trace-level code score"][0] == "not_yet"


def test_kb_hit_on_trace_instead_of_observation_fails():
    items, scores, session, cfgs = run()
    scores["t3"][1] = score("kb_hit", 1.0, kind="trace", target="t3")
    ok, detail = results(items, scores, session, cfgs)["observation-level score on kb-search"]
    assert not ok and "observation_id" in detail


def test_csat_on_trace_fails_and_out_of_range_fails():
    items, scores, _, cfgs = run()
    on_trace = [score("csat", 4.0, kind="trace", target="t5")]
    assert results(items, scores, on_trace, cfgs)["session-level score"][0] is False
    bad = [score("csat", 9.0, kind="session", target=SESSION)]
    assert results(items, scores, bad, cfgs)["session-level score"][0] is False


def test_two_feedback_votes_fail():
    items, scores, session, cfgs = run()
    scores["t4"].append(score("user_feedback", "thumbs_up", target="t4", dtype="CATEGORICAL", evaluator=None))
    ok, detail = results(items, scores, session, cfgs)["user feedback + correction (one vote per trace)"]
    assert not ok and "score_id" in detail


def test_missing_correction_is_not_yet():
    items, scores, session, cfgs = run()
    scores["t4"] = scores["t4"][:2]
    assert results(items, scores, session, cfgs)["user feedback + correction (one vote per trace)"][0] == "not_yet"


def test_thumbs_down_without_comment_fails():
    items, scores, session, cfgs = run()
    scores["t4"][1] = score("user_feedback", "thumbs_down", target="t4", dtype="CATEGORICAL", evaluator=None)
    assert results(items, scores, session, cfgs)["user feedback + correction (one vote per trace)"][0] is False


def test_unlinked_or_unattributed_scores_fail():
    items, scores, session, cfgs = run()
    scores["t2"] = [score("handoff_correct", True, target="t2", dtype="BOOLEAN", config=False, evaluator=None)]
    ok, detail = results(items, scores, session, cfgs)["scores linked to configs + provenance"]
    assert not ok and "configId" in detail and "evaluator" in detail


def test_planted_scored_true_fails():
    items, scores, session, cfgs = run()
    scores["t4"][0] = score("handoff_correct", True, target="t4", dtype="BOOLEAN")
    ok, detail = results(items, scores, session, cfgs)["planted wrong_handoff caught by handoff_correct"]
    assert not ok and "t4" in detail


def test_clean_failure_is_reported_but_passes():
    items, scores, session, cfgs = run()
    scores["t2"] = [score("handoff_correct", False, target="t2", dtype="BOOLEAN")]
    ok, detail = results(items, scores, session, cfgs)["planted wrong_handoff caught by handoff_correct"]
    assert ok and "t2" in detail and "agent error" in detail


def test_no_planted_trace_fails():
    items, scores, session, cfgs = run()
    items = [root("t4") if o.id == "r-t4" else o for o in items]
    assert results(items, scores, session, cfgs)["planted wrong_handoff caught by handoff_correct"][0] is False


@pytest.mark.parametrize("value", [False, 0, 0.0, "false"])
def test_false_values_in_any_shape(value):
    items, scores, session, cfgs = run()
    scores["t4"][0] = {**scores["t4"][0], "value": value}
    assert results(items, scores, session, cfgs)["planted wrong_handoff caught by handoff_correct"][0] is True


def test_check_module_06_prints_passed(capsys):
    assert check_module_06("run:x", session_id=SESSION, client=client(*run()), retries=1)
    assert "MODULE PASSED" in capsys.readouterr().out
