"""Module 04 verify checks against fakes shaped like the live API (server 4.77, fields=...,prompt,model)."""
import json
from types import SimpleNamespace as NS

from langfuse_lab.prompts import PROMPT_CONFIG, PROMPT_NAME, SYSTEM_PROMPT_TEMPLATE, WRONG_HANDOFF_TEMPLATE
from langfuse_lab.verify import NotYet, check_module_04, module_04_checks

from .test_verify_modules import FakeObservations, obs


class PromptRow:
    """Shaped like Prompt_Text: .dict() gives camelCase keys."""

    def __init__(self, version, text, labels, config=None, type_="text"):
        self.d = {"type": type_, "prompt": text, "name": PROMPT_NAME, "version": version,
                  "config": dict(PROMPT_CONFIG) if config is None else config, "labels": list(labels), "tags": [],
                  "commitMessage": None}

    def dict(self):
        return dict(self.d)


class FakePrompts:
    def __init__(self, rows):
        self.rows = {r.d["version"]: r for r in rows}

    def list(self, name=None, limit=None):
        if not self.rows:
            return NS(data=[])
        labels = sorted({lab for r in self.rows.values() for lab in r.d["labels"]})
        return NS(data=[NS(name=PROMPT_NAME, versions=sorted(self.rows), labels=labels)])

    def get(self, name, version=None):
        return self.rows[version]


def prompt_rows():
    return [PromptRow(1, SYSTEM_PROMPT_TEMPLATE, ["production"]),
            PromptRow(2, WRONG_HANDOFF_TEMPLATE, ["staging", "latest"])]


def gen(gid, tid, version, *, name=PROMPT_NAME, params=None):
    return obs(id=gid, trace_id=tid, type="GENERATION", prompt_name=name, prompt_version=version,
               model_parameters=dict(PROMPT_CONFIG) if params is None else params)


def handoff(hid, tid, target):
    return obs(id=hid, trace_id=tid, type="TOOL", name="create_handoff", prompt_name="", prompt_version=None,
               input=json.dumps({"target": target, "reason": "x"}))


def root(tid, phase, *extra):
    return obs(id=f"r-{tid}", trace_id=tid, type="AGENT", is_root_observation=True, name="support-request",
               prompt_name="", prompt_version=None, tags=["run:x", f"phase:{phase}", *extra])


def turn(tid, phase, version, target, *extra):
    return [root(tid, phase, *extra), gen(f"{tid}-g1", tid, version), handoff(f"{tid}-h", tid, target),
            gen(f"{tid}-g2", tid, version)]


def run():
    return [*turn("t1", "healthy", 1, "account_security"), *turn("t2", "healthy", 1, "billing_specialist"),
            *turn("t3", "bad", 2, "billing_specialist", "wrong_handoff"),
            *turn("t4", "bad", 2, "account_security", "wrong_handoff"),
            *turn("t5", "rollback", 1, "account_security"), *turn("t6", "rollback", 1, "account_security")]


def results(items=None, rows=None, **kw):
    client = NS(api=NS(observations=FakeObservations(run() if items is None else items),
                       prompts=FakePrompts(prompt_rows() if rows is None else rows)), flush=lambda: None)
    out = {}
    for c in module_04_checks("run:x", client, **kw):
        try:
            out[c.name] = c.fn()
        except (NotYet, AssertionError) as exc:
            out[c.name] = ("retry", str(exc))
    return out


def test_all_pass():
    r = results()
    assert all(v[0] is True for v in r.values()), r
    assert "1 misrouted" in r["planted bad version caught by its generation links"][1]


def test_check_module_04_reports(capsys):
    client = NS(api=NS(observations=FakeObservations(run()), prompts=FakePrompts(prompt_rows())), flush=lambda: None)
    assert check_module_04("run:x", client=client, retries=1) is True
    assert "MODULE PASSED" in capsys.readouterr().out


def test_missing_prompt_is_retryable():
    r = results(rows=[])
    assert r["prompt versions created once (good + bad)"][0] == "retry"


def test_duplicate_version_fails():
    rows = prompt_rows() + [PromptRow(3, SYSTEM_PROMPT_TEMPLATE, ["latest"])]
    rows[1].d["labels"] = ["staging"]
    ok, detail = results(rows=rows)["prompt versions created once (good + bad)"]
    assert not ok and "never dedupes" in detail


def test_missing_config_fails():
    rows = prompt_rows()
    rows[1].d["config"] = {}
    ok, detail = results(rows=rows)["prompt versions created once (good + bad)"]
    assert not ok and "lack config" in detail


def test_production_left_on_bad_version_fails():
    rows = prompt_rows()
    rows[0].d["labels"], rows[1].d["labels"] = [], ["production", "staging", "latest"]
    ok, detail = results(rows=rows)["labels: production = good, staging = bad"]
    assert not ok and "roll back" in detail


def test_unlinked_generation_fails():
    items = run()
    items[1] = gen("t1-g1", "t1", None, name="")
    ok, detail = results(items=items)["every generation linked to a prompt version"]
    assert not ok and "propagate_attributes(prompt=" in detail


def test_config_not_applied_fails():
    items = run()
    items[1] = gen("t1-g1", "t1", 1, params={"temperature": 0})
    ok, detail = results(items=items)["prompt config applied to the LLM calls"]
    assert not ok and "max_tokens" in detail and "llm_params" in detail


def test_bad_phase_served_by_good_version_fails():
    items = run()
    items[9] = gen("t3-g1", "t3", 1)
    items[11] = gen("t3-g2", "t3", 1)
    ok, detail = results(items=items)["planted bad version caught by its generation links"]
    assert not ok and "not served by the bad v2" in detail


def test_bad_version_that_did_no_harm_fails():
    items = [o if o.id != "t3-h" else handoff("t3-h", "t3", "account_security") for o in run()]
    ok, detail = results(items=items)["planted bad version caught by its generation links"]
    assert not ok and "none was misrouted" in detail


def test_rollback_still_on_bad_version_fails():
    items = [gen(o.id, o.trace_id, 2) if o.trace_id == "t5" and o.type == "GENERATION" else o for o in run()]
    ok, detail = results(items=items)["rollback: new traffic served by the good version"]
    assert not ok and "t5 linked to v2" in detail


def test_rollback_misrouted_fails():
    items = [handoff("t6-h", "t6", "billing_specialist") if o.id == "t6-h" else o for o in run()]
    ok, detail = results(items=items)["rollback: new traffic served by the good version"]
    assert not ok and "t6 handed off to ['billing_specialist']" in detail


def test_partial_run_is_not_yet():
    items = [o for o in run() if o.trace_id != "t6"]
    r = results(items=items)
    assert all(v[0] == "retry" for v in r.values()), r
