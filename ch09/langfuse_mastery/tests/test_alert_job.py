"""Module 12 TODO 3: one tick of the alert job (`alert_job.run_once`), with module 11's metrics faked (no server).

Needs module 11 TODO 1 (`analytics.compare` / `analytics.flag`): run_once reuses them."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from langfuse_lab.webhook_receiver import signature_ok
from langfuse_mastery import alert_job, weekly_pull

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
A = (T0, T0 + timedelta(minutes=1))
B = (T0 + timedelta(minutes=1), T0 + timedelta(minutes=2))
C = (T0 + timedelta(minutes=2), T0 + timedelta(minutes=3))
SCOPE = weekly_pull.Scope(tags=("run:m12-t",), environment="dev")
HEALTHY = {"requests": 4, "score.handoff_correct.API": 1.0, "error_rate": 0.0, "latency_ms.request.p95": 5200.0,
           "cost_usd.total": 0.0016, "latency_ms.tool.lookup_order.p95": 2.0}
BROKEN = dict(HEALTHY, **{"score.handoff_correct.API": 0.5})


@pytest.fixture
def metrics(monkeypatch):
    """window start -> (metrics, version) served by a fake weekly_pull.health_metrics."""
    served = {A[0]: (HEALTHY, "v1"), B[0]: (HEALTHY, "v1"), C[0]: (BROKEN, "v2")}

    def fake_health(langfuse, window, scope):
        assert scope == SCOPE
        m, v = served[window[0]]
        return {"metrics": dict(m), "by_version": {v: {"requests": m["requests"]}}, "top_users": []}

    monkeypatch.setattr(weekly_pull, "health_metrics", fake_health)
    return served


class Post:
    def __init__(self, *statuses):
        self.statuses, self.calls = list(statuses), []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers))
        return self.statuses.pop(0)


def tick(tmp_path, baseline, current, post, now, **kw):
    return alert_job.run_once(None, baseline, current, SCOPE, state_path=tmp_path / "state.json",
                              url="http://rx/m12/diy", secret="s3cret", now=now, post=post, **kw)


def test_snapshot_adds_cost_per_request_and_the_dominant_version(metrics):
    s = alert_job.snapshot(None, C, SCOPE)
    assert s["from"] == C[0].isoformat() and s["to"] == C[1].isoformat() and s["version"] == "v2"
    assert s["metrics"]["cost_usd.per_request"] == pytest.approx(0.0004)


def test_a_clean_tick_sends_nothing(metrics, tmp_path):
    post = Post()
    r = tick(tmp_path, A, B, post, T0 + timedelta(minutes=5))
    assert r["anomalies"] == [] and r["alerts"] == [] and r["sent"] == [] and r["suppressed"] == []
    assert post.calls == [] and json.loads((tmp_path / "state.json").read_text()) == {}
    assert r["baseline"]["metrics"]["requests"] == 4 and r["current"]["version"] == "v1"


def test_the_planted_tick_fires_once_and_records_it(metrics, tmp_path):
    post = Post(200)
    r = tick(tmp_path, A, C, post, T0 + timedelta(minutes=5))
    assert [a["rule"] for a in r["alerts"]] == ["routing-quality-drop"]
    (s,) = r["sent"]
    p = s["payload"]
    assert s["delivery"]["ok"] and p["status"] == "firing" and p["version"] == "v2"
    assert p["baseline"]["value"] == 1.0 and p["current"]["value"] == 0.5
    assert p["baseline"]["from"] == A[0].isoformat() and p["current"]["to"] == C[1].isoformat()
    url, body, headers = post.calls[0]
    assert json.loads(body) == p and signature_ok(body, headers["x-langfuse-signature"], "s3cret",
                                                   now=int(headers["x-langfuse-signature"][2:12]))
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved == r["state"] and saved[p["fingerprint"]]["status"] == "firing"


def test_a_failed_delivery_is_not_recorded_so_the_next_tick_retries(metrics, tmp_path):
    r = tick(tmp_path, A, C, Post(503, 503, 503, 503), T0 + timedelta(minutes=5), )
    assert r["sent"][0]["delivery"]["ok"] is False and r["state"] == {}
    assert json.loads((tmp_path / "state.json").read_text()) == {}
    again = tick(tmp_path, A, C, Post(200), T0 + timedelta(minutes=6))
    assert len(again["sent"]) == 1 and again["sent"][0]["delivery"]["ok"]


def test_repeat_is_suppressed_then_resolved(metrics, tmp_path):
    first = tick(tmp_path, A, C, Post(200), T0 + timedelta(minutes=5))
    fp = first["sent"][0]["payload"]["fingerprint"]
    post = Post()
    repeat = tick(tmp_path, A, C, post, T0 + timedelta(minutes=6))
    assert repeat["sent"] == [] and repeat["suppressed"] == [fp] and post.calls == []
    recovered = tick(tmp_path, A, B, Post(200), T0 + timedelta(minutes=7))
    (s,) = recovered["sent"]
    assert s["payload"]["status"] == "resolved" and s["payload"]["fingerprint"] == fp
    assert s["payload"]["current"]["value"] == 1.0 and s["delivery"]["ok"]
    assert json.loads((tmp_path / "state.json").read_text())[fp]["status"] == "resolved"


def test_cooldown_is_configurable(metrics, tmp_path):
    tick(tmp_path, A, C, Post(200), T0 + timedelta(minutes=5))
    r = tick(tmp_path, A, C, Post(200), T0 + timedelta(minutes=6), cooldown_s=30)
    assert len(r["sent"]) == 1 and r["sent"][0]["payload"]["repeat"] is True
