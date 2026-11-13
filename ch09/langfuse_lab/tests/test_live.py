import os

import pytest

from langfuse_lab.agent import SupportAgent
from langfuse_lab.failures import FailureConfig

pytestmark = pytest.mark.skipif(os.getenv("LAB_LIVE") != "1", reason="set LAB_LIVE=1 to call Azure")


def test_agent_answers_double_charge_with_tools():
    run = SupportAgent().invoke("I was charged twice for order 8129. Please fix it.")
    print(run.summary)
    assert "lookup_order" in run.summary["steps_completed"]
    assert run.summary["handoff_target"] == "billing_specialist"
    assert run.summary["final_answer"]


LOGIN = "I can't log in to my account cust_02. Please reset my password."


def test_login_goes_to_account_security():
    run = SupportAgent().invoke(LOGIN)
    print(run.summary)
    assert run.summary["handoff_target"] == "account_security"


def test_planted_wrong_handoff_fires():
    agent = SupportAgent(failures=FailureConfig.from_names(["wrong_handoff"]))
    run = agent.invoke(LOGIN)
    print(run.summary)
    assert run.summary["handoff_target"] == "billing_specialist"
