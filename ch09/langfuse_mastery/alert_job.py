"""Module 12: the DIY alert job. A script a scheduler (cron, a k8s CronJob, Airflow) runs every few minutes:

    $PY -m langfuse_mastery.alert_job --env production --state alert-state.json \\
        --url https://alerts.example.com/langfuse --secret-env ALERT_WEBHOOK_SECRET        # the last 15 min vs a week ago
    $PY -m langfuse_mastery.alert_job --tag run:m12-abc123 --env dev \\
        --baseline 2026-11-12T08:00:00+00:00 2026-11-12T08:01:00+00:00 \\
        --current 2026-11-12T08:03:00+00:00 2026-11-12T08:04:00+00:00 \\
        --state .../alert-state.json --url http://127.0.0.1/m12/diy --secret-env M12_DIY_SECRET   # the notebook's windows

One tick = compare a CURRENT window with a BASELINE window (module 11's metrics: `weekly_pull.health_metrics`),
turn what got worse into alerts (`analytics.compare` + `analytics.flag` + `alerting.to_alerts`), decide what to send
against the saved state (`alerting.decide`: dedup, cooldown, resolve), deliver signed webhooks, save the state.
It READS Langfuse only.

You implement TODO 3, `run_once`. The rest is given.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langfuse_mastery import alerting, analytics, weekly_pull


# ---------------------------------------------------------------- given: module 11's metrics for one window
def snapshot(langfuse, window, scope: weekly_pull.Scope) -> dict:
    """One window's metrics, from module 11's `weekly_pull.health_metrics` (your module 11 TODO 2), plus
    `cost_usd.per_request` (cost moves with traffic; per request it doesn't) and the window's dominant `version`.

        {"from": ISO, "to": ISO, "metrics": {...}, "by_version": {...}, "version": "v2" | None}"""
    health = weekly_pull.health_metrics(langfuse, window, scope)
    m = dict(health["metrics"])
    n = m.get("requests") or 0
    m["cost_usd.per_request"] = (m.get("cost_usd.total") or 0.0) / n if n else None
    by_version = health.get("by_version") or {}
    version = max(by_version, key=lambda v: by_version[v].get("requests") or 0) if by_version else None
    return {"from": alerting.iso(window[0]), "to": alerting.iso(window[1]), "metrics": m, "by_version": by_version,
            "version": version}


def scope_dict(scope: weekly_pull.Scope) -> dict:
    """The JSON-friendly scope `alerting` works with."""
    return {"environment": scope.environment, "tags": list(scope.tags)}


def load_state(path) -> dict:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() and p.read_text(encoding="utf-8").strip() else {}


def save_state(path, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(p)  # atomic: a crash mid-write never leaves half a state file


def default_windows(now: datetime, *, minutes: int = 15) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """(baseline, current) for a production tick: the last `minutes`, and the same minutes one week earlier
    (same weekday and hour: traffic and its mix are seasonal)."""
    end = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    current = (end - timedelta(minutes=minutes), end)
    return (current[0] - timedelta(days=7), current[1] - timedelta(days=7)), current


# ---------------------------------------------------------------- TODO 3: one tick
def run_once(langfuse, baseline, current, scope: weekly_pull.Scope, *, state_path, url: str, secret: str,
             rules=alerting.ALERT_RULES, cooldown_s: float = 3600, now: datetime | None = None, post=None,
             links: dict | None = None) -> dict:
    """TODO 3. One tick of the alert job. `baseline` / `current` are (from, to) datetime windows.

    1. b, c = snapshot(...) of both windows
    2. anomalies = analytics.flag(analytics.compare(b["metrics"], c["metrics"]), rules);
       alerts = alerting.to_alerts(anomalies, rules)
    3. state = load_state(state_path); decision = alerting.decide(state, alerts, scope=scope_dict(scope),
       now=now or datetime.now(timezone.utc), cooldown_s=cooldown_s)
    4. for every notification in decision["send"]: payload = alerting.build_payload(notification, scope=...,
       baseline=b, current=c, now=..., links=links); delivery = alerting.deliver(url, payload, secret, post=post)
       (pass `post` only when it is not None). If the delivery FAILED, put that fingerprint's entry back the way it
       was in `state` (or remove it when it wasn't there): a message that never arrived must not count as sent, so
       the next tick sends it again.
    5. save_state(state_path, the new state) and return

        {"baseline": b, "current": c, "anomalies": anomalies, "alerts": alerts,
         "sent": [{"payload": payload, "delivery": delivery}, ...],   # one per notification, delivered or not
         "suppressed": decision["suppressed"], "state": the saved state}"""
    raise NotImplementedError("TODO 3: implement run_once")


# ---------------------------------------------------------------- given: the CLI
def _dt(s: str) -> datetime:
    return alerting.parse_time(s.strip())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tag", action="append", default=[], help="only traces with this tag (repeatable)")
    p.add_argument("--env", help="only this environment")
    p.add_argument("--baseline", nargs=2, metavar=("FROM", "TO"))
    p.add_argument("--current", nargs=2, metavar=("FROM", "TO"))
    p.add_argument("--state", required=True, help="the job's state file (JSON)")
    p.add_argument("--url", required=True, help="the webhook to deliver alerts to")
    p.add_argument("--secret-env", required=True, help="name of the env var holding the signing secret")
    p.add_argument("--cooldown", type=float, default=3600, help="seconds before a firing alert is sent again")
    p.add_argument("--report", help="also write the tick's report (JSON) here")
    args = p.parse_args(argv)
    if bool(args.baseline) != bool(args.current):
        p.error("--baseline and --current go together")
    secret = os.environ.get(args.secret_env)
    if not secret:
        p.error(f"env var {args.secret_env} is not set (the webhook signing secret)")
    baseline, current = ((tuple(map(_dt, args.baseline)), tuple(map(_dt, args.current))) if args.baseline
                         else default_windows(datetime.now(timezone.utc)))
    from langfuse import get_client

    report = run_once(get_client(), baseline, current, weekly_pull.Scope(tags=tuple(args.tag), environment=args.env),
                      state_path=args.state, url=args.url, secret=secret, cooldown_s=args.cooldown)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"{len(report['alerts'])} alert(s): {len(report['sent'])} sent, {len(report['suppressed'])} suppressed")
    for s in report["sent"]:
        print(f"  {s['payload']['title']}  -> {'delivered' if s['delivery']['ok'] else 'FAILED'} "
              f"({s['delivery']['attempts']} attempt(s))")
    return 0 if all(s["delivery"]["ok"] for s in report["sent"]) else 1


if __name__ == "__main__":
    from dotenv import load_dotenv

    from langfuse_lab.config import ENV_FILE
    load_dotenv(ENV_FILE)
    raise SystemExit(main())
