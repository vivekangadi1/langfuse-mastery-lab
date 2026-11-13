from langfuse_lab.fixtures import load_fixtures


def test_cases_ported():
    cases = load_fixtures().cases
    assert len(cases) == 8
    assert {"case_id", "customer_request", "expected_handoff", "reference_answer"} <= cases[0].keys()


def test_sessions_are_multi_turn():
    sessions = load_fixtures().sessions
    assert len(sessions) == 6
    assert all(len(s["turns"]) >= 2 and s["customer_id"] in load_fixtures().customers for s in sessions)
