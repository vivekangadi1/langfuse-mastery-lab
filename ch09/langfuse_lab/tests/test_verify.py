from langfuse_lab.verify import Check, FeatureUnavailable, NotYet, print_report, run_checks


def test_retries_until_pass():
    attempts = []
    def flaky():
        attempts.append(1)
        return len(attempts) >= 3, f"attempt {len(attempts)}"
    results = run_checks([Check("flaky", flaky)], retries=5, delay=0, sleep=lambda s: None)
    assert results[0].status == "pass" and len(attempts) == 3


def test_fails_after_retries():
    results = run_checks([Check("never", lambda: (False, "nope"))], retries=2, delay=0, sleep=lambda s: None)
    assert results[0].status == "fail" and results[0].detail == "nope"


def test_unavailable_is_not_a_failure(capsys):
    def missing():
        raise FeatureUnavailable("Enterprise only")
    results = run_checks([Check("audit logs", missing)], retries=1, delay=0, sleep=lambda s: None)
    assert results[0].status == "unavailable"
    assert print_report(results) is True
    assert "⚠️" in capsys.readouterr().out


def test_exception_becomes_fail():
    def boom():
        raise RuntimeError("api down")
    results = run_checks([Check("boom", boom)], retries=1, delay=0, sleep=lambda s: None)
    assert results[0].status == "fail" and "api down" in results[0].detail


def test_unexpected_exception_fails_fast_without_retry():
    attempts = []
    def boom():
        attempts.append(1)
        raise RuntimeError("bad filter")
    results = run_checks([Check("boom", boom)], retries=5, delay=0, sleep=lambda s: None)
    assert results[0].status == "fail" and len(attempts) == 1


def test_not_yet_is_retried():
    attempts = []
    def late():
        attempts.append(1)
        if len(attempts) < 3:
            raise NotYet("not ingested")
        return True, "ok"
    results = run_checks([Check("late", late)], retries=5, delay=0, sleep=lambda s: None)
    assert results[0].status == "pass" and len(attempts) == 3


def test_retry_progress_is_reported():
    lines, calls = [], []
    def flaky():
        calls.append(1)
        return len(calls) >= 3, "x"
    run_checks([Check("flaky", flaky), Check("ok", lambda: (True, "y"))],
               retries=4, delay=0, sleep=lambda s: None, log=lines.append)
    assert lines == [
        "…waiting for ingestion (attempt 2/4): flaky",
        "…waiting for ingestion (attempt 3/4): flaky",
    ]
