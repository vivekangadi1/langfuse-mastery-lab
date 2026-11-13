"""Module 12 lab support: alert traffic, the oracle following the prompt's login rule, the webhook receiver, the native
alert spec."""
import json
import time
import urllib.error
import urllib.request

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from langfuse_lab import alert_spec, oracle_llm, prompts, regression, traffic
from langfuse_lab.webhook_receiver import Receiver, parse_signature, read_log, signature_ok


# ---------------------------------------------------------------- traffic
def test_alert_cases_four_windows_of_the_same_four_requests():
    cases = traffic.alert_cases(run_id="m12-t")
    assert len(cases) == 16 and len({c.conversation_id for c in cases}) == 16
    by_window = {}
    for c in cases:
        by_window.setdefault(c.window, []).append(c)
    assert list(by_window) == ["A", "B", "C", "D"]
    assert all([c.message for c in ws] == [c.message for c in by_window["A"]] for ws in by_window.values())
    assert {c.prompt for c in by_window["C"]} == {"bad"} and all(c.failures == ("wrong_handoff",) for c in by_window["C"])
    assert all(c.prompt == "good" and not c.failures for w in "ABD" for c in by_window[w])
    assert by_window["C"][0].tags == ("login_reset", "window:C", "wrong_handoff")
    assert sum(c.expected_handoff == "account_security" for c in by_window["A"]) == len(traffic.ALERT_LOGIN_CASES)


# ---------------------------------------------------------------- oracle
def test_oracle_reads_the_login_rule_from_the_system_prompt():
    assert oracle_llm.login_team(prompts.SYSTEM_PROMPT) == "account_security"
    assert oracle_llm.login_team(prompts.WRONG_HANDOFF_PROMPT) == "billing_specialist"
    assert oracle_llm.login_team(prompts.render(prompts.CLEAN_CANDIDATE_TEMPLATE, prompts.PROMPT_VARIABLES)) \
        == "account_security"
    assert oracle_llm.login_team(None) == "account_security"


@pytest.mark.parametrize("system, team", [(prompts.SYSTEM_PROMPT, "account_security"),
                                          (prompts.WRONG_HANDOFF_PROMPT, "billing_specialist")])
def test_oracle_routes_login_like_the_prompt_says(system, team):
    msgs = [SystemMessage(content=system), HumanMessage(content="Please help, I'm locked out of account cust_03.")]
    first = oracle_llm.decide(msgs)
    assert first.tool_calls[0]["name"] == "reset_password"
    plan = oracle_llm.plan_calls("I'm locked out of account cust_03.", login_to=oracle_llm.login_team(system))
    assert plan[-1] == ("create_handoff", {"target": team, "reason": "account access"})


def test_oracle_without_a_system_prompt_is_unchanged():
    plan = oracle_llm.plan_calls("I can't log in to my account cust_02.")
    assert plan[-1][1]["target"] == "account_security"


# ---------------------------------------------------------------- receiver
@pytest.fixture
def rx(tmp_path):
    r = Receiver(tmp_path / "deliveries.jsonl", port=0, secrets={"/diy": "s3cret"}).start()
    yield r
    r.stop()


def _post(url, body: str, headers=None):
    req = urllib.request.Request(url, data=body.encode(), method="POST",
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_signature_helpers_match_the_module_09_scheme():
    body, now = '{"id":"x"}', 1_800_000_000
    header = regression.sign(body, "k", now)
    assert parse_signature(header) == (now, header.split("v1=")[1])
    assert signature_ok(body, header, "k", now=now + 10)
    assert not signature_ok(body, header, "other", now=now)
    assert not signature_ok(body + " ", header, "k", now=now)
    assert not signature_ok(body, header, "k", now=now + 301)  # replayed too late
    assert parse_signature("garbage") is None and not signature_ok(body, None, "k")


def test_receiver_checks_signatures_dedupes_and_plants_outages(rx):
    body = json.dumps({"id": "d1", "type": "agent-alert"})
    good = regression.sign(body, "s3cret", int(time.time()))
    rx.fail_next("/diy", 1)
    assert _post(rx.url("/diy"), body, {"x-langfuse-signature": good}) == 503          # planted outage
    assert _post(rx.url("/diy"), body, {"x-langfuse-signature": good}) == 200          # retry accepted
    assert _post(rx.url("/diy"), body, {"x-langfuse-signature": good}) == 200          # re-delivery acked
    assert _post(rx.url("/diy"), body, {"x-langfuse-signature": "t=1,v1=00"}) == 401   # bad signature
    assert _post(rx.url("/open"), json.dumps({"id": "n1"})) == 200                     # no secret on this path
    assert _post(rx.url("/open"), "not json") == 400
    rows = rx.deliveries("/diy")
    assert [r["status"] for r in rows] == [503, 200, 200, 401]
    assert rows[0]["planted_outage"] and rows[2]["duplicate"] and not rows[1]["duplicate"]
    assert [r["signature_ok"] for r in rows] == [None, True, True, False]
    assert rows[1]["id"] == "d1" and rows[1]["type"] == "agent-alert" and rows[1]["body"] == body
    assert rows[1]["headers"]["x-langfuse-signature"] == good
    assert read_log(rx.log_path, "/open")[0]["signature_ok"] is None
    assert len(read_log(rx.log_path)) == 6


def test_receiver_urls(rx):
    assert rx.url("/diy").startswith("http://127.0.0.1:") and not rx.native_reachable
    assert rx.docker_url("/m12/monitor") is None


def test_receiver_falls_back_when_the_port_is_taken(tmp_path, rx):
    other = Receiver(tmp_path / "b.jsonl", port=rx.port).start()
    try:
        assert other.port != rx.port and other.bind_error
    finally:
        other.stop()


# ---------------------------------------------------------------- native spec
def test_native_spec_names_and_render():
    n = alert_spec.names("zz_")
    assert n["tag"] == "zz_alerts-lab" and n["monitor.routing"] == "zz_routing-pass-rate"
    assert n["automation.prompt"] == "zz_prompt deploys → local webhook"
    text = alert_spec.render("zz_", prompt_name="zz_support-agent-system")
    for want in ("zz_routing-pass-rate", "avg(scores-boolean.value)", "Tags any of zz_alerts-lab", "LT 0.9",
                 "SHOW_NO_DATA", "http://host.docker.internal/m12/monitor", "M12_PROMPT_WEBHOOK_SECRET",
                 "Name = zz_support-agent-system AND Labels any of live", "spend-5m"):
        assert want in text, want
    assert [m["key"] for m in alert_spec.MONITORS if m["planted"]] == ["routing"]


def test_native_webhooks_ready_reads_the_compose_file(tmp_path):
    f = tmp_path / "compose.yml"
    f.write_text("services:\n  w:\n    environment:\n      REDIS_PORT: 6379\n", encoding="utf-8")
    assert alert_spec.native_webhooks_ready(f)[0] is False
    f.write_text("    environment:\n      # LANGFUSE_WEBHOOK_WHITELISTED_HOST: host.docker.internal\n", encoding="utf-8")
    assert alert_spec.native_webhooks_ready(f)[0] is False
    f.write_text('      LANGFUSE_WEBHOOK_WHITELISTED_HOST: "example.com, host.docker.internal"\n', encoding="utf-8")
    assert alert_spec.native_webhooks_ready(f)[0] is True
    assert alert_spec.native_webhooks_ready(tmp_path / "missing.yml")[0] is False
    assert alert_spec.WHITELIST_LINE in alert_spec.NATIVE_HINT and alert_spec.COMPOSE_UP in alert_spec.NATIVE_HINT


def test_the_real_compose_file_is_parsed():
    ready, detail = alert_spec.native_webhooks_ready()
    assert isinstance(ready, bool) and detail


def test_agent_version_round_trip():
    assert alert_spec.agent_version(3) == "prompt-v3" and alert_spec.prompt_version_of("prompt-v3") == 3
    assert alert_spec.prompt_version_of("v3") is None and alert_spec.prompt_version_of(None) is None
    assert alert_spec.prompt_version_of("prompt-vx") is None
