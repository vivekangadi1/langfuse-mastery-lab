import http.client
import urllib.request

import pytest

from langfuse_lab import reset as reset_mod
from langfuse_lab.reset import compose_cmd, main, reset, wait_healthy


def test_compose_cmd_uses_repo_env_and_file():
    cmd = compose_cmd("down", "-v")
    assert cmd[:2] == ["docker", "compose"] and "--env-file" in cmd and cmd[-2:] == ["down", "-v"]


def test_reset_runs_down_then_up_then_waits():
    calls = []
    reset(run=lambda cmd, check: calls.append(cmd[-2:]), wait=lambda: calls.append("wait"))
    assert calls == [["down", "-v"], ["up", "-d"], "wait"]


def test_cli_requires_yes(monkeypatch):
    def boom():
        raise AssertionError("reset must not run without --yes")

    monkeypatch.setattr(reset_mod, "reset", boom)
    with pytest.raises(SystemExit):
        main([])


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_wait_healthy_retries_on_bad_responses(monkeypatch):
    responses = [http.client.IncompleteRead(b""), b"<html>starting</html>", b"[1, 2]",
                 b'{"status":"OK","version":"4.77.0"}']

    def fake_urlopen(url, timeout):
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return _Resp(r)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert wait_healthy(timeout=5, interval=0) == "4.77.0"
    assert responses == []
