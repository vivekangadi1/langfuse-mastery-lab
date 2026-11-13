"""Wipe the ch09 Langfuse project: docker compose down -v && up -d, then wait for health.

    $PY -m langfuse_lab.reset --yes
"""
from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import time
import urllib.request

from .config import COMPOSE_FILE, ENV_FILE

HEALTH_URL = "http://localhost:3000/api/public/health"


def compose_cmd(*args: str) -> list[str]:
    return ["docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE), *args]


def wait_healthy(timeout: float = 300, interval: float = 5) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
                body = json.loads(resp.read())
                if isinstance(body, dict) and body.get("status") == "OK":
                    return body.get("version", "?")
        except (OSError, ValueError, http.client.HTTPException):  # down, starting, or half-written reply
            pass
        time.sleep(interval)
    raise TimeoutError(f"Langfuse not healthy after {timeout}s")


def reset(*, run=subprocess.run, wait=wait_healthy):
    run(compose_cmd("down", "-v"), check=True)
    run(compose_cmd("up", "-d"), check=True)
    return wait()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="confirm: deletes ALL ch09 Langfuse data")
    args = parser.parse_args(argv)
    if not args.yes:
        parser.error("refusing to wipe data without --yes")
    print(f"Langfuse healthy, version {reset()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
