"""Module 12: the NATIVE alerts and automations, as data (no Langfuse code). The learner creates them in the UI.

On server 4.77 monitors ("Alerts") and automations exist on OSS but have NO public API (tRPC only), so they are
configured by hand from `render(...)`; verify checks what they DELIVER to the local receiver instead.

Verified in the server code (v4.77): a webhook URL must be http(s) on port 80/443 and pass the outbound SSRF check,
which blocks `host.docker.internal`, `localhost`, `*.internal` and private IPs unless the host is whitelisted
(`LANGFUSE_WEBHOOK_WHITELISTED_HOST`). The check runs when the automation is SAVED and on every delivery.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import COMPOSE_FILE

ALERT_TAG = "alerts-lab"      # stable tag on every module-12 trace (PREFIX + ALERT_TAG): the native monitors filter on it
LIVE_LABEL = "live"           # the prompt label this module's agent serves; a deploy moves it
SCORE_NAME = "handoff_correct"
ROOT_NAME = "support-request"
MONITOR_PATH, PROMPT_PATH, DIY_PATH = "/m12/monitor", "/m12/prompt", "/m12/diy"
VERSION_PREFIX = "prompt-v"   # a trace's `version` = the prompt version the `live` label served it: "prompt-v3"
DIY_SECRET_ENV = "M12_DIY_SECRET"  # the DIY job's signing secret (the notebook sets it; the cron-style tick reads it)
SECRET_ENV = {"monitor": "M12_MONITOR_WEBHOOK_SECRET", "prompt": "M12_PROMPT_WEBHOOK_SECRET"}
WHITELIST_HOST = "host.docker.internal"
WHITELIST_LINE = f"LANGFUSE_WEBHOOK_WHITELISTED_HOST: {WHITELIST_HOST}"
COMPOSE_UP = "docker compose --env-file .env -f ch09/docker-compose.langfuse.yml up -d"

NATIVE_HINT = (
    "native webhooks cannot reach this machine on this stack: Langfuse's outbound SSRF check blocks "
    f"{WHITELIST_HOST} and private IPs (verified 4.77: 'Blocked hostname detected' when the automation is saved and "
    "on every delivery). To enable them, add this line to ch09/docker-compose.langfuse.yml under langfuse-worker -> "
    "environment (the &langfuse-worker-env block; langfuse-web inherits it, and BOTH containers need it: web checks "
    f"on save, the worker on delivery):\n      {WHITELIST_LINE}\nthen run from the repo root: {COMPOSE_UP} "
    "(recreates langfuse-web and langfuse-worker, keeps all data). The receiver listens on 127.0.0.1:80; Langfuse "
    f"posts to http://{WHITELIST_HOST}/m12/... (ports 80/443 only).")

# One monitor = one Metrics API query over a sliding window, compared with a fixed threshold, evaluated every minute.
MONITORS = (
    {"key": "routing", "name": "routing-pass-rate", "planted": True,
     "view": "scores-boolean", "measure": "value", "aggregation": "avg",
     "filters": [("Name", "=", SCORE_NAME), ("Tags", "any of", "{tag}")],
     "window": "5m", "operator": "LT", "alert": 0.9, "warning": None, "no_data": "SHOW_NO_DATA", "renotify": "OFF",
     "why": "routing quality: the share of requests routed to the right team. SHOW_NO_DATA: an idle window is not a "
            "0% pass rate (SUBSTITUTE_ZERO, the default, would page you every quiet night)."},
    {"key": "latency", "name": "request-latency-p95", "planted": False,
     "view": "observations", "measure": "latency", "aggregation": "p95",
     "filters": [("Name", "=", ROOT_NAME), ("Tags", "any of", "{tag}")],
     "window": "5m", "operator": "GT", "alert": 20000, "warning": None, "no_data": "SHOW_NO_DATA", "renotify": "OFF",
     "why": "a latency guard rail (milliseconds). It must stay quiet in this module: nothing here gets slower."},
    {"key": "spend", "name": "spend-5m", "planted": False,
     "view": "observations", "measure": "totalCost", "aggregation": "sum",
     "filters": [("Tags", "any of", "{tag}")],
     "window": "5m", "operator": "GT", "alert": 0.05, "warning": None, "no_data": "SUBSTITUTE_ZERO", "renotify": "OFF",
     "why": "a cost guard rail (USD per 5 minutes). An idle window really costs $0, so SUBSTITUTE_ZERO is right here."},
)
AUTOMATIONS = (
    {"key": "monitor", "name": "alerts → local webhook", "event_source": "Alerts (monitor events)",
     "actions": None, "filter": None, "path": MONITOR_PATH, "secret_env": SECRET_ENV["monitor"]},
    {"key": "prompt", "name": "prompt deploys → local webhook", "event_source": "Prompt versions",
     "actions": ["updated"], "filter": [("Name", "=", "{prompt}"), ("Labels", "any of", LIVE_LABEL)],
     "path": PROMPT_PATH, "secret_env": SECRET_ENV["prompt"]},
)


def agent_version(prompt_version: int) -> str:
    """The trace `version` for a request served by prompt version `prompt_version` (what alerts name)."""
    return f"{VERSION_PREFIX}{int(prompt_version)}"


def prompt_version_of(version) -> int | None:
    """agent_version's inverse: "prompt-v3" -> 3; None for anything else."""
    text = str(version or "")
    tail = text[len(VERSION_PREFIX):] if text.startswith(VERSION_PREFIX) else ""
    return int(tail) if tail.isdigit() else None


def names(prefix: str = "") -> dict:
    """Project-global names for one PREFIX: monitors, automations, the stable tag."""
    return {"tag": prefix + ALERT_TAG, **{f"monitor.{m['key']}": prefix + m["name"] for m in MONITORS},
            **{f"automation.{a['key']}": prefix + a["name"] for a in AUTOMATIONS}}


def _filters(rows, **values) -> str:
    return " AND ".join(f"{col} {op} {val.format(**values)}" for col, op, val in rows)


def render(prefix: str = "", *, prompt_name: str, receiver=None) -> str:
    """The UI spec the learner follows (plain text)."""
    n = names(prefix)
    urls = {a["key"]: (receiver.docker_url(a["path"]) if receiver is not None else None)
            or f"http://{WHITELIST_HOST}{a['path']}" for a in AUTOMATIONS}
    lines = ["1) Automations (left nav: Automations, or from the alert form) -> New automation, twice:"]
    for a in AUTOMATIONS:
        lines += [f"   • name {prefix + a['name']!r}: trigger = {a['event_source']}"
                  + (f", actions {a['actions']}" if a["actions"] else "")
                  + (f", filter {_filters(a['filter'], prompt=prompt_name)}" if a["filter"] else ""),
                  f"     action = Webhook, URL {urls[a['key']]}, API version v1. Save, then copy the signing secret "
                  f"(lf-whsec_…, shown once) into the repo-root .env as {a['secret_env']}=…"]
    lines.append("2) Alerts (left nav) -> New alert, three times (each one: automation "
                 f"{prefix + AUTOMATIONS[0]['name']!r}):")
    for m in MONITORS:
        thresholds = f"{m['operator']} {m['alert']} -> ALERT" + (f", {m['warning']} -> WARNING" if m["warning"] else "")
        lines += [f"   • {n['monitor.' + m['key']]!r}: {m['aggregation']}({m['view']}.{m['measure']}) where "
                  f"{_filters(m['filters'], tag=n['tag'])}; window {m['window']}; {thresholds}; no data "
                  f"{m['no_data']}; re-notify {m['renotify']}",
                  f"     ({m['why']})"]
    return "\n".join(lines)


_WHITELIST_RE = re.compile(r"^\s*(LANGFUSE_WEBHOOK_WHITELISTED_HOST|LANGFUSE_WEBHOOK_ALLOWED_HOSTS)\s*:\s*(.+?)\s*$")


def native_webhooks_ready(compose_path=COMPOSE_FILE) -> tuple[bool, str]:
    """(ready, detail): does the compose file whitelist host.docker.internal for webhooks? (The running containers
    only see it after `docker compose ... up -d`.)"""
    path = Path(compose_path)
    if not path.exists():
        return False, f"{path} not found"
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _WHITELIST_RE.match(line.split("#", 1)[0])
        if m:
            hosts = [h.strip().strip("\"'").lower() for h in m.group(2).strip().strip("\"'").split(",")]
            if WHITELIST_HOST in hosts:
                return True, f"{m.group(1)} includes {WHITELIST_HOST} in {path.name}"
    return False, f"{path.name} does not whitelist {WHITELIST_HOST} for webhooks"
