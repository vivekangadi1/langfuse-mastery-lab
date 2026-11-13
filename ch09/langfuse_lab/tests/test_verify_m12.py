"""Module 12 verify checks against fakes shaped like the live API (server 4.77 / SDK 5.2) and a REAL webhook receiver
(its JSONL log is what verify reads): observations as camelCase dicts (latency in seconds, level enum), BOOLEAN scores
with source/timestamp, prompt versions with labels, Metrics API rows (`p95_latency` in ms), the alert job's tick
reports, and signed agent-alert / monitor-alert / prompt-version envelopes."""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from langfuse_lab import alert_spec, prompts, regression
from langfuse_lab.tests.test_verify_m11 import Enum, FakeMetrics, Row
from langfuse_lab.verify import FeatureUnavailable, NotYet, module_12_checks
from langfuse_lab.webhook_receiver import Receiver

TAG = "run:m12-test"
SECRET = "lf-whsec_diy"
NATIVE = {"monitor": "lf-whsec_monitor", "prompt": "lf-whsec_prompt"}
T0 = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
WIN = {w: (T0 + timedelta(seconds=70 * i), T0 + timedelta(seconds=70 * i + 60)) for i, w in enumerate("ABCD")}
CASES = ("login_reset", "locked_out", "double_charge", "shipping_status")
METRIC, RULE = "score.handoff_correct.API", "routing-quality-drop"
GOOD, BAD = 1, 2


def iso(d):
    return d.isoformat()


def build(*, dry=False, flake_b=False, c_version=BAD, c_misroutes=True, live=GOOD):
    """16 traces (root + one generation each), their handoff_correct scores, the prompt's two versions."""
    obs, scores = [], {}
    for w, (start, _) in WIN.items():
        version = alert_spec.agent_version(c_version if w == "C" else GOOD)
        for i, case in enumerate(CASES):
            tid = f"{w}-{case}"
            t = start + timedelta(seconds=5 + 12 * i)
            tags = [TAG, "alerts-lab", case, f"window:{w}"] + (["wrong_handoff"] if w == "C" else []) \
                + (["dry-run"] if dry else [])
            common = {"traceId": tid, "userId": f"cust_0{i}", "sessionId": f"s-{tid}", "version": version,
                      "environment": "dev", "tags": tags, "projectId": "p1", "traceName": "support-request"}
            obs.append({**common, "id": f"r-{tid}", "name": "support-request", "type": "AGENT",
                        "isRootObservation": True, "parentObservationId": None, "startTime": t, "latency": 5.0,
                        "level": Enum("DEFAULT"), "totalCost": None})
            obs.append({**common, "id": f"g-{tid}", "name": "AzureChatOpenAI", "type": "GENERATION",
                        "isRootObservation": False, "parentObservationId": f"r-{tid}",
                        "startTime": t + timedelta(seconds=1), "latency": 1.2, "level": Enum("DEFAULT"),
                        "totalCost": 0.0004})
            bad = (w == "C" and c_misroutes and case in ("login_reset", "locked_out")) \
                or (w == "B" and flake_b and case == "locked_out")
            scores[tid] = [{"id": f"hc-{tid}", "name": "handoff_correct", "value": 0.0 if bad else 1.0,
                            "source": Enum("API"), "dataType": "BOOLEAN", "timestamp": t + timedelta(seconds=4),
                            "comment": "expected account_security, got billing_specialist" if bad else "ok"}]
    versions = {GOOD: {"prompt": prompts.SYSTEM_PROMPT_TEMPLATE, "labels": ["production"]},
                BAD: {"prompt": prompts.WRONG_HANDOFF_TEMPLATE, "labels": ["staging", "latest"]}}
    versions[live]["labels"].append("live")
    return obs, scores, versions


def rates(scores):
    out = {}
    for w in WIN:
        vals = [s["value"] for t, rows in scores.items() if t.startswith(w + "-") for s in rows]
        out[w] = sum(vals) / len(vals)
    return out


class Fake:
    def __init__(self, obs, scores, versions):
        self.obs, self.scores, self.versions = obs, scores, versions
        self.api = NS(
            observations=NS(get_many=lambda fields=None, filter=None, limit=None, cursor=None: NS(
                data=[Row(o) for o in self.obs], meta=NS(cursor=None))),
            scores=NS(get_many=lambda trace_id, fields=None, limit=100: NS(
                data=[Row(s) for s in self.scores.get(trace_id, [])], meta=NS(cursor=None))),
            prompts=NS(list=lambda name, limit=100: NS(data=[NS(name=name, versions=sorted(self.versions))]),
                       get=lambda name, version: Row({"name": name, "version": version, "type": "text",
                                                      "config": prompts.PROMPT_CONFIG, **self.versions[version]})),
            metrics=FakeMetrics(obs, scores),
        )


# ---------------------------------------------------------------- the alert job's side
def snap(w, scores, *, requests=4, version=None):
    return {"from": iso(WIN[w][0]), "to": iso(WIN[w][1]),
            "metrics": {"requests": requests, METRIC: rates(scores)[w], "cost_usd.total": 0.0016,
                        "cost_usd.per_request": 0.0004, "latency_ms.request.p95": 5000.0},
            "by_version": {}, "version": version or alert_spec.agent_version(BAD if w == "C" else GOOD)}


def payload(status, *, b, c, since, seq, scores, fp="f" * 16, rule=RULE, metric=METRIC, values=None):
    bv, cv = values or (rates(scores)[b], rates(scores)[c])
    p = {"id": f"{fp}:{status}:{since}:{seq}", "type": "agent-alert", "apiVersion": "v1",
         "timestamp": iso(datetime.now(timezone.utc)), "status": status, "repeat": False, "rule": rule,
         "severity": "critical", "metric": metric, "direction": "down",
         "baseline": {"value": bv, "from": iso(WIN[b][0]), "to": iso(WIN[b][1])},
         "current": {"value": cv, "from": iso(WIN[c][0]), "to": iso(WIN[c][1])}, "delta": cv - bv,
         "version": alert_spec.agent_version(BAD if c == "C" else GOOD), "fingerprint": fp, "since": since,
         "scope": {"environment": "dev", "tags": [TAG]}, "links": {}}
    p["title"] = f"[{status.upper()} critical] {rule}: {metric} {bv} -> {cv}"
    return p


def post(rx, path, body: dict, secret):
    text = json.dumps(body, separators=(",", ":"), sort_keys=True)
    req = urllib.request.Request(rx.url(path), data=text.encode(), method="POST", headers={
        "content-type": "application/json", "x-langfuse-signature": regression.sign(text, secret, int(time.time()))})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def rx(tmp_path):
    r = Receiver(tmp_path / "deliveries.jsonl", port=0,
                 secrets={alert_spec.DIY_PATH: SECRET, alert_spec.MONITOR_PATH: NATIVE["monitor"],
                          alert_spec.PROMPT_PATH: NATIVE["prompt"]}).start()
    yield r
    r.stop()


def scenario(rx, scores, *, outage=True, deliver_firing=True, deliver_resolved=True, secret=SECRET,
             repeat_sends=False, clean_fires=False, firing_values=None, extra_firing=None, second_firing=False):
    """What a correct notebook run produces: four tick reports + the receiver's log."""
    since, fp = iso(T0 + timedelta(minutes=10)), "f" * 16
    firing = payload("firing", b="A", c="C", since=since, seq=1, scores=scores, values=firing_values)
    resolved = payload("resolved", b="A", c="D", since=since, seq=2, scores=scores)
    clean_sent = []
    if clean_fires:
        noisy = payload("firing", b="A", c="B", since=iso(T0), seq=1, scores=scores, fp="e" * 16)
        post(rx, alert_spec.DIY_PATH, noisy, secret)
        clean_sent = [{"payload": noisy, "delivery": {"ok": True, "attempts": 1}}]
    planted_sent, attempts = [], 0
    if deliver_firing:
        if outage:
            rx.fail_next(alert_spec.DIY_PATH, 1)
            attempts += post(rx, alert_spec.DIY_PATH, firing, secret) and 1
        attempts += post(rx, alert_spec.DIY_PATH, firing, secret) and 1
        planted_sent.append({"payload": firing, "delivery": {"ok": True, "status": 200, "attempts": attempts,
                                                             "id": firing["id"], "error": None}})
        if second_firing:
            again = dict(firing, id=f"{fp}:firing:{since}:9")
            post(rx, alert_spec.DIY_PATH, again, secret)
            planted_sent.append({"payload": again, "delivery": {"ok": True, "attempts": 1}})
    if extra_firing:
        post(rx, alert_spec.DIY_PATH, extra_firing, secret)
        planted_sent.append({"payload": extra_firing, "delivery": {"ok": True, "attempts": 1}})
    repeat_sent = [{"payload": dict(firing, id="x", repeat=True), "delivery": {"ok": True}}] if repeat_sends else []
    recovered_sent = []
    if deliver_resolved:
        post(rx, alert_spec.DIY_PATH, resolved, secret)
        recovered_sent.append({"payload": resolved, "delivery": {"ok": True, "attempts": 1}})
    firing_state = {"status": "firing", "since": since, "last_sent": since, "seq": 1, "alert": {}}
    alerts = [{"rule": RULE, "metric": METRIC}]
    ticks = {
        "clean": {"baseline": snap("A", scores), "current": snap("B", scores),
                  "alerts": [{"rule": "x"}] if clean_fires else [], "sent": clean_sent, "suppressed": [], "state": {}},
        "planted": {"baseline": snap("A", scores), "current": snap("C", scores), "alerts": alerts,
                    "sent": planted_sent, "suppressed": [], "state": {fp: firing_state}},
        "repeat": {"baseline": snap("A", scores), "current": snap("C", scores), "alerts": alerts,
                   "sent": repeat_sent, "suppressed": [] if repeat_sends else [fp], "state": {fp: firing_state}},
        "recovered": {"baseline": snap("A", scores), "current": snap("D", scores), "alerts": [],
                      "sent": recovered_sent, "suppressed": [],
                      "state": {fp: dict(firing_state, status="resolved", seq=2)}},
    }
    return ticks


@pytest.fixture(autouse=True)
def native_blocked(monkeypatch):
    monkeypatch.setattr(alert_spec, "native_webhooks_ready", lambda *a, **k: (False, "no whitelist"))


def run(fake, ticks, rx, *, triage="good", **kw):
    if triage == "good":
        triage = [{"trace_id": "C-login_reset"}, {"trace_id": "C-locked_out"}]
    checks = module_12_checks(TAG, fake, ticks=ticks, deliveries=rx.log_path, diy_secret=kw.pop("secret", SECRET),
                              triage=triage, native_secrets=kw.pop("native", NATIVE), **kw)
    out = {}
    for c in checks:
        try:
            out[c.name] = c.fn()
        except FeatureUnavailable as exc:
            out[c.name] = ("unavailable", str(exc))
        except (AssertionError, NotYet) as exc:
            out[c.name] = (False, str(exc))
    return out


def by_prefix(out, prefix):
    return next(v for k, v in out.items() if k.startswith(prefix))


def world(rx, **kw):
    build_kw = {k: kw.pop(k) for k in ("dry", "flake_b", "c_version", "c_misroutes", "live") if k in kw}
    obs, scores, versions = build(**build_kw)
    return Fake(obs, scores, versions), scenario(rx, scores, **kw)


# ---------------------------------------------------------------- the happy path
def test_all_pass_and_native_checks_are_unavailable(rx):
    fake, ticks = world(rx)
    out = run(fake, ticks, rx)
    for name, (ok, detail) in out.items():
        if name.startswith(("native", "Langfuse Cloud")):
            assert ok == "unavailable", (name, detail)
        else:
            assert ok is True, (name, detail)
    assert "v1" in by_prefix(out, "alert traffic")[1] and "v2" in by_prefix(out, "alert traffic")[1]
    assert alert_spec.WHITELIST_LINE in by_prefix(out, "native score")[1]
    assert "cloud-spend-alerts" in by_prefix(out, "Langfuse Cloud")[1]
    assert "1 planted 503" in by_prefix(out, "delivery survives")[1]


def test_partial_ingestion_is_not_yet(rx):
    fake, ticks = world(rx)
    fake.obs[:] = fake.obs[:-2]  # the last trace (root + generation) is not in yet
    fake.api.observations = NS(get_many=lambda **kw: NS(data=[Row(o) for o in fake.obs], meta=NS(cursor=None)))
    ok, detail = by_prefix(run(fake, ticks, rx), "alert traffic")
    assert ok is False and "15/16" in detail


# ---------------------------------------------------------------- 1 traffic
def test_dry_traffic_is_refused_unless_allowed(rx):
    fake, ticks = world(rx, dry=True)
    assert "dry-run" in by_prefix(run(fake, ticks, rx), "alert traffic")[1]
    assert by_prefix(run(fake, ticks, rx, allow_dry=True), "alert traffic")[0] is True


def test_live_label_must_be_rolled_back(rx):
    fake, ticks = world(rx, live=BAD)
    ok, detail = by_prefix(run(fake, ticks, rx), "alert traffic")
    assert ok is False and "roll back" in detail


def test_c_on_the_good_version_means_no_deploy(rx):
    fake, ticks = world(rx, c_version=GOOD)
    ok, detail = by_prefix(run(fake, ticks, rx), "alert traffic")
    assert ok is False and "deploy" in detail


# ---------------------------------------------------------------- 2 numbers
def test_tick_numbers_must_match_the_stored_data(rx):
    fake, ticks = world(rx)
    ticks["clean"]["current"]["metrics"]["requests"] = 5
    ticks["planted"]["current"]["metrics"]["latency_ms.request.p95"] = 9000.0
    ok, detail = by_prefix(run(fake, ticks, rx), "the alert job's numbers")
    assert ok is False and "requests = 5" in detail and "latency_ms.request.p95" in detail


def test_tick_version_must_match(rx):
    fake, ticks = world(rx)
    ticks["planted"]["current"]["version"] = "prompt-v1"
    ok, detail = by_prefix(run(fake, ticks, rx), "the alert job's numbers")
    assert ok is False and "version" in detail


# ---------------------------------------------------------------- 3 quiet
def test_a_clean_tick_that_pages_fails(rx):
    fake, ticks = world(rx, clean_fires=True)
    ok, detail = by_prefix(run(fake, ticks, rx), "clean windows stay quiet")
    assert ok is False and "tick clean" in detail and "clean window" in detail


def test_an_agent_flake_in_b_is_reported_as_unclean_data(rx):
    fake, ticks = world(rx, flake_b=True)
    ok, detail = by_prefix(run(fake, ticks, rx), "clean windows stay quiet")
    assert ok is False and "not clean" in detail and "RUN_TAG" in detail


# ---------------------------------------------------------------- 4 planted
def test_planted_needs_a_delivery(rx):
    fake, ticks = world(rx, deliver_firing=False)
    ok, detail = by_prefix(run(fake, ticks, rx), "planted")
    assert ok is False and "0 accepted firing" in detail


def test_planted_must_fire_once(rx):
    fake, ticks = world(rx, second_firing=True)
    ok, detail = by_prefix(run(fake, ticks, rx), "planted")
    assert ok is False and "2 accepted firing" in detail


def test_planted_signature_must_verify(rx):
    fake, ticks = world(rx)
    ok, detail = by_prefix(run(fake, ticks, rx, secret="another"), "planted")
    assert ok is False and "signature" in detail


def test_planted_numbers_must_be_the_stored_ones(rx):
    fake, ticks = world(rx, firing_values=(0.9, 0.5))
    ok, detail = by_prefix(run(fake, ticks, rx), "planted")
    assert ok is False and "baseline value 0.9" in detail


def test_no_drop_in_the_stored_data_is_explained(rx):
    fake, ticks = world(rx, c_misroutes=False)
    ok, detail = by_prefix(run(fake, ticks, rx), "planted")
    assert ok is False and "no routing drop" in detail


def test_another_rule_may_fire_only_when_the_stored_data_agrees(rx):
    obs, scores, versions = build()
    good = payload("firing", b="A", c="C", since=iso(T0), seq=1, scores=scores, fp="c" * 16,
                   rule="cost-per-request-up", metric="cost_usd.per_request", values=(0.0004, 0.0004))
    ticks = scenario(rx, scores, extra_firing=good)
    ok, detail = by_prefix(run(Fake(obs, scores, versions), ticks, rx), "planted")
    assert ok is True and "cost-per-request-up" in detail and "agrees" in detail


def test_another_rule_with_invented_numbers_fails(rx):
    obs, scores, versions = build()
    bad = payload("firing", b="A", c="C", since=iso(T0), seq=1, scores=scores, fp="c" * 16,
                  rule="request-latency-up", metric="latency_ms.request.p95", values=(5000.0, 15000.0))
    ticks = scenario(rx, scores, extra_firing=bad)
    ok, detail = by_prefix(run(Fake(obs, scores, versions), ticks, rx), "planted")
    assert ok is False and "request-latency-up" in detail


# ---------------------------------------------------------------- 5 retry, 6 cooldown, 7 resolve
def test_retry_needs_the_planted_outage(rx):
    fake, ticks = world(rx, outage=False)
    ok, detail = by_prefix(run(fake, ticks, rx), "delivery survives")
    assert ok is False and "no failed attempt" in detail


def test_repeat_tick_must_send_nothing(rx):
    fake, ticks = world(rx, repeat_sends=True)
    ok, detail = by_prefix(run(fake, ticks, rx), "dedup + cooldown")
    assert ok is False and "suppressed" in detail


def test_resolve_needs_one_resolved_delivery(rx):
    fake, ticks = world(rx, deliver_resolved=False)
    ok, detail = by_prefix(run(fake, ticks, rx), "resolve")
    assert ok is False and "0 accepted 'resolved'" in detail


# ---------------------------------------------------------------- 8 triage
@pytest.mark.parametrize("triage, want", [
    (["C-login_reset", "C-locked_out"], True),
    ([{"trace_id": "C-login_reset"}], "missing"),
    ([{"trace_id": "C-login_reset"}, {"trace_id": "C-locked_out"}, {"trace_id": "A-login_reset"}], "not failing"),
    (None, "TODO 4"),
])
def test_triage(rx, triage, want):
    fake, ticks = world(rx)
    ok, detail = by_prefix(run(fake, ticks, rx, triage=triage), "triage")
    assert ok is True if want is True else (ok is False and want in detail)


# ---------------------------------------------------------------- 9, 10 native (when the stack can deliver)
def monitor_alert(severity="ALERT"):
    c = WIN["C"]
    return {"id": "exec-1", "timestamp": iso(c[1]), "type": "monitor-alert", "apiVersion": "v1", "payload": {
        "monitorId": "m1", "projectId": "p1", "permalink": "http://localhost:3000/project/p1/alerts/m1",
        "message": {"title": "[ALERT] routing-pass-rate", "body": "..."}, "severity": severity,
        "timestamp": iso(c[1]), "fromTimestamp": iso(c[1] - timedelta(minutes=5)), "toTimestamp": iso(c[1]),
        "view": "scores-boolean", "filters": [{"column": "tags", "operator": "any of", "value": ["alerts-lab"],
                                               "type": "arrayOptions"}],
        "window": "5m", "monitorName": "routing-pass-rate", "metric": {"measure": "value", "aggregation": "avg"},
        "metricValue": 0.833, "thresholdOperator": "LT", "alertThreshold": 0.9, "warningThreshold": None,
        "alertedAt": iso(c[1])}}


def prompt_event(version, labels, i):
    return {"id": f"pe-{i}", "timestamp": iso(T0), "type": "prompt-version", "apiVersion": "v1", "action": "updated",
            "prompt": {"id": f"p{version}", "name": prompts.PROMPT_NAME, "version": version, "projectId": "p1",
                       "createdBy": "API", "labels": labels, "prompt": "...", "type": "text", "config": {},
                       "commitMessage": None, "tags": [], "createdAt": iso(T0), "updatedAt": iso(T0)}}


def test_native_checks_when_the_stack_can_deliver(rx, monkeypatch):
    monkeypatch.setattr(alert_spec, "native_webhooks_ready", lambda *a, **k: (True, "whitelisted"))
    fake, ticks = world(rx)
    out = run(fake, ticks, rx)
    assert by_prefix(out, "native score")[0] is False and "routing-pass-rate" in by_prefix(out, "native score")[1]
    assert by_prefix(out, "native prompt")[0] is False
    post(rx, alert_spec.MONITOR_PATH, monitor_alert(), NATIVE["monitor"])
    post(rx, alert_spec.PROMPT_PATH, prompt_event(BAD, ["staging", "latest", "live"], 1), NATIVE["prompt"])
    post(rx, alert_spec.PROMPT_PATH, prompt_event(GOOD, ["production", "live"], 2), NATIVE["prompt"])
    out = run(fake, ticks, rx)
    assert by_prefix(out, "native score")[0] is True, by_prefix(out, "native score")
    assert by_prefix(out, "native prompt")[0] is True, by_prefix(out, "native prompt")
    out = run(fake, ticks, rx, native={"monitor": "wrong", "prompt": "wrong"})
    assert "signature" in by_prefix(out, "native score")[1] and "signature" in by_prefix(out, "native prompt")[1]


def test_a_trace_stored_in_the_window_s_first_millisecond_counts(rx):
    """Live (dry rehearsal run:m12-78dfe1): the window opened at 13.300512, the first trace started 0.3 ms later and is
    stored truncated to 13.300. The Metrics API counts it; so must verify."""
    fake, ticks = world(rx)
    first = next(o for o in fake.obs if o["id"] == "r-C-login_reset")
    first["startTime"] = WIN["C"][0]
    for name in ("planted", "repeat"):
        ticks[name]["current"]["from"] = iso(WIN["C"][0] + timedelta(microseconds=512))
    out = run(fake, ticks, rx)
    for prefix in ("alert traffic", "the alert job's numbers", "planted", "triage"):
        assert by_prefix(out, prefix)[0] is True, (prefix, by_prefix(out, prefix))
