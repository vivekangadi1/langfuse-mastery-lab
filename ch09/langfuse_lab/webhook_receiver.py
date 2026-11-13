"""Module 12: a local webhook receiver for alert and prompt deliveries (stdlib only, no Langfuse code).

    rx = Receiver(log_path, secrets={"/m12/diy": "s3cret"}).start()   # 127.0.0.1:80 (falls back to a free port)
    rx.url("/m12/diy")          # what code on this machine posts to
    rx.docker_url("/m12/monitor")  # what Langfuse's containers post to: http://host.docker.internal/m12/monitor
    rx.fail_next("/m12/diy", 1)    # a planted outage: the next POST to that path gets 503
    rx.deliveries("/m12/diy")      # every attempt, oldest first

It behaves like a production receiver: it checks the `x-langfuse-signature` (Langfuse's scheme, module 09: `t=<unix>,
v1=<hex HMAC-SHA256(secret, f"{t}.{body}")>`, rejected with 401 when wrong or older than `tolerance_s`), acknowledges
a re-delivered envelope id without processing it twice (`duplicate: true`), and answers fast. Every attempt is one
JSON line in `log_path`: received_at, path, status, headers (lower-cased), the raw body, the envelope id/type,
signature_ok (None when the path has no secret), duplicate, planted_outage.

Why 127.0.0.1:80: Langfuse only delivers to ports 80/443, and Docker Desktop forwards `host.docker.internal:80` from the
containers to this machine's loopback (verified: the request arrives from 127.0.0.1). Whether Langfuse is ALLOWED to
call that host is a server setting (see `alert_spec.native_webhooks_ready`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SIGNATURE_HEADER = "x-langfuse-signature"
DOCKER_HOST = "host.docker.internal"


class _Server(ThreadingHTTPServer):
    # http.server sets SO_REUSEADDR, which on Windows lets a second server bind a port that is already in use (and
    # silently steal or split its traffic). A receiver must fail loudly instead.
    allow_reuse_address = False
    daemon_threads = True


def parse_signature(header: str | None) -> tuple[int, str] | None:
    """'t=1731400000,v1=ab12…' -> (1731400000, 'ab12…'); None when malformed."""
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    try:
        return int(parts["t"].strip()), parts["v1"].strip()
    except (KeyError, ValueError):
        return None


def signature_ok(body: str | bytes, header: str | None, secret: str, *, now: float | None = None,
                 tolerance_s: float = 300) -> bool:
    """True when `header` is a valid Langfuse-style signature of `body` under `secret`, at most `tolerance_s` old."""
    parsed = parse_signature(header)
    if not parsed or not secret:
        return False
    ts, mac = parsed
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    want = hmac.new(secret.encode("utf-8"), f"{ts}.{text}".encode("utf-8"), hashlib.sha256).hexdigest()
    fresh = abs((time.time() if now is None else now) - ts) <= tolerance_s
    return fresh and hmac.compare_digest(want, mac)


class Receiver:
    def __init__(self, log_path, *, host: str = "127.0.0.1", port: int = 80, secrets: dict | None = None,
                 tolerance_s: float = 300, fallback_port: bool = True):
        self.log_path = Path(log_path)
        self.host, self.wanted_port, self.fallback_port = host, port, fallback_port
        self.secrets = dict(secrets or {})
        self.tolerance_s = tolerance_s
        self._outages: dict[str, list[int]] = {}
        self._accepted: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.bind_error: str | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "Receiver":
        if self._server:
            return self
        handler = self._handler()
        try:
            self._server = _Server((self.host, self.wanted_port), handler)
        except OSError as exc:
            if not self.fallback_port:
                raise
            self.bind_error = f"port {self.wanted_port}: {exc}"
            self._server = _Server((self.host, 0), handler)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._server.serve_forever, name="m12-webhook-receiver", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @property
    def port(self) -> int | None:
        return self._server.server_address[1] if self._server else None

    @property
    def native_reachable(self) -> bool:
        """Langfuse's containers can only post to port 80/443 (this receiver serves plain http on 80)."""
        return self.port == 80

    def url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def docker_url(self, path: str) -> str | None:
        """The URL to paste into a Langfuse webhook action (None when not on port 80)."""
        return f"http://{DOCKER_HOST}{path}" if self.native_reachable else None

    # ------------------------------------------------------------ test knobs
    def set_secret(self, path: str, secret: str | None) -> None:
        with self._lock:
            if secret:
                self.secrets[path] = secret
            else:
                self.secrets.pop(path, None)

    def fail_next(self, path: str, n: int = 1, status: int = 503) -> None:
        """A planted outage: the next `n` POSTs to `path` get `status` (and are logged as planted_outage)."""
        with self._lock:
            self._outages[path] = [status] * n

    # ------------------------------------------------------------ reading the log
    def deliveries(self, path: str | None = None) -> list[dict]:
        return read_log(self.log_path, path)

    # ------------------------------------------------------------ the HTTP side
    def _record(self, path: str, headers: dict, body: str) -> int:
        try:
            envelope = json.loads(body)
        except ValueError:
            envelope = None
        env_id = envelope.get("id") if isinstance(envelope, dict) else None
        env_type = envelope.get("type") if isinstance(envelope, dict) else None
        row = {"received_at": datetime.now(timezone.utc).isoformat(), "path": path, "headers": headers, "body": body,
               "id": env_id, "type": env_type, "signature_ok": None, "duplicate": False, "planted_outage": False}
        with self._lock:
            outage = self._outages.get(path)
            if outage:
                row["status"], row["planted_outage"] = outage.pop(0), True
            else:
                secret = self.secrets.get(path)
                if secret is not None:
                    row["signature_ok"] = signature_ok(body, headers.get(SIGNATURE_HEADER), secret,
                                                       tolerance_s=self.tolerance_s)
                if row["signature_ok"] is False:
                    row["status"] = 401
                elif env_id is not None and (path, env_id) in self._accepted:
                    row["status"], row["duplicate"] = 200, True
                else:
                    row["status"] = 200 if envelope is not None else 400
                    if env_id is not None and row["status"] == 200:
                        self._accepted.add((path, env_id))
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row["status"]

    def _handler(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (http.server naming)
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n).decode("utf-8", errors="replace")
                headers = {k.lower(): v for k, v in self.headers.items()}
                status = receiver._record(self.path, headers, body)
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": status}).encode())

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):  # keep the notebook quiet
                pass

        return Handler


def read_log(log_path, path: str | None = None) -> list[dict]:
    """The receiver's JSONL log (every attempt, oldest first), optionally for one path."""
    p = Path(log_path)
    if not p.exists():
        return []
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if path is None or r.get("path") == path]
