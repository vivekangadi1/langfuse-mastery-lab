"""Module 13: a forwarding tap in front of Langfuse that records every OTLP export (stdlib + opentelemetry-proto, no
Langfuse code).

    tap = Tap(log_path).start()          # 127.0.0.1:<free port>, forwards everything to http://localhost:3000
    Langfuse(base_url=tap.url, ...)      # in the deployment's process: its exports now go through the tap
    tap.outage(6, delay=2)               # a planted outage: in 2 s, answer 503 to OTLP exports for 6 s
    tap.exports()                        # every export attempt, oldest first

Every request is forwarded unchanged (API calls too); only OTLP span exports (`POST /api/public/otel/v1/traces`) are
logged, one JSON line each: received_at, status, ms, bytes, spans, trace_ids (hex, sorted), names (span name ->
count), batch (sha1 of the body: the SDK compresses a batch ONCE and sends the same bytes on every retry, so equal
`batch` = the same batch retried) and outage (the tap answered 503 itself).

Why a tap: the SDK exports in a background thread and says nothing on success. The tap makes batching, retries and
drops visible (and measurable) without touching the server.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OTLP_PATH = "/api/public/otel/v1/traces"
_HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "proxy-connection"}


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False  # see webhook_receiver._Server: fail loudly instead of sharing a port on Windows
    daemon_threads = True


def decode_export(body: bytes, encoding: str | None = None) -> dict:
    """{"spans": n, "trace_ids": [...], "names": {name: count}} of one OTLP/HTTP protobuf export body."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    enc = (encoding or "").strip().lower()
    data = gzip.decompress(body) if enc == "gzip" else zlib.decompress(body) if enc == "deflate" else body
    req = ExportTraceServiceRequest.FromString(data)
    spans = [s for rs in req.resource_spans for ss in rs.scope_spans for s in ss.spans]
    return {"spans": len(spans), "trace_ids": sorted({s.trace_id.hex() for s in spans}),
            "names": dict(Counter(s.name for s in spans))}


def read_exports(log_path) -> list[dict]:
    path = Path(log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Tap:
    def __init__(self, log_path, *, upstream: str = "http://localhost:3000", host: str = "127.0.0.1", port: int = 0,
                 timeout: float = 30.0):
        self.log_path = Path(log_path)
        self.upstream = upstream.rstrip("/")
        self.host, self.wanted_port, self.timeout = host, port, timeout
        self._lock = threading.Lock()
        self._down: tuple[float, float] = (0.0, 0.0)
        self._server: _Server | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "Tap":
        if self._server:
            return self
        self._server = _Server((self.host, self.wanted_port), self._handler())
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._server.serve_forever, name="m13-otlp-tap", daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def url(self) -> str:
        if not self._server:
            raise RuntimeError("the tap is not running (call start())")
        return f"http://{self.host}:{self._server.server_address[1]}"

    # ------------------------------------------------------------ planted outage
    def outage(self, seconds: float, *, delay: float = 0.0) -> tuple[float, float]:
        """From now + delay, for `seconds`, answer 503 to every OTLP export (the API stays up). Returns the window
        (unix seconds)."""
        start = time.time() + delay
        with self._lock:
            self._down = (start, start + seconds)
        return self._down

    def is_down(self, now: float | None = None) -> bool:
        a, b = self._down
        return a <= (time.time() if now is None else now) < b

    # ------------------------------------------------------------ log
    def exports(self, trace_ids=None) -> list[dict]:
        """Logged export attempts, oldest first; with `trace_ids`, only those carrying any of them."""
        rows = read_exports(self.log_path)
        if trace_ids is None:
            return rows
        want = set(trace_ids)
        return [r for r in rows if want.intersection(r.get("trace_ids") or [])]

    def _log(self, row: dict) -> None:
        with self._lock, self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def _forward(self, method: str, path: str, headers, body: bytes) -> tuple[int, bytes, list]:
        req = urllib.request.Request(self.upstream + path, data=body or None, method=method,
                                     headers={k: v for k, v in headers.items() if k.lower() not in _HOP})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), list(resp.headers.items())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), list(exc.headers.items())
        except Exception as exc:  # upstream unreachable: what a load balancer in front of a dead Langfuse answers
            return 502, str(exc).encode("utf-8"), [("content-type", "text/plain")]

    def _handler(self):
        tap = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def _handle(self):
                t0 = time.time()
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                otlp = self.command == "POST" and self.path.split("?", 1)[0] == OTLP_PATH
                row = None
                if otlp:
                    row = {"received_at": datetime.now(timezone.utc).isoformat(), "bytes": len(body),
                           "batch": hashlib.sha1(body).hexdigest()[:16], "outage": tap.is_down()}
                    try:
                        row.update(decode_export(body, self.headers.get("content-encoding")))
                    except Exception as exc:  # not protobuf (e.g. JSON OTLP): still forwarded, counted as unknown
                        row.update(spans=None, trace_ids=[], names={}, decode_error=str(exc)[:200])
                if row is not None and row["outage"]:
                    status, data, headers = 503, b'{"message":"planted outage (otlp_tap)"}', [
                        ("content-type", "application/json")]
                else:
                    status, data, headers = tap._forward(self.command, self.path, self.headers, body)
                self.send_response(status)
                for k, v in headers:
                    if k.lower() not in _HOP:
                        self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                if row is not None:
                    row.update(status=status, ms=round((time.time() - t0) * 1000, 1))
                    tap._log(row)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

        return Handler
