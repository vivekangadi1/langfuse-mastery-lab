"""Module 12 TODO 1: the alert job's pure logic (no server needed)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from langfuse_lab.webhook_receiver import signature_ok
from langfuse_mastery import alerting as al

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
SCOPE = {"environment": "dev", "tags": ["run:m12-t"]}
QUALITY = {"metric": "score.handoff_correct.API", "direction": "down", "prev": 1.0, "curr": 0.5, "delta": -0.5,
           "pct": -0.5, "rule": "score.handoff_correct.API"}
LATENCY = {"metric": "latency_ms.tool.lookup_order.p95", "direction": "up", "prev": 2.0, "curr": 3001.0,
           "delta": 2999.0, "pct": 1499.5, "rule": "latency_ms.tool.*.p95"}

# A real Langfuse monitor alert, generated with the server's own schemas (4.77, MonitorWebhookQueueEventSchema).
NATIVE = json.loads(
    '{"id":"exec-uuid","timestamp":"2026-11-12T09:00:30.000Z","type":"monitor-alert","apiVersion":"v1","payload":'
    '{"monitorId":"cmzzprobe0000monitor","projectId":"ch09-eval-project","permalink":"http://localhost:3000/project/'
    'ch09-eval-project/alerts/cmzzprobe0000monitor","message":{"title":"[ALERT] routing pass rate","body":"..."},'
    '"severity":"ALERT","timestamp":"2026-11-12T09:00:30.000Z","fromTimestamp":"2026-11-12T08:55:00.000Z",'
    '"toTimestamp":"2026-11-12T09:00:00.000Z","view":"scores-boolean","filters":[{"column":"name","operator":"=",'
    '"value":"handoff_correct","type":"string"},{"column":"tags","operator":"any of","value":["alerts-lab"],'
    '"type":"arrayOptions"}],"window":"5m","monitorName":"routing pass rate","metric":{"measure":"value",'
    '"aggregation":"avg"},"metricValue":0.8333,"thresholdOperator":"LT","alertThreshold":0.9,"warningThreshold":null,'
    '"alertedAt":"2026-11-12T09:00:30.000Z"}}')
PROMPT_EVENT = {"id": "x", "type": "prompt-version", "apiVersion": "v1", "action": "updated", "prompt": {"name": "p"}}


def _alert(**kw):
    return {**al.to_alerts([QUALITY])[0], **kw}


# ---------------------------------------------------------------- to_alerts
def test_to_alerts_maps_patterns_to_rule_names_and_severities():
    out = al.to_alerts([QUALITY, LATENCY])
    assert out == [
        {"rule": "routing-quality-drop", "severity": "critical", "metric": "score.handoff_correct.API",
         "direction": "down", "baseline": 1.0, "current": 0.5, "delta": -0.5, "pct": -0.5},
        {"rule": "tool-latency-up", "severity": "warning", "metric": "latency_ms.tool.lookup_order.p95",
         "direction": "up", "baseline": 2.0, "current": 3001.0, "delta": 2999.0, "pct": 1499.5}]


def test_to_alerts_drops_anomalies_of_unknown_rules_and_uses_the_given_rules():
    rogue = dict(QUALITY, rule="feedback_share.*", metric="feedback_share.thumbs_down")
    assert al.to_alerts([rogue]) == []
    mine = ({"name": "fb", "pattern": "feedback_share.*", "direction": "up", "severity": "info"},)
    assert al.to_alerts([rogue, QUALITY], mine) == [
        {"rule": "fb", "severity": "info", "metric": "feedback_share.thumbs_down", "direction": "down",
         "baseline": 1.0, "current": 0.5, "delta": -0.5, "pct": -0.5}]


def test_given_rules_are_shaped_for_analytics_flag():
    for r in al.ALERT_RULES:
        assert {"name", "pattern", "direction", "min_abs", "min_pct", "severity"} <= set(r)
    assert len({r["pattern"] for r in al.ALERT_RULES}) == len(al.ALERT_RULES)


# ---------------------------------------------------------------- fingerprint
def test_fingerprint_is_stable_and_ignores_values():
    a, b = _alert(), _alert(baseline=0.9, current=0.1, delta=-0.8)
    fp = al.fingerprint(a, SCOPE)
    assert len(fp) == 16 and all(ch in "0123456789abcdef" for ch in fp)
    assert al.fingerprint(b, SCOPE) == fp
    import hashlib
    assert fp == hashlib.sha256(b"routing-quality-drop|score.handoff_correct.API|dev|run:m12-t").hexdigest()[:16]


def test_fingerprint_depends_on_rule_metric_environment_and_tags():
    fp = al.fingerprint(_alert(), SCOPE)
    assert al.fingerprint(_alert(rule="other"), SCOPE) != fp
    assert al.fingerprint(_alert(metric="error_rate"), SCOPE) != fp
    assert al.fingerprint(_alert(), {"environment": "production", "tags": ["run:m12-t"]}) != fp
    assert al.fingerprint(_alert(), {"environment": "dev", "tags": ["b", "a"]}) == \
        al.fingerprint(_alert(), {"environment": "dev", "tags": ["a", "b"]})
    assert al.fingerprint(_alert(), {"environment": None, "tags": None}) == \
        al.fingerprint(_alert(), {"environment": "", "tags": []})


# ---------------------------------------------------------------- decide
def test_decide_fires_a_new_alert_once():
    a = _alert()
    d = al.decide({}, [a], scope=SCOPE, now=T0)
    fp = al.fingerprint(a, SCOPE)
    assert d["send"] == [{"status": "firing", "fingerprint": fp, "alert": a, "since": T0.isoformat(), "seq": 1,
                          "repeat": False}]
    assert d["suppressed"] == []
    assert d["state"] == {fp: {"status": "firing", "since": T0.isoformat(), "last_sent": T0.isoformat(), "seq": 1,
                               "alert": a}}


def test_decide_suppresses_within_the_cooldown_and_keeps_the_latest_numbers():
    first = al.decide({}, [_alert()], scope=SCOPE, now=T0)
    worse = _alert(current=0.25)
    d = al.decide(first["state"], [worse], scope=SCOPE, now=T0 + timedelta(minutes=5), cooldown_s=3600)
    fp = al.fingerprint(worse, SCOPE)
    assert d["send"] == [] and d["suppressed"] == [fp]
    assert d["state"][fp]["last_sent"] == T0.isoformat() and d["state"][fp]["seq"] == 1
    assert d["state"][fp]["alert"]["current"] == 0.25


def test_decide_renotifies_after_the_cooldown():
    first = al.decide({}, [_alert()], scope=SCOPE, now=T0)
    later = T0 + timedelta(hours=2)
    d = al.decide(first["state"], [_alert()], scope=SCOPE, now=later, cooldown_s=3600)
    (n,) = d["send"]
    assert n["status"] == "firing" and n["repeat"] is True and n["seq"] == 2 and n["since"] == T0.isoformat()
    assert d["state"][n["fingerprint"]]["last_sent"] == later.isoformat()


def test_decide_resolves_when_the_alert_is_gone():
    a = _alert()
    first = al.decide({}, [a], scope=SCOPE, now=T0)
    later = T0 + timedelta(minutes=10)
    d = al.decide(first["state"], [], scope=SCOPE, now=later)
    fp = al.fingerprint(a, SCOPE)
    assert d["send"] == [{"status": "resolved", "fingerprint": fp, "alert": a, "since": T0.isoformat(), "seq": 2,
                          "repeat": False}]
    assert d["state"][fp]["status"] == "resolved" and d["state"][fp]["resolved_at"] == later.isoformat()
    # nothing more to say on the next quiet tick
    assert al.decide(d["state"], [], scope=SCOPE, now=later + timedelta(minutes=1))["send"] == []


def test_decide_a_new_incident_after_a_resolve_fires_again():
    a = _alert()
    s1 = al.decide({}, [a], scope=SCOPE, now=T0)["state"]
    s2 = al.decide(s1, [], scope=SCOPE, now=T0 + timedelta(minutes=10))["state"]
    again = T0 + timedelta(minutes=12)
    d = al.decide(s2, [a], scope=SCOPE, now=again)
    (n,) = d["send"]
    assert n["status"] == "firing" and n["since"] == again.isoformat() and n["seq"] == 1 and not n["repeat"]


def test_decide_does_not_mutate_its_input_and_orders_firing_before_resolved():
    q, lat = al.to_alerts([QUALITY, LATENCY])
    state = al.decide({}, [lat], scope=SCOPE, now=T0)["state"]
    frozen = json.dumps(state, sort_keys=True)
    d = al.decide(state, [q], scope=SCOPE, now=T0 + timedelta(minutes=1))
    assert json.dumps(state, sort_keys=True) == frozen
    assert [n["status"] for n in d["send"]] == ["firing", "resolved"]
    assert [n["alert"]["rule"] for n in d["send"]] == ["routing-quality-drop", "tool-latency-up"]


# ---------------------------------------------------------------- alert_scope
def _snap(frm, to, value, version="v2"):
    return {"from": frm.isoformat(), "to": to.isoformat(),
            "metrics": {"score.handoff_correct.API": value, "requests": 4}, "version": version}


def _payload(status="firing", now=T0):
    n = al.decide({}, [_alert()], scope=SCOPE, now=now)["send"][0]
    n = dict(n, status=status)
    return al.build_payload(n, scope=SCOPE, baseline=_snap(T0 - timedelta(minutes=4), T0 - timedelta(minutes=3), 1.0),
                            current=_snap(T0 - timedelta(minutes=2), T0 - timedelta(minutes=1), 0.5), now=now)


def test_alert_scope_of_our_payload():
    sc = al.alert_scope(_payload())
    assert sc == {"from": T0 - timedelta(minutes=2), "to": T0 - timedelta(minutes=1), "tags": ["run:m12-t"],
                  "environment": "dev"}


def test_alert_scope_of_a_real_langfuse_monitor_alert():
    sc = al.alert_scope(NATIVE)
    assert sc == {"from": datetime(2026, 11, 12, 8, 55, tzinfo=UTC), "to": datetime(2026, 11, 12, 9, 0, tzinfo=UTC),
                  "tags": ["alerts-lab"], "environment": None}
    with_env = json.loads(json.dumps(NATIVE))
    with_env["payload"]["filters"].append({"column": "environment", "operator": "any of", "value": ["dev"],
                                           "type": "stringOptions"})
    assert al.alert_scope(with_env)["environment"] == "dev"


def test_alert_scope_of_other_events_is_none():
    assert al.alert_scope(PROMPT_EVENT) is None and al.alert_scope({}) is None


# ---------------------------------------------------------------- build_payload
def test_build_payload_fields():
    p = _payload()
    fp = al.fingerprint(_alert(), SCOPE)
    assert p["id"] == f"{fp}:firing:{T0.isoformat()}:1"
    assert p["type"] == "agent-alert" and p["apiVersion"] == "v1" and p["timestamp"] == T0.isoformat()
    assert (p["status"], p["repeat"], p["rule"], p["severity"], p["metric"], p["direction"]) == \
        ("firing", False, "routing-quality-drop", "critical", "score.handoff_correct.API", "down")
    assert p["baseline"] == {"value": 1.0, "from": (T0 - timedelta(minutes=4)).isoformat(),
                             "to": (T0 - timedelta(minutes=3)).isoformat()}
    assert p["current"]["value"] == 0.5 and p["delta"] == -0.5 and p["version"] == "v2"
    assert p["fingerprint"] == fp and p["since"] == T0.isoformat()
    assert p["scope"] == SCOPE and p["links"] == {}
    assert p["title"] == "[FIRING critical] routing-quality-drop: score.handoff_correct.API 1 -> 0.5 (version v2)"
    json.dumps(p)  # JSON-serializable


def test_build_payload_reads_values_from_the_snapshots_and_keeps_links():
    n = {"status": "resolved", "fingerprint": "f" * 16, "alert": _alert(), "since": T0.isoformat(), "seq": 2,
         "repeat": False}
    p = al.build_payload(n, scope=SCOPE, baseline=_snap(T0, T0, 1.0, "v1"), current=_snap(T0, T0, 1.0, "v1"),
                         now=T0 + timedelta(minutes=9), links={"traces": "http://x"})
    assert p["current"]["value"] == 1.0 and p["delta"] == 0.0 and p["version"] == "v1"  # today's healthy value
    assert p["id"] == f"{'f' * 16}:resolved:{T0.isoformat()}:2" and p["links"] == {"traces": "http://x"}
    missing = al.build_payload(n, scope=SCOPE, baseline={"from": T0, "to": T0, "metrics": {}, "version": None},
                               current={"from": T0, "to": T0, "metrics": {}, "version": None}, now=T0)
    assert missing["delta"] is None and missing["baseline"]["from"] == T0.isoformat()


# ---------------------------------------------------------------- deliver
class FakePost:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, dict(headers), timeout))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _deliver(post, **kw):
    sleeps = []
    clock = iter(range(1_800_000_000, 1_800_000_100))
    res = al.deliver("http://rx/m12/diy", _payload(), "s3cret", post=post, sleep=sleeps.append,
                     clock=lambda: next(clock), **kw)
    return res, sleeps


def test_deliver_first_try():
    post = FakePost(200)
    res, sleeps = _deliver(post)
    assert res == {"ok": True, "status": 200, "attempts": 1, "id": _payload()["id"], "error": None} and sleeps == []
    url, body, headers, timeout = post.calls[0]
    assert url == "http://rx/m12/diy" and timeout == 5.0
    assert body == json.dumps(_payload(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert headers["content-type"] == "application/json" and headers["user-agent"] == al.USER_AGENT
    assert signature_ok(body, headers[al.SIGNATURE_HEADER], "s3cret", now=1_800_000_000)
    assert headers[al.SIGNATURE_HEADER].startswith("t=1800000000,v1=")


def test_deliver_retries_5xx_and_connection_errors_with_backoff_same_body_fresh_signature():
    post = FakePost(503, ConnectionRefusedError("down"), 200)
    res, sleeps = _deliver(post)
    assert res["ok"] and res["attempts"] == 3 and res["status"] == 200 and sleeps == [0.5, 1.0]
    bodies = {c[1] for c in post.calls}
    assert len(bodies) == 1
    sigs = [c[2][al.SIGNATURE_HEADER] for c in post.calls]
    assert len(set(sigs)) == 3  # a fresh timestamp per attempt


def test_deliver_retries_429_but_not_other_4xx():
    res, sleeps = _deliver(FakePost(429, 202))
    assert res["ok"] and res["attempts"] == 2
    post = FakePost(401, 200)
    res, sleeps = _deliver(post)
    assert res == {"ok": False, "status": 401, "attempts": 1, "id": _payload()["id"], "error": None}
    assert sleeps == [] and len(post.calls) == 1


def test_deliver_gives_up_after_the_attempts():
    res, sleeps = _deliver(FakePost(500, 502, TimeoutError("slow"), 503))
    assert res["ok"] is False and res["attempts"] == 4 and res["status"] == 503 and sleeps == [0.5, 1.0, 2.0]
    res, _ = _deliver(FakePost(OSError("a"), OSError("b")), attempts=2, backoff_s=1)
    assert res["status"] is None and res["error"] == "OSError: b" and res["attempts"] == 2
