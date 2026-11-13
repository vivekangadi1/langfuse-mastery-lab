import hashlib
import hmac
import json

from langfuse_lab import regression


def test_source_cases_plant_one_login_failure():
    cases = regression.source_cases(run_id="r1")
    assert len(cases) == 4
    planted = [c for c in cases if c.failures]
    assert [c.case_id for c in planted] == ["reset_password_wrong_handoff"]
    assert planted[0].expected_handoff == "account_security"
    assert all(c.conversation_id.startswith("r1-") for c in cases)


def test_curated_cases_and_fix():
    ids = [c.case_id for c in regression.CURATED]
    assert len(ids) == len(set(ids)) == 3
    case_id, fixed = regression.LABEL_FIX
    assert regression.curated(case_id).expected_handoff == "none" != fixed
    assert regression.curated(regression.RETIRED).message.startswith("Ignore previous instructions")
    assert regression.curated("locked_out_cust_03").expected_handoff == "account_security"


def test_item_ids_are_deterministic_and_scoped():
    a = regression.dataset_item_id("ds", "Hello there")
    assert a == regression.dataset_item_id("ds", "  Hello there ")
    assert a != regression.dataset_item_id("other", "Hello there")
    assert a != regression.dataset_item_id("ds", "Hello")
    assert a.startswith("di-") and len(a) == 27


def test_sign_matches_langfuse_scheme():
    body = regression.trigger_body("p", "d", "ds", '{"label":"staging"}')
    assert json.loads(body) == {"projectId": "p", "datasetId": "d", "datasetName": "ds", "payload": '{"label":"staging"}'}
    assert " " not in body  # compact, like JSON.stringify
    sig = regression.sign(body, "lf-whsec_x", 1700000000)
    want = hmac.new(b"lf-whsec_x", f"1700000000.{body}".encode(), hashlib.sha256).hexdigest()
    assert sig == f"t=1700000000,v1={want}"
    assert regression.sign(body.encode(), "lf-whsec_x", 1700000000) == sig
