import pytest

from langfuse_lab import traffic
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.traffic import (
    PII_SUFFIX, cost_demo_conversations, demo_conversations, estimate_llm_calls, main, plan_traffic, run_traffic, score_demo_cases,
    score_demo_session,
)


def test_plan_is_deterministic_and_sized():
    a, b = plan_traffic(20, seed=1), plan_traffic(20, seed=1)
    assert a == b and len(a) == 20
    assert all(spec.user_id.startswith("cust_") and spec.turns for spec in a)


def test_ids_are_run_scoped():
    v1, v2 = plan_traffic(30, seed=1, release="v1"), plan_traffic(30, seed=1, release="v2")
    assert {s.conversation_id for s in v1}.isdisjoint(s.conversation_id for s in v2)
    assert {s.session_id for s in v1}.isdisjoint(s.session_id for s in v2)
    assert all(s.run_id == "dev-v1-s1" and s.conversation_id.startswith("dev-v1-s1-c") for s in v1)
    assert plan_traffic(5, seed=1, run_id="mine")[0].conversation_id == "mine-c000"


def test_session_ids_are_unique_per_conversation():
    assert len({s.session_id for s in plan_traffic(100, seed=0)}) == 100


def test_some_conversations_are_multi_turn_sessions():
    plan = plan_traffic(40, seed=2)
    multi = [s for s in plan if s.multi_turn]
    assert multi and all(len(s.turns) > 1 for s in multi)
    assert all(s.intent.startswith("s_") and s.session_id == f"{s.conversation_id}-{s.intent}" for s in multi)
    assert all(not s.multi_turn and len(s.turns) == 1 for s in plan if s not in multi)


def test_pii_failure_appends_card_number_only_to_billing_conversations():
    plan = plan_traffic(40, seed=3, fail=["pii"])
    with_pii = [s for s in plan if PII_SUFFIX in s.turns[0]]
    assert with_pii and all(s.intent in ("double_charge", "s_refund_followup") for s in with_pii)
    assert all(s in with_pii for s in plan if s.intent in ("double_charge", "s_refund_followup"))
    assert all(spec.failures == ("pii",) for spec in plan)


def test_bare_string_fail_is_one_mode():
    assert all(spec.failures == ("pii",) for spec in plan_traffic(5, seed=3, fail="pii"))


def test_tags():
    plan = plan_traffic(40, seed=2, fail=["slow_tool"], env="ci", release="v9")
    single = next(s for s in plan if not s.multi_turn)
    assert single.tags == (single.intent, "slow_tool", "env:ci", "release:v9")
    multi = next(s for s in plan if s.multi_turn)
    assert multi.tags == (multi.intent, "slow_tool", "env:ci", "release:v9", "multi_turn")


def test_login_traffic_is_exposed():
    logins = [s for s in plan_traffic(20, seed=0) if s.intent in ("password_reset", "s_login_then_2fa")]
    assert len(logins) >= 2


def test_estimate_excludes_injection_turns():
    plan = plan_traffic(40, seed=4)
    assert any(s.intent == "injection" for s in plan)
    assert estimate_llm_calls(plan) == 3 * sum(len(s.turns) for s in plan if s.intent != "injection")


def test_run_traffic_isolates_errors():
    plan = plan_traffic(3, seed=5)
    def runner(spec):
        if spec is plan[1]:
            raise RuntimeError("boom")
        return "ok"
    results = run_traffic(plan, runner)
    assert [r.ok for r in results] == [True, False, True]


def test_cli_dry_run(capsys):
    assert main(["--n", "5", "--dry-run"]) == 0
    assert "estimated agent LLM calls: <=" in capsys.readouterr().out


def test_cli_rejects_runner_without_colon(capsys):
    with pytest.raises(SystemExit):
        main(["--n", "1", "--runner", "nocolon"])
    assert "module:function" in capsys.readouterr().err


def test_demo_conversations_shape():
    demo = demo_conversations(run_id="m02-a")
    assert [s.intent for s in demo] == ["s_refund_followup", "s_login_then_2fa", "policy"]
    assert sum(len(s.turns) for s in demo) == 5
    assert [s.user_id for s in demo] == ["cust_00", "cust_02", "cust_05"]
    assert [s.multi_turn for s in demo] == [True, True, False]
    assert demo[2].turns == ("What is your refund policy?",)
    assert demo == demo_conversations(run_id="m02-a")


def test_demo_conversations_ids_are_run_scoped():
    a, b = demo_conversations(run_id="m02-a"), demo_conversations(run_id="m02-b")
    assert [s.conversation_id for s in a] == ["m02-a-c000", "m02-a-c001", "m02-a-c002"]
    assert [s.session_id for s in a] == ["m02-a-c000-s_refund_followup", "m02-a-c001-s_login_then_2fa", "m02-a-c002"]
    assert {s.session_id for s in a}.isdisjoint(s.session_id for s in b)
    assert all(s.run_id == "m02-a" for s in a)


def test_demo_conversations_tags_env_release():
    demo = demo_conversations(run_id="m02-a", env="prod", release="v2")
    assert demo[0].tags == ("s_refund_followup", "env:prod", "release:v2", "multi_turn")
    assert demo[2].tags == ("policy", "env:prod", "release:v2")
    assert demo_conversations(run_id="x")[0].env == "staging"


def test_score_demo_cases_shape():
    cases = score_demo_cases(run_id="m06-a")
    assert [c.case_id for c in cases] == ["refund_double_charge", "shipping_status_no_handoff_needed",
                                          "kb_refund_policy", "reset_password_wrong_handoff"]
    assert [c.failures for c in cases].count(("wrong_handoff",)) == 1
    assert cases[3].failures == ("wrong_handoff",)
    assert [c.expected_kb for c in cases] == [None, None, "kb_refunds", None]
    assert all(c.message and c.reference_answer for c in cases)
    assert cases == score_demo_cases(run_id="m06-a")


def test_score_demo_cases_match_fixture_expectations():
    fixture = {c["case_id"]: c for c in load_fixtures().cases}
    for case in score_demo_cases(run_id="m06-a"):
        if case.case_id in fixture:
            assert case.expected_handoff == fixture[case.case_id]["expected_handoff"]
            assert case.message == fixture[case.case_id]["customer_request"]
    assert {a["id"] for a in load_fixtures().kb} >= {"kb_refunds"}


def test_score_demo_ids_are_run_scoped():
    a, b = score_demo_cases(run_id="m06-a"), score_demo_cases(run_id="m06-b")
    assert [c.conversation_id for c in a] == [f"m06-a-c{i:03d}" for i in range(4)]
    assert all(c.session_id == c.conversation_id for c in a)
    assert {c.session_id for c in a}.isdisjoint(c.session_id for c in b)


def test_score_demo_session():
    spec = score_demo_session(run_id="m06-a")
    assert spec.turns and len(spec.turns) == 2 and spec.user_id == "cust_00" and spec.multi_turn
    assert spec.session_id == "m06-a-c004-s_refund_followup"
    assert spec.session_id not in {c.session_id for c in score_demo_cases(run_id="m06-a")}


def test_cost_demo_conversations_shape():
    plan = cost_demo_conversations(run_id="r1")
    assert len(plan) == 4 and sum(len(s.turns) for s in plan) == 7
    assert len({s.session_id for s in plan}) == 4 and len({s.user_id for s in plan}) == 4
    spikes = [s for s in plan if s.failures]
    assert [(s.intent, s.user_id, s.failures, s.release) for s in spikes] == [
        ("s_login_then_2fa", "cust_02", ("cost_spike",), "v2")]
    assert all(s.release == "v1" for s in plan if not s.failures)
    assert spikes[0].multi_turn and "cost_spike" in spikes[0].tags and "release:v2" in spikes[0].tags


def test_cost_demo_conversations_ids_are_run_scoped():
    a, b = cost_demo_conversations(run_id="ra"), cost_demo_conversations(run_id="rb")
    assert all(s.conversation_id.startswith("ra-c") and s.session_id.startswith("ra-c") for s in a)
    assert {s.session_id for s in a}.isdisjoint(s.session_id for s in b)


def test_cost_demo_llm_budget():
    assert estimate_llm_calls(cost_demo_conversations(run_id="r")) == 21


def test_prompt_demo_cases_phases():
    cases = traffic.prompt_demo_cases(run_id="m04-x")
    assert [c.phase for c in cases] == ["healthy", "healthy", "bad", "bad", "rollback", "rollback"]
    assert all(c.failures == ("wrong_handoff",) for c in cases if c.phase == "bad")
    assert not any(c.failures for c in cases if c.phase != "bad")
    login = [c for c in cases if c.phase in ("bad", "rollback")]
    assert all(c.expected_handoff == "account_security" for c in login)


def test_prompt_demo_cases_ids_and_tags():
    cases = traffic.prompt_demo_cases(run_id="m04-x")
    assert len({c.conversation_id for c in cases}) == 6
    assert all(c.conversation_id.startswith("m04-x-") and c.session_id == c.conversation_id for c in cases)
    assert cases[2].tags == ("phase:bad", "login_cust_02", "wrong_handoff")
    assert cases[0].tags == ("phase:healthy", "login_cust_02")


def test_mask_demo_conversations_plant_pii():
    plan = traffic.mask_demo_conversations(run_id="m05-x")
    assert [(s.intent, s.user_id, s.failures) for s in plan] == [
        ("double_charge", "cust_00", ("pii",)), ("s_refund_followup", "cust_00", ("pii",)),
        ("password_reset", "cust_02", ())]
    assert sum(len(s.turns) for s in plan) == 4
    for s in plan:
        assert ("pii" in s.tags) == bool(s.failures)
        assert s.turns[0].endswith(traffic.PII_SUFFIX) == ("pii" in s.failures)
        assert all(traffic.PII_SUFFIX not in t for t in s.turns[1:])
    assert "8129" in plan[0].turns[0] and "8129" in plan[1].turns[0]


def test_mask_demo_ids_are_run_scoped():
    a, b = traffic.mask_demo_conversations(run_id="m05-a"), traffic.mask_demo_conversations(run_id="m05-b")
    up = traffic.upload_demo_case(run_id="m05-a")
    ids = [s.conversation_id for s in a] + [up.conversation_id]
    assert ids == [f"m05-a-c{i:03d}" for i in range(4)]
    assert {s.session_id for s in a}.isdisjoint(s.session_id for s in b)


def test_upload_demo_case_carries_phone_email_and_order():
    up = traffic.upload_demo_case(run_id="m05-a")
    assert up.intent == "damaged_item_photo" and up.user_id == "cust_05" and len(up.turns) == 1
    assert all(x in up.turns[0] for x in ("8115", "+1 415-555-0132", "farah@example.com"))
    assert not up.failures and traffic.PII_SUFFIX not in up.turns[0]


def test_mask_demo_llm_budget():
    plan = traffic.mask_demo_conversations(run_id="r") + [traffic.upload_demo_case(run_id="r")]
    assert estimate_llm_calls(plan) == 15


def test_live_eval_cases():
    cases = traffic.live_eval_cases(run_id="m07-a")
    assert len(cases) == 5
    assert [c.case_id for c in cases if "wrong_handoff" in c.failures] == ["reset_password_wrong_handoff"]
    unjudged = [c for c in cases if c.case_id in traffic.UNJUDGED_CASES]
    assert len(unjudged) == 1 and not unjudged[0].failures and unjudged[0].expected_handoff == "none"
    assert len({c.conversation_id for c in cases}) == 5
    assert all(c.conversation_id.startswith("m07-a-") for c in cases)
    assert {c.conversation_id for c in cases}.isdisjoint(c.conversation_id for c in traffic.live_eval_cases(run_id="m07-b"))
