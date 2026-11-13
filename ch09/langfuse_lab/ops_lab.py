"""Module 13 (production ops) lab support, no Langfuse code: what this edition can do, the live stack's topology, the
SDK lab's scenarios, and a worker runner that can hold a process (with its SDK buffer) open.

Edition facts were read from the server source at tag v4.77.0 (read-only, GitHub):
`web/src/features/entitlements/constants/entitlements.ts`, `organizationApiKeyRouter.ts`,
`authenticateOrganizationApiRequest.ts`, `packages/shared/src/features/rbac/*`. The live probes are in the module 13
plan (docs/plans/2026-11-12-langfuse-module-13.md).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from .config import COMPOSE_FILE
from .specialist import LAB_ROOT, _tail, _worker_env

OPS_TAG = "ops-lab"            # stable tag on every module-13 trace (PREFIX + OPS_TAG)
SDK_ENVIRONMENT = "sdk-lab"    # the SDK lab's cheap traces (no LLM) live in their own environment
SCORE_NAME = "handoff_correct"
ROOT_NAME = "support-request"
STAGING_KEY_ENV = ("M13_STAGING_PUBLIC_KEY", "M13_STAGING_SECRET_KEY")  # optional TODO 5 (created in the UI)

# ------------------------------------------------------------------ edition facts (server v4.77.0)
OSS_ENTITLEMENTS = frozenset({"trace-deletion", "scheduled-blob-exports", "in-app-agent"})
ENTERPRISE_ENTITLEMENTS = OSS_ENTITLEMENTS | frozenset({
    "rbac-project-roles", "self-host-allowed-organization-creators", "self-host-ui-customization", "audit-logs",
    "data-retention", "prompt-protected-labels", "admin-api", "external-media-storage", "storage-iam-role-auth"})
ENTITLEMENTS = {"oss": OSS_ENTITLEMENTS, "enterprise": ENTERPRISE_ENTITLEMENTS}

# feature -> (entitlement, what it means on OSS, the OSS workaround). Cloud-only features are in CLOUD_ONLY.
ENTERPRISE_ONLY = {
    "organization API keys + the organization management API": (
        "admin-api",
        "no org keys on OSS: creating one needs admin-api, the 'Organization settings -> API keys' page is hidden, and "
        "every call made with an org key is refused (403 'not available on your current plan'). So projects, project "
        "API keys and memberships cannot be managed through the API (POST/PUT/DELETE /api/public/projects, "
        "/projects/{id}/apiKeys, /organizations/memberships, /projects/{id}/memberships); the project key gets 403",
        "manage projects, keys and members in the UI and keep the plan (ops_plan.PLAN) as the reviewed source of truth"),
    "SCIM user provisioning (/api/public/scim)": (
        "admin-api",
        "SCIM is authenticated with an org key, so it is unavailable with the org API",
        "invite people in the UI (Organization settings -> Members); deprovision by removing the membership"),
    "project-level roles": (
        "rbac-project-roles",
        "a member's ORGANIZATION role applies to every project of the organization (no per-project override)",
        "isolate people with a separate organization (e.g. contractors only in a non-prod org)"),
    "native data retention": (
        "data-retention",
        "Project settings -> Data retention is not available; projects keep data forever (retention_days=None)",
        "a scheduled deletion job through the public API (trace-deletion is in OSS): ops.expired + delete_traces"),
    "audit logs": (
        "audit-logs",
        "no record of who changed prompts, keys, members or settings",
        "keep the plan and prompt changes in git; the worker logs each trace deletion with the requesting API key"),
    "protected prompt labels": (
        "prompt-protected-labels",
        "any MEMBER (prompts:CUD) can move the `production` label, i.e. deploy a prompt",
        "keep production prompts in a project where people are VIEWERs and a CI key moves labels (module 10)"),
    "UI customization and allowed organization creators": (
        "self-host-ui-customization / self-host-allowed-organization-creators",
        "every user can create organizations; no custom branding or links",
        "AUTH_DISABLE_SIGNUP=true and SSO with an allowed domain (self-hosting configuration)"),
}
CLOUD_ONLY = {
    "spend alerts (organization billing)": "Langfuse Cloud billing feature (`cloud-spend-alerts`): module 12 shows it "
                                           "as unavailable; self-hosted, use a spend monitor (module 12 TODO 2)",
    "managed SSO per domain + enforced SSO": "Langfuse Cloud; self-hosted uses the AUTH_* provider variables",
}
EE_LICENSE_ENV = "LANGFUSE_EE_LICENSE_KEY"
_LICENSE_RE = re.compile(r"^\s*-?\s*" + EE_LICENSE_ENV + r"\s*[:=]\s*(\S+)")


def stack_edition(compose_path=COMPOSE_FILE) -> tuple[str, str]:
    """("oss" | "enterprise", detail): does the compose file give the server an Enterprise license key?"""
    path = Path(compose_path)
    if not path.exists():
        return "oss", f"{path} not found (assuming OSS)"
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LICENSE_RE.match(line.split("#", 1)[0])
        if m and m.group(1).strip("\"'"):
            return "enterprise", f"{EE_LICENSE_ENV} is set in {path.name}"
    return "oss", f"no {EE_LICENSE_ENV} in {path.name}: the open-source edition (plan `oss`)"


# ------------------------------------------------------------------ the live stack (read-only)
HEALTH = {"web": "http://localhost:3000/api/public/health", "web ready": "http://localhost:3000/api/public/ready",
          "worker": "http://127.0.0.1:3030/api/health"}
ROLES = {  # container name suffix -> (role, stateful, how it scales)
    "langfuse_web": ("UI + public API + OTLP ingestion endpoint", False, "horizontally, behind a load balancer"),
    "langfuse_worker": ("queues: ingestion, evals, deletions, exports, monitors", False,
                        "horizontally; more workers = more queue throughput"),
    "langfuse_clickhouse": ("traces, observations, scores (OLAP)", True, "managed ClickHouse / a cluster with replicas"),
    "langfuse_postgres": ("users, projects, keys, prompts, configs (OLTP)", True, "managed Postgres"),
    "langfuse_redis": ("queues + caches (BullMQ)", True, "managed Redis/Valkey (noeviction)"),
    "langfuse_minio": ("raw events, media, exports (S3)", True, "S3 / GCS / Azure Blob"),
}


def _docker_json(args: list[str]) -> list[dict]:
    try:
        out = subprocess.run(["docker", *args, "--format", "{{json .}}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{"error": str(exc)}]
    if out.returncode != 0:
        return [{"error": (out.stderr or out.stdout).strip()[:300]}]
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def _health(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return f"{resp.status} {resp.read(200).decode('utf-8', 'replace')}"
    except Exception as exc:
        return f"unreachable ({exc})"


def topology(prefix: str = "ch09_langfuse_") -> dict:
    """The running stack as `docker ps` / `docker stats --no-stream` see it, plus the health endpoints (read-only)."""
    ps = [r for r in _docker_json(["ps"]) if "error" in r or str(r.get("Names", "")).startswith(prefix)]
    stats = {r.get("Name"): r for r in _docker_json(["stats", "--no-stream"]) if "error" not in r}
    containers = []
    for r in ps:
        if "error" in r:
            containers.append(r)
            continue
        name = r["Names"]
        role = next((v for k, v in ROLES.items() if name.endswith(k)), ("?", None, "?"))
        st = stats.get(name, {})
        containers.append({"name": name, "image": r.get("Image"), "status": r.get("Status"), "ports": r.get("Ports"),
                           "role": role[0], "stateful": role[1], "scales": role[2], "cpu": st.get("CPUPerc"),
                           "mem": st.get("MemUsage")})
    return {"containers": containers, "health": {k: _health(u) for k, u in HEALTH.items()}}


# ------------------------------------------------------------------ the SDK lab
# Each scenario is ONE deployment process with its own client (the singleton trap: module 05). The client settings are
# passed to the learner's make_client; `exit` says how the process ends: "finish" calls the learner's finish(),
# "killed" dies with os._exit (an OOM kill, a frozen Lambda) before anything flushes it.
SDK_SCENARIOS = (
    {"name": "defaults", "client": {}, "n": 20, "pace_s": 0.05, "exit": "finish",
     "why": "SDK defaults: flush_at 512 spans, flush_interval 5 s, timeout 5 s"},
    {"name": "tuned", "client": {"flush_at": 6, "flush_interval": 0.5}, "n": 20, "pace_s": 0.05, "exit": "finish",
     "why": "small batches, sent while serving (fresher data, more requests)"},
    {"name": "flush-per-request", "client": {}, "n": 10, "pace_s": 0.0, "exit": "finish", "flush_each": True,
     "why": "the anti-pattern: flush() inside the request puts a network round trip on the request path"},
    {"name": "outage", "client": {"flush_at": 6, "flush_interval": 0.5}, "n": 30, "pace_s": 0.25, "exit": "finish",
     "outage": {"delay_s": 2.0, "seconds": 6.0}, "planted": True,
     "why": "PLANTED: Langfuse answers 503 for 6 s; the default retry budget (timeout=5) gives up on a batch"},
    {"name": "outage-hardened", "client": {"flush_at": 6, "flush_interval": 0.5, "timeout": 20}, "n": 30,
     "pace_s": 0.25, "exit": "finish", "outage": {"delay_s": 2.0, "seconds": 6.0},
     "why": "the same outage with timeout=20: the exporter keeps retrying for up to 20 s per batch"},
    {"name": "killed-job", "client": {"flush_interval": 60}, "n": 10, "pace_s": 0.0, "exit": "killed",
     "planted": True, "why": "PLANTED: a batch job dies (os._exit) before its buffer is exported"},
    {"name": "job", "client": {"flush_interval": 60}, "n": 10, "pace_s": 0.0, "exit": "finish",
     "why": "the same job ending with finish(): everything is exported before the process exits"},
)
SPANS_PER_REQUEST = 3  # support-request -> guardrail + kb-search (the worker's cheap request)
WORKER = LAB_ROOT / "langfuse_mastery" / "_ops_worker.py"


def scenario(name: str) -> dict:
    return next(s for s in SDK_SCENARIOS if s["name"] == name)


class WorkerHandle:
    """A running worker process: JSON payload on stdin (one line); with hold=True it reports `held` and waits for
    `release()` before it finishes (its SDK buffer stays in memory meanwhile)."""

    def __init__(self, proc: subprocess.Popen, script: Path):
        self.proc, self.script = proc, script
        self.held: dict | None = None

    def wait_held(self) -> dict:
        line = self.proc.stdout.readline()
        if not line.strip():
            raise RuntimeError(f"worker {self.script.name} ended before holding: {_tail(self.proc.stderr.read())}")
        self.held = json.loads(line)
        return self.held

    def release(self) -> None:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.write("go\n")
            self.proc.stdin.close()

    def result(self, timeout: float = 120) -> dict:
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.proc.kill()
            raise RuntimeError(f"worker {self.script.name} timed out after {timeout:g}s") from exc
        if self.proc.returncode != 0:
            raise RuntimeError(f"worker {self.script.name} exited {self.proc.returncode}; stderr tail:\n{_tail(err)}")
        lines = [ln for ln in out.splitlines() if ln.strip()]
        try:
            result = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"worker {self.script.name} printed no JSON on its last stdout line; stdout tail:\n"
                               f"{_tail(out)}\nstderr tail:\n{_tail(err)}") from exc
        result["stderr"] = [ln for ln in (err or "").splitlines() if ln.strip()][-12:]
        return result


def start_worker(payload: dict, *, script: Path = WORKER, hold: bool = False) -> WorkerHandle:
    """Start `script` with the venv Python and the payload on stdin (UTF-8, ch09/ on PYTHONPATH, os.environ passed
    through so LANGFUSE_* reach the child)."""
    script = Path(script).resolve()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=_worker_env(),
                            creationflags=creationflags)
    proc.stdin.write(json.dumps({**payload, "hold": hold}) + "\n")
    proc.stdin.flush()
    handle = WorkerHandle(proc, script)
    if not hold:
        proc.stdin.close()
    return handle
