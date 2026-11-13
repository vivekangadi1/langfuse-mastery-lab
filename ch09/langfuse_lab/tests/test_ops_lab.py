"""Module 13 lab support: the OTLP tap, edition facts, the worker runner, the ops traffic (no Langfuse needed)."""
import gzip
import json
import textwrap
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from langfuse_lab import ops_lab, traffic
from langfuse_lab.otlp_tap import OTLP_PATH, Tap, decode_export, read_exports


def otlp_body(spans, *, gzip_it=True) -> bytes:
    """spans: [(trace_id_hex, name)] -> an OTLP/HTTP protobuf body like the SDK's exporter sends."""
    req = ExportTraceServiceRequest(resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[
        Span(trace_id=bytes.fromhex(t), span_id=bytes.fromhex(f"{i + 1:016x}"), name=n) for i, (t, n) in enumerate(spans)
    ])])])
    data = req.SerializeToString()
    return gzip.compress(data) if gzip_it else data


T1, T2 = "a" * 32, "b" * 32


@pytest.fixture
def upstream():
    """A fake Langfuse: records requests, answers 200 (OTLP) or echoes the path."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self):
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n) if n else b""
            seen.append((self.command, self.path, dict(self.headers), body))
            data = json.dumps({"path": self.path}).encode()
            self.send_response(207 if self.path == "/api/public/health" else 200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _do

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()


def post(url, path, body, headers=None):
    req = urllib.request.Request(url + path, data=body, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ---------------------------------------------------------------- tap
def test_decode_export_gzip_and_plain():
    spans = [(T1, "support-request"), (T1, "guardrail"), (T2, "support-request")]
    for gz in (True, False):
        got = decode_export(otlp_body(spans, gzip_it=gz), "gzip" if gz else None)
        assert got == {"spans": 3, "trace_ids": [T1, T2], "names": {"support-request": 2, "guardrail": 1}}


def test_tap_forwards_and_logs_every_export(tmp_path, upstream):
    url, seen = upstream
    tap = Tap(tmp_path / "exports.jsonl", upstream=url).start()
    try:
        body = otlp_body([(T1, "a"), (T1, "b"), (T2, "c")])
        status, _ = post(tap.url, OTLP_PATH, body, {"content-encoding": "gzip", "authorization": "Basic x"})
        assert status == 200
        status, data = post(tap.url, "/api/public/health", b"{}")  # not an export: forwarded, not logged
        assert status == 207 and json.loads(data) == {"path": "/api/public/health"}
        with urllib.request.urlopen(tap.url + "/api/public/projects?x=1", timeout=5) as resp:  # GET passes through
            assert json.loads(resp.read()) == {"path": "/api/public/projects?x=1"}
    finally:
        tap.stop()
    assert [s[1] for s in seen] == [OTLP_PATH, "/api/public/health", "/api/public/projects?x=1"]
    assert seen[0][3] == body and seen[0][2].get("Authorization") == "Basic x"  # unchanged bytes and headers
    rows = read_exports(tmp_path / "exports.jsonl")
    assert len(rows) == 1
    r = rows[0]
    assert (r["status"], r["spans"], r["trace_ids"], r["outage"]) == (200, 3, [T1, T2], False)
    assert r["batch"] and r["bytes"] == len(body)
    assert tap.exports([T2]) == rows and tap.exports(["c" * 32]) == []


def test_tap_planted_outage_answers_503_without_forwarding(tmp_path, upstream):
    url, seen = upstream
    tap = Tap(tmp_path / "exports.jsonl", upstream=url).start()
    try:
        body = otlp_body([(T1, "a")])
        window = tap.outage(0.5)
        assert window[1] - window[0] == pytest.approx(0.5) and tap.is_down()
        assert post(tap.url, OTLP_PATH, body, {"content-encoding": "gzip"})[0] == 503
        assert post(tap.url, OTLP_PATH, body, {"content-encoding": "gzip"})[0] == 503  # a retry: same bytes
        time.sleep(0.6)
        assert not tap.is_down()
        assert post(tap.url, OTLP_PATH, body, {"content-encoding": "gzip"})[0] == 200
        tap.outage(1.0, delay=5.0)
        assert not tap.is_down() and tap.is_down(time.time() + 5.5)
    finally:
        tap.stop()
    rows = tap.exports()
    assert [(r["status"], r["outage"]) for r in rows] == [(503, True), (503, True), (200, False)]
    assert len({r["batch"] for r in rows}) == 1  # the same batch, retried
    assert len(seen) == 1  # only the accepted attempt reached the server


def test_tap_reports_an_unreachable_upstream_as_502(tmp_path):
    tap = Tap(tmp_path / "exports.jsonl", upstream="http://127.0.0.1:9", timeout=2).start()
    try:
        assert post(tap.url, OTLP_PATH, otlp_body([(T1, "a")]), {"content-encoding": "gzip"})[0] == 502
    finally:
        tap.stop()
    assert tap.exports()[0]["status"] == 502


# ---------------------------------------------------------------- edition facts
def test_enterprise_only_features_are_not_in_oss():
    assert ops_lab.OSS_ENTITLEMENTS < ops_lab.ENTERPRISE_ENTITLEMENTS
    for feature, (entitlement, means, workaround) in ops_lab.ENTERPRISE_ONLY.items():
        for e in entitlement.split(" / "):
            assert e in ops_lab.ENTERPRISE_ENTITLEMENTS and e not in ops_lab.OSS_ENTITLEMENTS, feature
        assert means and workaround
    assert "trace-deletion" in ops_lab.OSS_ENTITLEMENTS  # the DIY retention job and erasure rely on it


def test_stack_edition_reads_the_license_key(tmp_path):
    oss = tmp_path / "oss.yml"
    oss.write_text("services:\n  web:\n    environment:\n      SALT: x\n      # LANGFUSE_EE_LICENSE_KEY: later\n")
    assert ops_lab.stack_edition(oss)[0] == "oss"
    ee = tmp_path / "ee.yml"
    ee.write_text("services:\n  web:\n    environment:\n      LANGFUSE_EE_LICENSE_KEY: lf_ee_123\n")
    assert ops_lab.stack_edition(ee)[0] == "enterprise"
    empty = tmp_path / "empty.yml"
    empty.write_text("environment:\n  LANGFUSE_EE_LICENSE_KEY: \"\"\n")
    assert ops_lab.stack_edition(empty)[0] == "oss"
    assert ops_lab.stack_edition(tmp_path / "missing.yml")[0] == "oss"
    assert ops_lab.stack_edition()[0] == "oss"  # this repo's stack


def test_sdk_scenarios_are_unique_and_include_the_planted_failures():
    names = [s["name"] for s in ops_lab.SDK_SCENARIOS]
    assert len(names) == len(set(names))
    assert {s["name"] for s in ops_lab.SDK_SCENARIOS if s.get("planted")} == {"outage", "killed-job"}
    out, hard = ops_lab.scenario("outage"), ops_lab.scenario("outage-hardened")
    assert out["outage"] == hard["outage"] and hard["client"]["timeout"] > out["outage"]["seconds"] * 2
    assert "timeout" not in out["client"]  # the default retry budget (5 s) is what loses data
    assert ops_lab.scenario("killed-job")["exit"] == "killed" and ops_lab.scenario("job")["exit"] == "finish"


# ---------------------------------------------------------------- worker runner
WORKER = """
import json, sys
p = json.loads(sys.stdin.readline())
if p.get("hold"):
    print(json.dumps({"held": True, "trace_ids": ["x"]}), flush=True)
    sys.stdin.readline()
print("noise")
print(json.dumps({"echo": p["n"], "hold": p["hold"]}))
"""


def test_start_worker_plain_and_held(tmp_path):
    script = tmp_path / "w.py"
    script.write_text(textwrap.dedent(WORKER))
    assert ops_lab.start_worker({"n": 3}, script=script).result(timeout=60)["echo"] == 3
    h = ops_lab.start_worker({"n": 4}, script=script, hold=True)
    assert h.wait_held() == {"held": True, "trace_ids": ["x"]}
    assert h.proc.poll() is None  # still holding
    h.release()
    res = h.result(timeout=60)
    assert res["echo"] == 4 and res["hold"] is True and res["stderr"] == []


def test_start_worker_reports_a_crash(tmp_path):
    script = tmp_path / "boom.py"
    script.write_text("import sys\nsys.stdin.readline()\nraise SystemExit('kaputt')\n")
    with pytest.raises(RuntimeError, match="kaputt"):
        ops_lab.start_worker({"n": 1}, script=script).result(timeout=60)


# ---------------------------------------------------------------- traffic
def test_ops_cases_two_deployments_run_scoped_users():
    cases = traffic.ops_cases(run_id="m13-t")
    assert len(cases) == 6 and len({c.conversation_id for c in cases}) == 6
    assert [c.deployment for c in cases] == [traffic.OPS_DEPLOYMENTS[0]] * 3 + [traffic.OPS_DEPLOYMENTS[1]] * 3
    assert all(c.user_id.startswith("cust-m13-t-") for c in cases)
    subject = traffic.ops_subject("m13-t")
    assert sum(c.user_id == subject for c in cases) == 2  # the subject has a trace in each deployment
    assert cases[0].tags == ("login_reset", "deployment:support-api@production")
    assert {c.expected_handoff for c in cases} == {"account_security", "billing_specialist", "none"}
