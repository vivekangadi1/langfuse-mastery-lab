"""Module 14 lab support: loop traffic, the two fix prompts, the oracles that follow the prompt's rules, the loop spec
(names, audit lines, the evidence schema) and the incident report renderers."""
import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from langfuse_lab import loop_spec, oracle_llm, prompts, regression, traffic
from langfuse_mastery.ci import harness


# ---------------------------------------------------------------- traffic
def test_loop_cases_three_windows_of_the_same_three_requests():
    cases = traffic.loop_cases(run_id="m14-t")
    assert len(cases) == 9 and len({c.conversation_id for c in cases}) == 9
    by_window = {}
    for c in cases:
        by_window.setdefault(c.window, []).append(c)
    assert list(by_window) == ["baseline", "incident", "recovered"]
    assert all([c.message for c in ws] == [c.message for c in by_window["baseline"]] for ws in by_window.values())
    assert [c.served for c in cases[::3]] == ["good", "bad", "fix"]
    assert by_window["incident"][0].tags == ("login_reset", "window:incident")
    login = [c for c in by_window["baseline"] if c.case_id in traffic.LOOP_INCIDENT_CASES]
    assert len(login) == 2 and {c.expected_handoff for c in login} == {"account_security"}


def test_the_incident_requests_are_new_to_the_seeded_dataset():
    seeded = {it["input"]["message"] for it in regression.seed_items("support-regression")}
    loop = {m for cid, _u, m, _e in traffic.LOOP_DEMO if cid in traffic.LOOP_INCIDENT_CASES}
    assert loop and not loop & seeded  # confirmed failures become NEW items, not upserts of curated ones


# ---------------------------------------------------------------- prompts + oracles
def test_quick_fix_repairs_login_but_breaks_billing_and_the_fix_does_not():
    assert oracle_llm.login_team(prompts.WRONG_HANDOFF_TEMPLATE) == "billing_specialist"
    assert oracle_llm.login_team(prompts.QUICK_FIX_TEMPLATE) == "account_security"
    assert oracle_llm.billing_team(prompts.QUICK_FIX_TEMPLATE) == "account_security"
    assert oracle_llm.login_team(prompts.LOOP_FIX_TEMPLATE) == "account_security"
    assert oracle_llm.billing_team(prompts.LOOP_FIX_TEMPLATE) == "billing_specialist"
    assert "billing_specialist" not in prompts.QUICK_FIX_TEMPLATE
    assert len({prompts.SYSTEM_PROMPT_TEMPLATE, prompts.WRONG_HANDOFF_TEMPLATE, prompts.CLEAN_CANDIDATE_TEMPLATE,
                prompts.QUICK_FIX_TEMPLATE, prompts.LOOP_FIX_TEMPLATE}) == 5
    assert prompts.HOTFIX_LABEL == "hotfix"


def test_oracle_misroutes_double_charges_on_the_quick_fix_only():
    msg = [HumanMessage(content="I was charged twice for order 8129. Please fix it.")]
    for template, team in ((prompts.SYSTEM_PROMPT_TEMPLATE, "billing_specialist"),
                           (prompts.QUICK_FIX_TEMPLATE, "account_security"),
                           (prompts.LOOP_FIX_TEMPLATE, "billing_specialist")):
        first = oracle_llm.decide([SystemMessage(content=template), *msg])
        assert first.tool_calls[0]["name"] == "lookup_order"
        assert oracle_llm.plan_calls(msg[0].content, billing_to=oracle_llm.billing_team(template))[-1][1]["target"] == team
    assert oracle_llm.billing_team(None) == "billing_specialist"


def test_harness_dry_oracle_reads_the_served_prompt_and_module_10_is_unchanged():
    # module 10: staging sends login to billing, everything else is the expected team
    assert harness._oracle("staging", "account_security") == "billing_specialist"
    assert harness._oracle("candidate", "account_security") == "account_security"
    staging = prompts.render(prompts.WRONG_HANDOFF_TEMPLATE, prompts.PROMPT_VARIABLES)
    assert harness._oracle("staging", "account_security", staging) == "billing_specialist"
    assert harness._oracle("staging", "billing_specialist", staging) == "billing_specialist"
    quick = prompts.render(prompts.QUICK_FIX_TEMPLATE, prompts.PROMPT_VARIABLES)
    assert harness._oracle("hotfix", "billing_specialist", quick) == "account_security"
    assert harness._oracle("hotfix", "account_security", quick) == "account_security"
    assert harness._oracle("hotfix", "none", quick) == "none"
    fix = prompts.render(prompts.LOOP_FIX_TEMPLATE, prompts.PROMPT_VARIABLES)
    assert [harness._oracle("hotfix", e, fix) for e in ("account_security", "billing_specialist", "none")] == \
        ["account_security", "billing_specialist", "none"]


# ---------------------------------------------------------------- loop spec
def test_names_are_prefixed_and_audit_lines_round_trip():
    n = loop_spec.names("zz_")
    assert n["queue"] == "zz_incident-review" and n["prompt"] == "zz_support-agent-system"
    assert n["judge"] == "zz_routing_judge" and loop_spec.names()["expected"] == "expected_handoff"
    line = loop_spec.audit_text("run:m14-x", "promote", "v5 -> production + live")
    assert line == "[run:m14-x] promote: v5 -> production + live"
    assert loop_spec.parse_audit(line) == ("run:m14-x", "promote", "v5 -> production + live")
    assert loop_spec.parse_audit("a human comment") is None
    assert loop_spec.parse_audit("[run:m14-x] lunch: soon") is None
    with pytest.raises(ValueError):
        loop_spec.audit_text("run:m14-x", "lunch", "soon")
    assert loop_spec.incident_id("m14-abc123") == "INC-m14-abc123"


def sample_evidence():
    ui = "http://localhost:3000/project/p"
    at = {s: f"2026-11-12T10:{i:02d}:00+00:00" for i, s in enumerate(loop_spec.STAGE_NAMES)}
    ev = []
    for stage in loop_spec.STAGE_NAMES:
        numbers, refs, links = loop_spec.EVENT_SCHEMA[stage]
        ev.append({"stage": stage, "at": at[stage], "title": f"{stage} happened",
                   "numbers": {k: 1 for k in numbers}, "refs": {k: "x" for k in refs},
                   "links": {k: f"{ui}/{stage}/{k}" for k in links}})
    gate = next(e for e in ev if e["stage"] == "gate")
    gate["numbers"].update(decision="block", version=4, regressed=["refund_double_charge"])
    ev.append({**gate, "numbers": {**gate["numbers"], "decision": "ship", "version": 5, "regressed": []}})
    return {"incident": "INC-m14-t", "run_tag": "run:m14-t", "prompt": "support-agent-system",
            "environment": "production", "judge_mode": "stand-in", "events": ev,
            "durations": {"time_to_detect_s": 180, "time_to_review_s": 60, "time_to_fix_s": 420,
                          "time_to_recover_s": 660}, "problems": []}


def test_evidence_schema_accepts_a_complete_trail_and_names_what_is_missing():
    ev = sample_evidence()
    assert loop_spec.evidence_problems(ev) == []
    broken = json.loads(json.dumps(ev))
    broken["events"] = [e for e in broken["events"] if e["stage"] != "resolved"]
    del broken["events"][0]["numbers"]["judge_pass_rate"]
    broken["events"][1]["at"] = "yesterday"
    del broken["durations"]
    probs = loop_spec.evidence_problems(broken)
    assert "missing key: durations" in probs
    assert "baseline: numbers lacks 'judge_pass_rate'" in probs
    assert any(p.startswith("deploy: 'at' is not an ISO timestamp") for p in probs)
    assert "no event for stage 'resolved'" in probs
    assert loop_spec.evidence_problems([]) == ["evidence is not a dict"]


def test_reports_render_the_headline_timeline_and_links(tmp_path):
    ev = sample_evidence()
    md = loop_spec.render_markdown(ev)
    assert md.startswith("# Incident INC-m14-t") and "| time | stage |" in md
    assert "3 min 00 s after the deploy" in md and "v4 blocked by the gate (refund_double_charge)" in md
    assert "[queue](http://localhost:3000/project/p/review/queue)" in md
    page = loop_spec.render_html(ev)
    assert page.startswith("<!doctype html>") and "prefers-color-scheme:dark" in page
    assert 'class="block"' in page and 'class="ship"' in page and "11 min 00 s" in page
    paths = loop_spec.write_report(ev, tmp_path / "out")
    assert sorted(p.name for p in paths.values()) == sorted(loop_spec.FILES.values())
    assert json.loads(paths["evidence"].read_text(encoding="utf-8"))["incident"] == "INC-m14-t"
