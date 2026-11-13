from langfuse_lab import standin_judge as sj
from langfuse_lab import traffic


def test_policy_team_keywords():
    assert sj.policy_team("I was charged twice for order 8129.") == "billing_specialist"
    assert sj.policy_team("I cannot log in after changing my password.") == "account_security"
    assert sj.policy_team("My parcel is delayed") == "shipping_specialist"
    assert sj.policy_team("How long do returns take?") == "none"
    assert sj.policy_team("") == "none"


def test_planted_wrong_handoff_flagged_and_refund_policy_false_alarm():
    ok, why = sj.judge_routing("I cannot log in after changing my password.", "billing_specialist")
    assert ok is False and "account_security" in why and "billing_specialist" in why and why.startswith("[stand-in")
    # deliberately naive: a refund-policy QUESTION answered directly is flagged (a false alarm a human must overturn)
    assert sj.judge_routing("What is your refund policy?", "none")[0] is False
    assert sj.judge_routing("Where is my order 9912 and when should it arrive?", None)[0] is True


def test_evaluator_marks_stand_in():
    assert sj.EVALUATOR.startswith(sj.STAND_IN_PREFIX)


def test_review_cases():
    cases = traffic.review_cases(run_id="m08-abc")
    assert len(cases) == 5
    assert [c.case_id for c in cases if c.failures] == ["reset_password_wrong_handoff"]
    assert all(c.conversation_id.startswith("m08-abc-") for c in cases)
    planted = next(c for c in cases if c.failures)
    assert sj.judge_routing(planted.message, "billing_specialist")[0] is False
