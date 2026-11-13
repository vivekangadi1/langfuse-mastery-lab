"""Module 12: the DIY alert job's pure logic. You implement these; `pytest langfuse_mastery/tests/test_alerting.py`
checks them.

Module 11 answers "what got worse?" (`analytics.compare` + `analytics.flag`). An alert job has to turn that into
notifications a team can live with: which rule and how bad (`to_alerts`), one incident = one identity
(`fingerprint`), fire once, then stay quiet until a cooldown has passed, and say when it is over (`decide`), where the
evidence is (`alert_scope`), a self-contained payload (`build_payload`), and a signed delivery that survives a flaky
receiver (`deliver`).

No Langfuse imports here: inputs are plain dicts (the alert job's snapshots, module 11's anomaly rows, the JSON
envelopes Langfuse posts). Times are timezone-aware datetimes in, ISO strings out (state and payloads are JSON).

THE STATE (what the job remembers between ticks; one JSON file):

    {fingerprint: {"status": "firing" | "resolved", "since": ISO, "last_sent": ISO, "seq": int,
                   "alert": {...}, "resolved_at": ISO (resolved only)}}

A NOTIFICATION (what `decide` asks the job to send):

    {"status": "firing" | "resolved", "fingerprint", "alert", "since": ISO, "seq": int, "repeat": bool}
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from langfuse_lab.regression import sign  # Langfuse's webhook signature scheme (module 09): t=<unix>,v1=<hmac>

PAYLOAD_TYPE = "agent-alert"
API_VERSION = "v1"
USER_AGENT = "m12-alert-job/1.0"
SIGNATURE_HEADER = "x-langfuse-signature"

# Given: what pages whom. Same shape as module 11's DEFAULT_RULES (analytics.flag reads pattern / direction / min_abs /
# min_pct; the FIRST matching pattern decides) plus a rule `name` and a `severity`. Thresholds are deliberately
# generous on latency and cost: one noisy LLM call must not page anyone.
ALERT_RULES = (
    {"name": "routing-quality-drop", "pattern": "score.handoff_correct.API", "direction": "down",
     "min_abs": 0.15, "min_pct": None, "severity": "critical"},
    {"name": "error-rate-up", "pattern": "error_rate", "direction": "up", "min_abs": 0.2, "min_pct": None,
     "severity": "critical"},
    {"name": "tool-latency-up", "pattern": "latency_ms.tool.*.p95", "direction": "up", "min_abs": 1000.0,
     "min_pct": 0.5, "severity": "warning"},
    {"name": "request-latency-up", "pattern": "latency_ms.request.p95", "direction": "up", "min_abs": 5000.0,
     "min_pct": 1.0, "severity": "warning"},
    {"name": "cost-per-request-up", "pattern": "cost_usd.per_request", "direction": "up", "min_abs": None,
     "min_pct": 0.5, "severity": "warning"},
)


def iso(dt) -> str | None:
    """Given: a datetime (or ISO string) -> ISO string in UTC."""
    if dt is None:
        return None
    return parse_time(dt).isoformat()


def parse_time(v) -> datetime:
    """Given: ISO string (a trailing Z is fine) or datetime -> tz-aware datetime (naive = UTC)."""
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def _fmt(v) -> str:
    if v is None:
        return "–"
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def title(payload: dict) -> str:
    """Given: the one-line summary of a payload (what a chat message or a page shows first)."""
    return (f"[{payload['status'].upper()} {payload['severity']}] {payload['rule']}: {payload['metric']} "
            f"{_fmt(payload['baseline']['value'])} -> {_fmt(payload['current']['value'])} "
            f"(version {payload.get('version') or '?'})")


def to_alerts(anomalies: list[dict], rules=ALERT_RULES) -> list[dict]:
    """analytics.flag rows -> alerts.

    Each anomaly's "rule" is the PATTERN of the rule that matched (that is what analytics.flag returns). Find the rule
    with that pattern in `rules` and return, in the anomalies' order,

        {"rule": the rule's name, "severity", "metric", "direction",
         "baseline": the anomaly's prev, "current": its curr, "delta", "pct"}

    Anomalies whose pattern is not in `rules` are dropped."""
    raise NotImplementedError("TODO 1: implement to_alerts")


def fingerprint(alert: dict, scope: dict) -> str:
    """The identity of an incident: the first 16 hex chars of sha256(f"{rule}|{metric}|{environment}|{tags}") where
    environment is scope["environment"] or "" and tags is ",".join(sorted(scope["tags"] or [])).

    Values and times are NOT part of it: the same problem seen on the next tick is the same incident (that is what
    makes dedup possible), and the same rule in another environment is a different one."""
    raise NotImplementedError("TODO 1: implement fingerprint")


def decide(state: dict, alerts: list[dict], *, scope: dict, now: datetime, cooldown_s: float = 3600) -> dict:
    """What to send on this tick, and the state to save. Never mutates `state`.

    For each alert (fp = fingerprint(alert, scope)):
    - not firing in `state` (absent, or resolved) -> send status "firing", repeat False:
      state[fp] = {"status": "firing", "since": now, "last_sent": now, "seq": 1, "alert": alert}
    - firing and now - last_sent < cooldown_s -> SUPPRESS (send nothing): keep since/last_sent/seq, store the new
      "alert" (the latest numbers), add fp to "suppressed"
    - firing and the cooldown has passed -> send "firing" again with repeat True: last_sent = now, seq + 1
    Then every fingerprint that is firing in `state` but has NO alert on this tick -> send status "resolved"
    (repeat False, its stored "alert", its "since"): state[fp] gets status "resolved", resolved_at = now, seq + 1.
    Resolved entries without a new alert stay as they are (no message).

    A notification is {"status", "fingerprint", "alert", "since", "seq", "repeat"} (seq = the entry's seq after the
    update). Times in the state and the notifications are ISO strings (`iso`).
    Returns {"send": [notifications: firing ones in the alerts' order, then resolved ones],
             "suppressed": [fingerprints], "state": the new state}."""
    raise NotImplementedError("TODO 1: implement decide")


def alert_scope(envelope: dict) -> dict | None:
    """Where the evidence of an alert is, for OUR payloads and LANGFUSE's monitor alerts alike:

    - type "agent-alert" (build_payload):  {"from": current.from, "to": current.to, "tags": scope.tags,
                                           "environment": scope.environment}
    - type "monitor-alert" (a Langfuse monitor; the alert is under envelope["payload"]):
          {"from": fromTimestamp, "to": toTimestamp, "tags": the values of the filters whose column is "tags"
           (any operator that lists values), "environment": the first value of a filter on column "environment"
           (a string, or the first element of a list), or None}
    - anything else (e.g. "prompt-version") -> None.
    "from"/"to" are tz-aware datetimes (`parse_time`)."""
    raise NotImplementedError("TODO 1: implement alert_scope")


def build_payload(notification: dict, *, scope: dict, baseline: dict, current: dict, now: datetime,
                  links: dict | None = None) -> dict:
    """The webhook body for one notification.

    `baseline` / `current` are the alert job's snapshots of the two windows: {"from", "to", "metrics", "version"}.
    Read the alert's metric from BOTH snapshots' metrics (so a "resolved" message shows today's healthy value, not the
    old bad one). Returns (every time an ISO string):

        {"id": f"{fingerprint}:{status}:{since}:{seq}",   # stable: a retry or a re-delivery carries the SAME id
         "type": PAYLOAD_TYPE, "apiVersion": API_VERSION, "timestamp": now,
         "status", "repeat", "rule", "severity", "metric", "direction"  (from the notification / its alert),
         "baseline": {"value", "from", "to"}, "current": {"value", "from", "to"},
         "delta": current - baseline (None if either is None), "version": current["version"],
         "fingerprint", "since", "scope": {"environment", "tags"}, "links": links or {},
         "title": title(payload)}   # set last: title() reads the other fields"""
    raise NotImplementedError("TODO 1: implement build_payload")


def http_post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    """Given: one POST; returns the HTTP status (4xx/5xx included). Connection errors raise OSError."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def deliver(url: str, payload: dict, secret: str, *, post=None, sleep=time.sleep, clock=time.time,
            attempts: int = 4, backoff_s: float = 0.5, timeout_s: float = 5.0) -> dict:
    """POST `payload`, signed the way Langfuse signs its webhooks, with retries.

    - body = json.dumps(payload, separators=(",", ":"), sort_keys=True) encoded as UTF-8: built ONCE, so every attempt
      sends the same bytes (and the same id: the receiver can drop re-deliveries)
    - headers per attempt: "content-type": "application/json", "user-agent": USER_AGENT,
      SIGNATURE_HEADER: sign(body, secret, int(clock()))   (signed per attempt: a fresh timestamp)
    - `post(url, body_bytes, headers, timeout_s) -> HTTP status` (default `http_post`); it may raise OSError
    - 2xx -> done (ok). An exception, 429 or 5xx -> retry, up to `attempts` attempts in total, sleeping
      backoff_s * 2**i after failed attempt i (0.5 s, 1 s, 2 s, ...; no sleep after the last one). Any other 4xx
      -> stop at once (the receiver rejected the message; retrying can't help)
    Returns {"ok", "status": the last HTTP status or None, "attempts": attempts made, "id": payload["id"],
             "error": the last exception as text or None}."""
    raise NotImplementedError("TODO 1: implement deliver")
