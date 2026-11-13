import os
import textwrap

import pytest

from langfuse_lab.specialist import SLA_HOURS, resolve_handoff, run_worker

CONVERSATION = ["I was charged twice for order 8129.", "How long until I see the refund?"]


@pytest.mark.parametrize("team,prefix", [
    ("billing_specialist", "BIL"), ("account_security", "SEC"), ("shipping_specialist", "SHP"),
])
def test_resolve_handoff_per_team(team, prefix):
    out = resolve_handoff(team, "double charge", CONVERSATION)
    assert out["team"] == team
    assert out["ticket_id"].startswith(f"{prefix}-") and len(out["ticket_id"]) == len(prefix) + 7
    assert out["sla_hours"] == SLA_HOURS[team]
    assert 0 < len(out["summary"]) <= 120
    assert out == resolve_handoff(team, "double charge", CONVERSATION)  # deterministic


def test_ticket_id_depends_on_reason_and_target():
    a = resolve_handoff("billing_specialist", "double charge", CONVERSATION)["ticket_id"]
    b = resolve_handoff("billing_specialist", "refund delay", CONVERSATION)["ticket_id"]
    assert a != b


def test_summary_is_capped_for_long_conversations():
    out = resolve_handoff("shipping_specialist", "late", ["x" * 500, "y" * 500])
    assert len(out["summary"]) <= 120


def test_unknown_team():
    assert resolve_handoff("sales", "upsell", CONVERSATION) == {"error": "unknown_team"}


def _script(tmp_path, body):
    path = tmp_path / "worker.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_run_worker_round_trips_json_in_a_child_process(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGFUSE_LAB_TEST_VAR", "inherited")
    script = _script(tmp_path, """
        import json, os, sys
        payload = json.load(sys.stdin)
        print("some log line")
        print(json.dumps({"echo": payload, "pid": os.getpid(), "env": os.environ.get("LANGFUSE_LAB_TEST_VAR")}))
        print()
    """)
    out = run_worker(script, {"q": "café ✓"})
    assert out["echo"] == {"q": "café ✓"}
    assert out["pid"] != os.getpid()
    assert out["env"] == "inherited"


def test_run_worker_non_zero_exit_raises_with_stderr(tmp_path):
    script = _script(tmp_path, """
        import sys
        sys.stderr.write("boom: worker exploded")
        sys.exit(3)
    """)
    with pytest.raises(RuntimeError, match="boom: worker exploded"):
        run_worker(script, {})


def test_run_worker_unparsable_output_raises(tmp_path):
    script = _script(tmp_path, """
        import sys
        sys.stderr.write("warn")
        print("not json")
    """)
    with pytest.raises(RuntimeError, match="not json"):
        run_worker(script, {})


def test_run_worker_child_speaks_utf8_without_parent_utf8_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    script = _script(tmp_path, """
        import json
        print(json.dumps({"x": "café ✓"}, ensure_ascii=False))
    """)
    assert run_worker(script, {}) == {"x": "café ✓"}


def test_run_worker_child_can_import_the_lab_from_outside_ch09(tmp_path):
    script = _script(tmp_path, """
        import json
        from langfuse_lab.specialist import resolve_handoff
        print(json.dumps(resolve_handoff("account_security", "2fa", ["lost phone"])))
    """)
    assert run_worker(script, {})["team"] == "account_security"


def test_run_worker_forwards_child_stderr_on_success(tmp_path, capsys):
    script = _script(tmp_path, """
        import sys
        sys.stderr.write("auth failed for exporter\\n")
        print('{"ok": true}')
    """)
    assert run_worker(script, {}) == {"ok": True}
    assert "[worker stderr] auth failed for exporter" in capsys.readouterr().err


def test_run_worker_quiet_child_writes_nothing_to_stderr(tmp_path, capsys):
    script = _script(tmp_path, 'print(\'{"ok": true}\')\n')
    run_worker(script, {})
    assert capsys.readouterr().err == ""


def test_run_worker_timeout_raises_runtime_error(tmp_path):
    script = _script(tmp_path, """
        import sys, time
        sys.stderr.write("started\\n"); sys.stderr.flush()
        time.sleep(30)
    """)
    with pytest.raises(RuntimeError, match="timed out after 1s") as info:
        run_worker(script, {}, timeout=1)
    assert str(script.resolve()) in str(info.value)


def test_run_worker_errors_name_resolved_script_and_keep_only_the_tail(tmp_path):
    script = _script(tmp_path, """
        import sys
        for i in range(100):
            sys.stderr.write(f"line{i:03d}\\n")
        sys.exit(1)
    """)
    with pytest.raises(RuntimeError) as info:
        run_worker(script, {})
    msg = str(info.value)
    assert str(script.resolve()) in msg
    assert "line099" in msg and "line080" in msg and "line079" not in msg
