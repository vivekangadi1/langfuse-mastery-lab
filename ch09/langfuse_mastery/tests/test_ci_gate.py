"""Module 10: tests for langfuse_mastery/ci_gate.py. The decide() tests also need module 09's experiments.gate()."""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from langfuse_mastery import ci_gate as cg
from langfuse_mastery import experiments

SHA = "0123456789ABCDEF0123456789abcdef01234567"
GH_PR = {"GITHUB_ACTIONS": "true", "GITHUB_SHA": "f" * 40, "GATE_HEAD_SHA": SHA, "GITHUB_REF": "refs/pull/42/merge",
         "GITHUB_HEAD_REF": "fix-login-prompt", "GITHUB_REF_NAME": "42/merge", "GITHUB_RUN_ID": "987654",
         "GITHUB_RUN_ATTEMPT": "2", "CANDIDATE_LABEL": "candidate"}


# ---------------------------------------------------------------- ci_context
def test_ci_context_on_a_pull_request():
    ctx = cg.ci_context(GH_PR, local_sha="a" * 40, local_branch="main")
    assert ctx == {"git_sha": SHA.lower(), "short_sha": "0123456", "branch": "fix-login-prompt", "pr": "42",
                   "ci_run": "987654.2", "candidate_label": "candidate", "trigger": "ci", "run_tag": "ci:987654.2"}


def test_ci_context_push_uses_github_sha_and_ref_name():
    env = {"GITHUB_ACTIONS": "true", "GITHUB_SHA": "b" * 40, "GITHUB_REF": "refs/heads/main", "GITHUB_REF_NAME": "main",
           "GITHUB_RUN_ID": "5"}
    ctx = cg.ci_context(env)
    assert (ctx["git_sha"], ctx["branch"], ctx["pr"], ctx["ci_run"]) == ("b" * 40, "main", "", "5.1")
    assert ctx["candidate_label"] == "staging"


def test_ci_context_local_run():
    env = {"GATE_RUN_ID": "m10-abc-staging", "GATE_RUN_TAG": "run:m10-abc", "CANDIDATE_LABEL": "staging",
           "GITHUB_HEAD_REF": "", "GATE_HEAD_SHA": ""}
    ctx = cg.ci_context(env, local_sha="c" * 40, local_branch="langfuse-mastery")
    assert ctx == {"git_sha": "c" * 40, "short_sha": "ccccccc", "branch": "langfuse-mastery", "pr": "",
                   "ci_run": "m10-abc-staging", "candidate_label": "staging", "trigger": "local",
                   "run_tag": "run:m10-abc"}


@pytest.mark.parametrize("env,local_sha", [({"GATE_RUN_ID": "x"}, None), ({"GATE_RUN_ID": "x"}, "not-a-sha"),
                                            ({"GATE_RUN_ID": "x", "GITHUB_SHA": "abc"}, None)])
def test_ci_context_needs_a_sha(env, local_sha):
    with pytest.raises(ValueError):
        cg.ci_context(env, local_sha=local_sha)


def test_ci_context_needs_a_run_id():
    with pytest.raises(ValueError):
        cg.ci_context({}, local_sha="d" * 40)


# ---------------------------------------------------------------- names + metadata
CTX = {"git_sha": "c" * 40, "short_sha": "ccccccc", "branch": "b", "pr": "", "ci_run": "r1", "candidate_label": "staging",
       "trigger": "local", "run_tag": "run:t"}


def test_run_name():
    assert cg.run_name(CTX, "candidate", "staging", 2) == "ci staging · candidate staging v2 · ccccccc · r1"
    assert cg.run_name(CTX, "rerun", "production", 1, attempt=2) == "ci staging · rerun production v1 · ccccccc · r1 #2"


def test_run_names_are_unique_per_role_side_and_attempt():
    names = {cg.run_name(CTX, role, label, 1, attempt=a) for role in ("baseline", "candidate", "rerun")
             for label in ("production", "staging") for a in (0, 1, 2)}
    assert len(names) == 18


def test_experiment_metadata_stringifies():
    v = datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc)
    meta = cg.experiment_metadata(CTX, role="rerun", label="production", prompt_version=1, dataset_version=v,
                                  attempt=1, rerun_of="exp1", baseline_reused=True, note=None)
    assert meta == {"run_tag": "run:t", "trigger": "local", "git_sha": "c" * 40, "branch": "b", "pr": "", "ci_run": "r1",
                    "candidate_label": "staging", "role": "rerun", "prompt_label": "production", "prompt_version": "1",
                    "dataset_version": v.isoformat(), "attempt": "1", "rerun_of": "exp1", "baseline_reused": "true",
                    "note": ""}
    assert all(isinstance(x, str) for x in meta.values())


def test_experiment_metadata_keeps_a_string_version():
    meta = cg.experiment_metadata(CTX, role="baseline", label="production", prompt_version="1",
                                  dataset_version="2026-11-12T07:00:00+00:00")
    assert meta["dataset_version"] == "2026-11-12T07:00:00+00:00" and meta["role"] == "baseline"


# ---------------------------------------------------------------- pass rates + confirmation
def test_pass_rates():
    assert cg.pass_rates([{"a": True, "c": None}, {"a": False, "b": True}, {"a": True}]) == \
        {"a": pytest.approx(2 / 3), "b": 1.0, "c": None}
    assert cg.pass_rates([]) == {}


BASE = {"login": True, "locked": True, "refund": True, "invoice": False, "kb": True, "gone": True}
CAND = {"login": False, "locked": False, "refund": True, "invoice": True, "kb": True, "new": True}


def first():
    return experiments.compare_runs(BASE, CAND)  # module 09 (regressed: locked, login; improved: invoice)


def test_confirm_majority_vote():
    comparison = {"n": 5, "baseline_rate": 0.8, "candidate_rate": 0.6, "delta": -0.2, "regressed": ["locked", "login"],
                  "improved": ["invoice"], "missing": ["gone", "new"]}
    out = cg.confirm(comparison, [BASE, {"login": True, "locked": False}], [CAND, {"login": False, "locked": True}])
    assert out["suspects"] == ["locked", "login"]
    assert out["regressed"] == ["login"]          # baseline 1.0 vs candidate 0.0
    assert out["flaky"] == ["locked"]             # baseline 0.5 vs candidate 0.5: noise
    assert out["improved"] == ["invoice"] and out["missing"] == ["gone", "new"]
    assert out["n"] == 5 and out["rates"]["locked"] == [0.5, 0.5] and out["rates"]["refund"] == [1.0, 1.0]
    assert out["baseline_rate"] == pytest.approx((1 + 0.5 + 1 + 0 + 1) / 5)
    assert out["candidate_rate"] == pytest.approx((0 + 0.5 + 1 + 1 + 1) / 5)
    assert out["delta"] == pytest.approx(out["candidate_rate"] - out["baseline_rate"])


def test_confirm_without_reruns_confirms_every_suspect():
    comparison = {"n": 2, "baseline_rate": 1.0, "candidate_rate": 0.5, "delta": -0.5, "regressed": ["a"],
                  "improved": [], "missing": []}
    out = cg.confirm(comparison, [{"a": True, "b": True}], [{"a": False, "b": True}])
    assert out["regressed"] == ["a"] and out["flaky"] == []


def test_confirm_candidate_majority_pass_is_flaky():
    comparison = {"n": 1, "baseline_rate": 1.0, "candidate_rate": 0.0, "delta": -1.0, "regressed": ["a"],
                  "improved": [], "missing": []}
    out = cg.confirm(comparison, [{"a": True}, {"a": True}, {"a": True}], [{"a": False}, {"a": True}, {"a": True}])
    assert out["regressed"] == [] and out["flaky"] == ["a"]


def test_confirm_with_nothing_compared():
    out = cg.confirm({"n": 0, "baseline_rate": None, "candidate_rate": None, "delta": None, "regressed": [],
                      "improved": [], "missing": ["a"]}, [{"a": None}], [{"a": True}])
    assert out["n"] == 0 and out["baseline_rate"] is None and out["delta"] is None and out["missing"] == ["a"]


def test_confirm_on_compare_runs_output():
    out = cg.confirm(first(), [BASE, {"login": True, "locked": True}], [CAND, {"login": False, "locked": False}])
    assert out["regressed"] == ["locked", "login"] and out["flaky"] == []


# ---------------------------------------------------------------- decide
def confirmed(regressed=(), delta=0.0, n=6, flaky=()):
    return {"n": n, "baseline_rate": 0.8, "candidate_rate": None if delta is None else 0.8 + delta, "delta": delta, "regressed": list(regressed),
            "flaky": list(flaky), "suspects": sorted([*regressed, *flaky]), "improved": [], "missing": [], "rates": {}}


def test_decide_ship():
    assert cg.decide(confirmed(flaky=["x"])) == {"decision": "ship", "reasons": [], "exit_code": 0}


def test_decide_block_on_a_confirmed_regression():
    d = cg.decide(confirmed(regressed=["login"], delta=-0.17))
    assert d["decision"] == "block" and d["exit_code"] == 1 and len(d["reasons"]) == 1 and "1" in d["reasons"][0]


def test_decide_tolerances():
    assert cg.decide(confirmed(regressed=["a"]), max_regressions=1)["decision"] == "ship"
    assert cg.decide(confirmed(delta=-0.3))["decision"] == "block"
    assert cg.decide(confirmed(delta=-0.3), max_drop=0.5)["decision"] == "ship"


def test_decide_error_when_nothing_compared():
    d = cg.decide(confirmed(n=0, delta=None))
    assert d["decision"] == "error" and d["exit_code"] == 2 and len(d["reasons"]) == 1


# ---------------------------------------------------------------- rows + reports
INFO = {"login": {"case_id": "reset_password", "message": "I cannot log in", "expected": "account_security"},
        "locked": {"case_id": "locked_out", "message": "locked out", "expected": "account_security"},
        "refund": {"case_id": "refund", "message": "charged twice", "expected": "billing_specialist"},
        "invoice": {"case_id": "invoice", "message": "invoice", "expected": "none"},
        "kb": {"case_id": "kb", "message": "policy", "expected": "none"}}


def rows():
    c = {"n": 5, "baseline_rate": 0.7, "candidate_rate": 0.6, "delta": -0.1, "regressed": ["login"],
         "flaky": ["locked"], "suspects": ["locked", "login"], "improved": ["invoice"], "missing": ["gone"],
         "rates": {"login": [1.0, 0.0], "locked": [0.5, 0.5], "refund": [1.0, 1.0], "invoice": [0.0, 1.0],
                   "kb": [0.0, 0.0]}}
    return cg.report_rows(c, INFO)


def test_report_rows_status_and_order():
    got = [(r["case_id"], r["status"]) for r in rows()]
    assert got == [("reset_password", "regressed"), ("locked_out", "flaky"), ("gone", "missing"), ("kb", "fail"),
                   ("invoice", "improved"), ("refund", "pass")]
    login = rows()[0]
    assert login == {"id": "login", "case_id": "reset_password", "message": "I cannot log in",
                     "expected": "account_security", "baseline": 1.0, "candidate": 0.0, "status": "regressed"}
    gone = rows()[2]
    assert (gone["baseline"], gone["candidate"], gone["expected"], gone["message"]) == (None, None, None, "")


BLOCK = {"decision": "block", "reasons": ["1 regressed item(s) > 0 allowed"], "exit_code": 1}
SHIP = {"decision": "ship", "reasons": [], "exit_code": 0}


def test_junit_xml_block():
    text = cg.junit_xml(BLOCK, rows(), suite="release-gate staging")
    assert text.startswith('<?xml version="1.0" encoding="utf-8"?>')
    root = ET.fromstring(text.split("?>", 1)[1])
    assert root.tag == "testsuite" and root.get("name") == "release-gate staging"
    cases = root.findall("testcase")
    assert len(cases) == 7 and root.get("tests") == "7"
    assert cases[0].get("name") == "release gate" and cases[0].find("failure") is not None
    assert "1 regressed" in cases[0].find("failure").get("message")
    by_name = {c.get("name"): c for c in cases}
    assert by_name["reset_password"].find("failure") is not None
    assert "account_security" in by_name["reset_password"].find("failure").get("message")
    assert by_name["gone"].find("error") is not None
    assert by_name["locked_out"].find("system-out") is not None and by_name["locked_out"].find("failure") is None
    assert len(list(by_name["refund"])) == 0
    assert (root.get("failures"), root.get("errors")) == ("2", "1")


def test_junit_xml_ship_and_error_and_escaping():
    ship = ET.fromstring(cg.junit_xml(SHIP, [], suite="s").split("?>", 1)[1])
    assert ship.get("tests") == "1" and ship.get("failures") == "0" and ship.get("errors") == "0"
    err = {"decision": "error", "reasons": ['nothing "compared" <n == 0>'], "exit_code": 2}
    root = ET.fromstring(cg.junit_xml(err, [], suite="s").split("?>", 1)[1])
    assert root.get("errors") == "1" and root.find("testcase/error").get("message") == 'nothing "compared" <n == 0>'


def test_markdown_summary_block():
    md = cg.markdown_summary(BLOCK, rows(), title="Release gate: staging v2", links={"candidate": "http://x/e2"})
    lines = md.splitlines()
    assert lines[0] == "## ❌ Release gate: staging v2: block"
    assert "- 1 regressed item(s) > 0 allowed" in lines
    assert "| item | expected | baseline | candidate | status |" in lines
    assert "| reset_password | account_security | 1.00 | 0.00 | regressed |" in lines
    assert "| gone | None | - | - | missing |" in lines
    assert not any(l.startswith("| refund ") for l in lines)
    assert "1/6 item(s) pass on the candidate." in lines and "- [candidate](http://x/e2)" in lines
    assert md.endswith("\n")


def test_markdown_summary_ship_without_table():
    passing = [r for r in rows() if r["status"] == "pass"]
    md = cg.markdown_summary(SHIP, passing, title="Release gate: candidate v3")
    assert md.splitlines()[0] == "## ✅ Release gate: candidate v3: ship"
    assert "- no confirmed regressions" in md and "| item |" not in md and "1/1 item(s) pass" in md
    err = cg.markdown_summary({"decision": "error", "reasons": ["nothing compared"], "exit_code": 2}, [], title="G")
    assert err.startswith("## ⚠️ G: error")
