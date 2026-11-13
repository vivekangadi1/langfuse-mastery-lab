"""Specialist handoff: pure business logic + a subprocess runner.

The specialist is what a support agent hands off to. Module 02 runs it in a SEPARATE OS process
so the learner can practise distributed tracing (the child continues the parent's trace).

No Langfuse code here on purpose: the learner writes the worker script and its instrumentation.
`run_worker` only moves JSON over stdin/stdout and passes the environment through, so
LANGFUSE_* keys reach the child (plus UTF-8 stdio and ch09/ on PYTHONPATH).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

TICKET_PREFIX = {"billing_specialist": "BIL", "account_security": "SEC", "shipping_specialist": "SHP"}
SLA_HOURS = {"billing_specialist": 24, "account_security": 4, "shipping_specialist": 48}
SUMMARY_MAX = 120
TAIL_LINES = 20
LAB_ROOT = Path(__file__).resolve().parents[1]  # ch09/, so `import langfuse_lab` works in the child


def resolve_handoff(target: str, reason: str, conversation: list[str]) -> dict:
    """Open a deterministic ticket with the target team (no LLM)."""
    if target not in TICKET_PREFIX:
        return {"error": "unknown_team"}
    digest = hashlib.sha1((reason + target).encode("utf-8")).hexdigest()[:6]
    summary = f"{reason}: {' | '.join(conversation)}"
    if len(summary) > SUMMARY_MAX:
        summary = summary[: SUMMARY_MAX - 3] + "..."
    return {
        "team": target,
        "ticket_id": f"{TICKET_PREFIX[target]}-{digest}",
        "sla_hours": SLA_HOURS[target],
        "summary": summary,
    }


def _tail(text, lines: int = TAIL_LINES) -> str:
    if isinstance(text, bytes):  # TimeoutExpired may carry bytes even in text mode
        text = text.decode("utf-8", errors="replace")
    return "\n".join((text or "").strip().splitlines()[-lines:])


def _worker_env() -> dict:
    """os.environ (LANGFUSE_* keys included) + UTF-8 stdio + the lab importable without a bootstrap."""
    pythonpath = os.pathsep.join(p for p in (str(LAB_ROOT), os.environ.get("PYTHONPATH", "")) if p)
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": pythonpath}


def run_worker(script: Path, payload: dict, *, timeout: float = 60) -> dict:
    """Run `script` with the venv Python, payload as JSON on stdin; return the JSON on its last stdout line.

    A successful child's stderr is forwarded to ours (prefixed) so exporter/auth errors stay visible.
    """
    script = Path(script).resolve()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            [sys.executable, str(script)], input=json.dumps(payload), capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=_worker_env(), timeout=timeout,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"worker timed out after {timeout:g}s: {script}; stderr tail:\n{_tail(exc.stderr)}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"worker {script} exited {proc.returncode}; stderr tail:\n{_tail(proc.stderr)}")
    if proc.stderr.strip():
        for line in proc.stderr.rstrip().splitlines():
            print(f"[worker stderr] {line}", file=sys.stderr)
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"worker {script} printed no JSON on its last stdout line; stdout tail:\n{_tail(proc.stdout)}\n"
            f"stderr tail:\n{_tail(proc.stderr)}"
        ) from exc
