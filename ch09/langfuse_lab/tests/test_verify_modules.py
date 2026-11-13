from types import SimpleNamespace as NS

import pytest

from langfuse_lab.verify import (
    FIELDS, FIELDS_WITH_METADATA, NotYet, check_module_00, check_module_01, fetch_run, module_00_checks,
    module_01_checks, root_observations, run_checks, run_snapshot, trace_observations,
)


def obs(**kw):
    base = dict(id="o", trace_id="t1", type="SPAN", name="n", level="DEFAULT", input=None, output=None,
                latency=0.1, is_root_observation=False, trace_name="support", tags=["run:x"])
    base.update(kw)
    return NS(**base)


class FakeObservations:
    def __init__(self, items):
        self.items = items

    def visible(self):
        return self.items

    def get_many(self, **kwargs):
        if kwargs.get("trace_id"):
            return NS(data=[o for o in self.visible() if o.trace_id == kwargs["trace_id"]])
        return NS(data=[o for o in self.visible() if o.is_root_observation])


def fake_client(items):
    return NS(api=NS(observations=FakeObservations(items)))


GOOD = [
    obs(id="r", type="AGENT", is_root_observation=True, input="q", output="a"),
    obs(id="g", type="GENERATION"), obs(id="t", type="TOOL", latency=3.2),
    obs(id="gr", type="GUARDRAIL"), obs(id="rt", type="RETRIEVER"),
]


def test_module_01_all_pass():
    checks = module_01_checks("run:x", fake_client(GOOD), expect_slow_tool=True)
    assert all(c.fn()[0] for c in checks), [(c.name, c.fn()) for c in checks]


def test_module_01_missing_guardrail_fails():
    items = [o for o in GOOD if o.type != "GUARDRAIL"]
    by_name = {c.name: c.fn() for c in module_01_checks("run:x", fake_client(items))}
    assert by_name["guardrail observation"][0] is False


def test_check_module_01_reports(capsys):
    assert check_module_01("run:x", client=fake_client(GOOD), retries=1) is True


def three_trace_run():
    """Mirrors the real module-01 run: only t2 (refund-policy question) hits the retriever."""
    return [
        obs(id="r1", trace_id="t1", type="AGENT", is_root_observation=True, input="q1", output="a1"),
        obs(id="g1", trace_id="t1", type="GENERATION"), obs(id="to1", trace_id="t1", type="TOOL"),
        obs(id="gr1", trace_id="t1", type="GUARDRAIL"),
        obs(id="r2", trace_id="t2", type="AGENT", is_root_observation=True, input="q2", output="a2"),
        obs(id="g2", trace_id="t2", type="GENERATION"), obs(id="to2", trace_id="t2", type="TOOL"),
        obs(id="gr2", trace_id="t2", type="GUARDRAIL"), obs(id="rt2", trace_id="t2", type="RETRIEVER"),
        obs(id="r3", trace_id="t3", type="AGENT", is_root_observation=True, input="q3", output="a3"),
        obs(id="g3", trace_id="t3", type="GENERATION"), obs(id="gr3", trace_id="t3", type="GUARDRAIL"),
        obs(id="to3", trace_id="t3", type="TOOL", latency=3.2),
    ]


def test_module_01_three_traces_all_pass():
    checks = module_01_checks("run:x", fake_client(three_trace_run()), expect_slow_tool=True)
    results = {c.name: c.fn() for c in checks}
    assert all(r[0] for r in results.values()), results
    assert results["retriever observation"][1] == "1 RETRIEVER observation(s) across 3 trace(s)"


def test_module_01_retriever_missing_from_run_fails():
    items = [o for o in three_trace_run() if o.id != "rt2"]
    by_name = {c.name: c.fn() for c in module_01_checks("run:x", fake_client(items))}
    assert by_name["retriever observation"][0] is False


def test_module_01_root_io_fails_if_any_root_lacks_output():
    items = [obs(**{**vars(o), "output": None}) if o.id == "r2" else o for o in three_trace_run()]
    by_name = {c.name: c.fn() for c in module_01_checks("run:x", fake_client(items))}
    passed, detail = by_name["root has input+output"]
    assert passed is False and "t2" in detail


def test_fields_include_metrics_for_latency():
    assert "metrics" in FIELDS.split(",")


def test_missing_roots_raise_not_yet_mentioning_window():
    checks = {c.name: c for c in module_01_checks("run:x", fake_client([]))}
    with pytest.raises(NotYet, match=r"in the last 24h"):
        checks["generation observation"].fn()


class TruncatedObservations:
    def get_many(self, **kwargs):
        return NS(data=[obs(is_root_observation=True)], meta=NS(cursor="more"))


def test_truncated_results_fail_fast():
    client = NS(api=NS(observations=TruncatedObservations()))
    with pytest.raises(RuntimeError, match="truncated"):
        root_observations(client, "run:x")
    with pytest.raises(RuntimeError, match="truncated"):
        trace_observations(client, "t1")


def test_untruncated_meta_is_fine():
    class Obs:
        def get_many(self, **kwargs):
            return NS(data=[obs(is_root_observation=True)], meta=NS(cursor=None))
    assert len(root_observations(NS(api=NS(observations=Obs())), "run:x")) == 1


def module_00_client(items, flushed):
    return NS(
        api=NS(observations=FakeObservations(items), health=NS(health=lambda: NS(status="OK", version="4.77"))),
        auth_check=lambda: True,
        flush=lambda: flushed.append(1),
    )


def test_hello_trace_passes_if_any_hello_root_has_io():
    items = [
        obs(id="h1", trace_id="t1", trace_name="hello-langfuse", is_root_observation=True, input="hi", output=None),
        obs(id="h2", trace_id="t2", trace_name="hello-langfuse", is_root_observation=True, input="hi", output="hello"),
    ]
    by_name = {c.name: c.fn() for c in module_00_checks("run:x", module_00_client(items, []))}
    assert by_name["hello trace with input+output"][0] is True


def test_hello_trace_missing_raises_not_yet():
    checks = {c.name: c for c in module_00_checks("run:x", module_00_client([], []))}
    with pytest.raises(NotYet):
        checks["hello trace with input+output"].fn()


def test_check_modules_flush_once_before_checks(capsys):
    flushed = []
    hello = [obs(trace_name="hello-langfuse", is_root_observation=True, input="hi", output="hello")]
    assert check_module_00("run:x", client=module_00_client(hello, flushed), retries=1) is True
    assert flushed == [1]
    client = fake_client(GOOD)
    client.flush = lambda: flushed.append(2)
    assert check_module_01("run:x", client=client, retries=1) is True
    assert flushed == [1, 2]


def test_expected_traces_short_is_retryable_fail():
    from langfuse_lab.verify import _run_once
    items = [o for o in three_trace_run() if o.trace_id != "t3"]
    checks = module_01_checks("run:x", fake_client(items), expected_traces=3)
    for c in checks:
        r = _run_once(c)
        assert r.status == "fail" and r.retryable, (c.name, r)
        assert "2/3 traces tagged 'run:x' ingested so far" in r.detail


def test_expected_traces_met_passes():
    from langfuse_lab.verify import _run_once
    checks = module_01_checks("run:x", fake_client(three_trace_run()), expect_slow_tool=True, expected_traces=3)
    assert all(_run_once(c).status == "pass" for c in checks)


def test_check_module_01_accepts_expected_traces(capsys):
    assert check_module_01("run:x", client=fake_client(three_trace_run()), expected_traces=3, retries=1) is True


def test_expect_warning_passes_if_any_root_is_warning():
    items = [obs(**{**vars(o), "level": "WARNING"}) if o.id == "r2" else o for o in three_trace_run()]
    by_name = {c.name: c.fn() for c in module_01_checks("run:x", fake_client(items), expect_warning=True)}
    passed, detail = by_name["tool error surfaced as WARNING"]
    assert passed is True and "WARNING=1" in detail and "DEFAULT=2" in detail


def test_expect_warning_fails_without_warning_root():
    by_name = {c.name: c.fn() for c in module_01_checks("run:x", fake_client(three_trace_run()), expect_warning=True)}
    passed, detail = by_name["tool error surfaced as WARNING"]
    assert passed is False and "DEFAULT=3" in detail


def test_warning_check_absent_by_default():
    names = [c.name for c in module_01_checks("run:x", fake_client(three_trace_run()))]
    assert "tool error surfaced as WARNING" not in names


def test_check_module_01_accepts_expect_warning(capsys):
    items = [obs(**{**vars(o), "level": "WARNING"}) if o.id == "r1" else o for o in three_trace_run()]
    assert check_module_01("run:x", client=fake_client(items), expect_warning=True, retries=1) is True


def test_retriever_missing_detail_suggests_rerun():
    items = [o for o in three_trace_run() if o.id != "rt2"]
    by_name = {c.name: c.fn() for c in module_01_checks("run:x", fake_client(items))}
    assert "the model may have skipped search_kb" in by_name["retriever observation"][1]
    assert "skipped" not in by_name["guardrail observation"][1]


def test_warning_check_handles_sdk_level_enum():
    import enum

    class ObservationLevel(enum.Enum):  # mirrors the SDK's enum: str() would give "ObservationLevel.WARNING"
        DEFAULT = "DEFAULT"
        WARNING = "WARNING"

    items = [obs(**{**vars(o), "level": ObservationLevel.WARNING if o.id == "r3" else ObservationLevel.DEFAULT})
             if o.is_root_observation else o for o in three_trace_run()]
    by_name = {c.name: c.fn() for c in module_01_checks("run:x", fake_client(items), expect_warning=True)}
    assert by_name["tool error surfaced as WARNING"] == (True, "root levels: DEFAULT=2, WARNING=1")


class CountingObservations(FakeObservations):
    """Counts roots queries vs per-trace queries; `hidden_until={trace_id or observation id: n}` hides that trace
    (or that one observation) until the nth roots query."""

    def __init__(self, items, hidden_until=None):
        super().__init__(items)
        self.roots_calls, self.trace_calls, self.hidden_until = 0, 0, hidden_until or {}

    def visible(self):
        return [o for o in self.items
                if max(self.hidden_until.get(o.trace_id, 0), self.hidden_until.get(o.id, 0)) <= self.roots_calls]

    def get_many(self, **kwargs):
        if kwargs.get("trace_id"):
            self.trace_calls += 1
        else:
            self.roots_calls += 1
        return super().get_many(**kwargs)


def counting_client(items, **kw):
    counting = CountingObservations(items, **kw)
    return counting, NS(api=NS(observations=counting), flush=lambda: None)


NO_SLEEP = dict(delay=0, sleep=lambda s: None)


def test_one_attempt_fetches_run_once_for_all_checks(capsys):
    counting, client = counting_client(three_trace_run())
    assert check_module_01("run:x", client=client, expect_slow_tool=True, expected_traces=3, retries=1) is True
    assert (counting.roots_calls, counting.trace_calls) == (1, 3)


def test_each_retry_attempt_refetches_the_run(capsys):
    counting, client = counting_client(three_trace_run(), hidden_until={"t3": 2})  # t3 ingested by attempt 2
    assert check_module_01("run:x", client=client, expect_slow_tool=True, expected_traces=3, retries=3,
                           sleep=lambda s: None) is True
    assert counting.roots_calls == 2 and counting.trace_calls == 3  # attempt 1 stops at 2/3 roots; attempt 2 fetches 3


def test_direct_run_checks_cannot_reuse_a_stale_snapshot():
    """No reset to forget: run_checks opens a new attempt, and the checks' own snapshot refetches."""
    counting, client = counting_client(three_trace_run(), hidden_until={"t3": 2})
    results = run_checks(module_01_checks("run:x", client, expected_traces=3), retries=3, **NO_SLEEP, log=lambda m: None)
    assert all(r.status == "pass" for r in results), results
    assert counting.roots_calls == 2


def test_not_yet_is_memoized_within_an_attempt(capsys):
    counting, client = counting_client([])
    assert check_module_01("run:x", client=client, retries=1) is False
    assert counting.roots_calls == 1
    assert capsys.readouterr().out.count("in the last 24h") == 5  # every check reports the same shared NotYet


def test_outside_run_checks_every_call_fetches_fresh():
    counting, client = counting_client(three_trace_run())
    snap = run_snapshot(client, "run:x")
    snap.get(); snap.get()
    assert counting.roots_calls == 2


def test_check_modules_forward_run_kwargs(capsys):
    sleeps, lines = [], []
    _, client = counting_client([])
    check_module_01("run:x", client=client, retries=2, delay=0.25, sleep=sleeps.append, log=lines.append)
    assert sleeps == [0.25] and lines and "attempt 2/2" in lines[0]
    hello = [obs(trace_name="hello-langfuse", is_root_observation=True, input="hi", output=None)]
    sleeps.clear()
    check_module_00("run:x", client=module_00_client(hello, []), retries=2, sleep=sleeps.append, log=lambda m: None)
    assert len(sleeps) == 1


class RecordingObservations:
    def __init__(self):
        self.calls = []

    def get_many(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("trace_id"):
            return NS(data=[])
        return NS(data=[obs(is_root_observation=True, start_time=None)])


def test_fields_are_selectable_and_forwarded():
    rec = RecordingObservations()
    client = NS(api=NS(observations=rec))
    assert rec.calls == [] and "metadata" in FIELDS_WITH_METADATA.split(",") and "metadata" not in FIELDS.split(",")
    root_observations(client, "run:x")
    trace_observations(client, "t1")
    assert [c["fields"] for c in rec.calls] == [FIELDS, FIELDS]
    rec.calls.clear()
    fetch_run(client, "run:x", fields=FIELDS_WITH_METADATA)
    assert [c["fields"] for c in rec.calls] == [FIELDS_WITH_METADATA] * 2


def test_run_snapshot_forwards_fetch_kwargs():
    import json
    rec = RecordingObservations()
    run_snapshot(NS(api=NS(observations=rec)), "run:x", hours=1, fields=FIELDS_WITH_METADATA).get()
    roots_call = rec.calls[0]
    assert roots_call["fields"] == FIELDS_WITH_METADATA
    since = next(f["value"] for f in json.loads(roots_call["filter"]) if f["column"] == "startTime")
    from datetime import datetime, timedelta, timezone
    assert datetime.now(timezone.utc) - datetime.fromisoformat(since) < timedelta(hours=1, minutes=1)


def test_fetch_run_raises_not_yet():
    with pytest.raises(NotYet, match=r"in the last 24h"):
        fetch_run(fake_client([]), "run:x")
    with pytest.raises(NotYet, match=r"2/3 traces"):
        fetch_run(fake_client([o for o in three_trace_run() if o.trace_id != "t3"]), "run:x", expected_traces=3)
    assert [r.trace_id for r, _ in fetch_run(fake_client(three_trace_run()), "run:x")] == ["t1", "t2", "t3"]


# ---------------------------------------------------------------- module 02
import json  # noqa: E402

from langfuse_lab.verify import _metadata, _run_once, check_module_02, module_02_checks  # noqa: E402

# Metadata JSON-encoded per value ('"4242"' is the string 4242), as SDK v5 may send it. The live server returned it
# already decoded (pid as int) — see test_module_02_passes_on_live_shaped_run.
ROOT_PID, WORKER_PID = '"4242"', '"5151"'


def obs2(**kw):
    base = dict(user_id="", session_id="", environment="staging", version="v1", metadata=None,
                parent_observation_id=None)
    base.update(kw)
    return obs(**base)


def root2(trace_id, user, session, conv, intent="faq", **kw):
    return obs2(id=f"r-{trace_id}", trace_id=trace_id, type="AGENT", is_root_observation=True, input="q", output="a",
                user_id=user, session_id=session,
                metadata={"intent": json.dumps(intent), "conversation_id": json.dumps(conv), "pid": ROOT_PID}, **kw)


def gen2(trace_id):
    return obs2(id=f"g-{trace_id}", trace_id=trace_id, type="GENERATION", parent_observation_id=f"r-{trace_id}")


def module_02_run():
    """2 sessions x 2 turns + 1 single-turn session; 3 users; t5 hands off to a worker process.

    The specialist hangs under a handoff span (grandchild of the root) to exercise the parent-chain walk.
    """
    return [
        root2("t1", "u-alice", "s-alice", "c1"), gen2("t1"),
        root2("t2", "u-alice", "s-alice", "c1"), gen2("t2"),
        root2("t3", "u-bob", "s-bob", "c2"), gen2("t3"),
        root2("t4", "u-bob", "s-bob", "c2"), gen2("t4"),
        root2("t5", "u-carol", "s-carol", "c3", intent="billing"),
        obs2(id="h-t5", trace_id="t5", name="handoff", parent_observation_id="r-t5", metadata={"pid": ROOT_PID}),
        obs2(id="sp5", trace_id="t5", type="AGENT", name="specialist-billing_specialist", parent_observation_id="h-t5",
             metadata={"pid": WORKER_PID, "team": '"billing"'}),
    ]


def m2_results(items, **kw):
    return {c.name: c.fn() for c in module_02_checks("run:x", fake_client(items), **kw)}


def replace(items, oid, **changes):
    return [obs(**{**vars(o), **changes}) if o.id == oid else o for o in items]


HANDOFF = "handoff continued in another process"


def test_module_02_all_pass_on_realistic_run():
    results = m2_results(module_02_run())
    assert all(r[0] for r in results.values()), results
    detail = results[HANDOFF][1]
    assert "specialist-billing_specialist" in detail and "5151" in detail and "4242" in detail


def test_module_02_passes_on_live_shaped_run():
    """Live shape (run:m02-412db5): metadata already decoded, int pids, OTel noise keys, direct-child specialist."""
    noise = {"scope.name": "langfuse-sdk", "resourceAttributes.service.name": "lab", "ls_integration": "langgraph"}
    items = []
    for t, user, session in [("t1", "u-a", "s-a"), ("t2", "u-a", "s-a"), ("t3", "u-b", "s-b"), ("t4", "u-b", "s-b"),
                             ("t5", "u-c", "s-c")]:
        md = {"intent": "faq", "conversation_id": f"c-{session}", "pid": 9616, **noise}
        if t == "t5":
            md["ticket_id"] = "T-1"
        items.append(obs2(id=f"r-{t}", trace_id=t, type="AGENT", is_root_observation=True, input="q", output="a",
                          user_id=user, session_id=session, metadata=md))
    items.append(obs2(id="sp", trace_id="t5", type="AGENT", name="specialist-account_security",
                      parent_observation_id="r-t5", user_id="u-c", session_id="s-c", metadata={"pid": 7777},
                      is_root_observation=True))  # live: the worker's local root is flagged root despite its parent
    results = m2_results(items)
    assert all(r[0] for r in results.values()), results
    assert "7777" in results[HANDOFF][1] and "9616" in results[HANDOFF][1]


def test_module_02_pid_int_vs_encoded_string_is_same_process():
    passed, detail = m2_results(replace(module_02_run(), "sp5", metadata={"pid": 4242}))[HANDOFF]
    assert passed is False and "same process" in detail


def test_module_02_missing_session_fails():
    passed, detail = m2_results(replace(module_02_run(), "r-t3", session_id=""))["every turn has user + session"]
    assert passed is False and "t3" in detail and "session_id=missing" in detail


def test_module_02_user_session_lists_every_offending_root():
    items = replace(replace(replace(module_02_run(), "r-t1", session_id=""), "r-t3", session_id=""),
                    "r-t5", user_id=None)
    passed, detail = m2_results(items)["every turn has user + session"]
    assert passed is False and detail.startswith("3/5 root(s) missing user_id/session_id")
    assert all(t in detail for t in ("t1", "t3", "t5")) and "t2" not in detail


def test_module_02_only_one_multi_turn_session_fails():
    passed, detail = m2_results(replace(module_02_run(), "r-t4", session_id="s-bob-2"))["multi-turn sessions grouped"]
    assert passed is False and "1 session(s)" in detail


def test_module_02_too_few_users_fails():
    passed, detail = m2_results(replace(module_02_run(), "r-t5", user_id="u-bob"))["users tracked"]
    assert passed is False and "2 distinct" in detail


def test_module_02_missing_business_metadata_shows_received_keys():
    items = replace(module_02_run(), "r-t2", metadata={"intent": '"faq"', "pid": ROOT_PID})
    passed, detail = m2_results(items)["business metadata"]
    assert passed is False and "t2" in detail and "conversation_id" in detail
    assert "(got keys: ['intent', 'pid'])" in detail


def test_module_02_no_metadata_at_all_says_none():
    passed, detail = m2_results(replace(module_02_run(), "r-t1", metadata=None))["business metadata"]
    assert passed is False and "(got keys: none)" in detail


def test_module_02_wrong_environment_and_version_list_every_root():
    items = replace(replace(module_02_run(), "r-t1", environment="default"), "r-t4", version=None)
    passed, detail = m2_results(items)["environment + version"]
    assert passed is False and detail.startswith("2/5 root(s)")
    assert "t1" in detail and "'default'" in detail and "t4" in detail
    assert "propagate_attributes(environment=..., version=...)" in detail and "release" not in detail


def test_module_02_wrong_version_fails():
    passed, detail = m2_results(module_02_run(), version="v2")["environment + version"]
    assert passed is False and "'v1'" in detail and "'v2'" in detail and detail.startswith("5/5")


def test_module_02_specialist_in_same_process_fails():
    passed, detail = m2_results(replace(module_02_run(), "sp5", metadata={"pid": ROOT_PID}))[HANDOFF]
    assert passed is False and "same process" in detail


def test_module_02_specialist_without_pid_fails():
    passed, detail = m2_results(replace(module_02_run(), "sp5", metadata={}))[HANDOFF]
    assert passed is False and "span metadata has no pid" in detail


def test_module_02_root_without_pid_names_the_fix():
    items = replace(module_02_run(), "r-t5", metadata={"intent": '"billing"', "conversation_id": '"c3"'})
    passed, detail = m2_results(items)[HANDOFF]
    assert passed is False
    assert "root metadata has no pid — include it in propagate_attributes(metadata=...)" in detail


def test_module_02_orphan_specialist_fails_handoff_but_not_turn_checks():
    """Worker started a new root in the same trace (no parent_span_id): it must not count as a turn or a handoff."""
    items = replace(module_02_run(), "sp5", parent_observation_id=None, is_root_observation=True)
    results = m2_results(items)
    passed, detail = results[HANDOFF]
    assert passed is False
    assert "specialist span is not under the support-request root — pass parent_span_id in trace_context" in detail
    assert results["every turn has user + session"] == (True, "all 5 root(s) have user_id + session_id")
    assert all(results[n][0] for n in results if n != HANDOFF), results


def test_module_02_specialist_flagged_root_but_parented_still_passes():
    """Live server sets is_root_observation=True on the worker's span; its parent link is what proves the handoff."""
    results = m2_results(replace(module_02_run(), "sp5", is_root_observation=True))
    assert all(r[0] for r in results.values()), results


def test_module_02_specialist_with_broken_parent_chain_fails():
    passed, detail = m2_results(replace(module_02_run(), "sp5", parent_observation_id="missing-span"))[HANDOFF]
    assert passed is False and "parent_span_id" in detail


def test_module_02_parent_cycle_does_not_hang():
    items = replace(replace(module_02_run(), "sp5", parent_observation_id="h-t5"), "h-t5", parent_observation_id="sp5")
    assert m2_results(items)[HANDOFF][0] is False


def test_module_02_no_specialist_span_fails_with_flush_hint():
    items = [o for o in module_02_run() if o.id != "sp5"]
    passed, detail = m2_results(items)[HANDOFF]
    assert passed is False and "did the worker flush" in detail


def test_module_02_no_specialist_on_partial_custom_snapshot_is_not_yet():
    items = [o for o in module_02_run() if o.trace_id in ("t1", "t2")]
    client = fake_client(items)
    snap = run_snapshot(client, "run:x", fields=FIELDS_WITH_METADATA)  # caller's snapshot: no expected_traces gate
    checks = {c.name: c for c in module_02_checks("run:x", client, expected_traces=5, snapshot=snap)}
    with pytest.raises(NotYet, match="2/5"):
        checks[HANDOFF].fn()


def test_module_02_partial_run_is_retryable_not_yet():
    items = [o for o in module_02_run() if o.trace_id != "t5"]
    for c in module_02_checks("run:x", fake_client(items)):
        r = _run_once(c)
        assert r.status == "fail" and r.retryable and "4/5 traces" in r.detail, (c.name, r)


@pytest.mark.parametrize("raw, expected", [
    (None, {}),
    ({}, {}),
    ({"pid": 4242, "env": "prod"}, {"pid": 4242, "env": "prod"}),  # already decoded (what the live server returns)
    ({"env": '"prod"', "pid": '"4242"'}, {"env": "prod", "pid": "4242"}),  # JSON-encoded values
    ({"pid": "4242", "flag": "true", "x": "NaN", "n": "3"},  # plain strings stay strings
     {"pid": "4242", "flag": "true", "x": "NaN", "n": "3"}),
    ({"tags": '["a", "b"]', "obj": '{"k": 1}'}, {"tags": ["a", "b"], "obj": {"k": 1}}),
    ('{"intent": "\\"faq\\"", "k": 1}', {"intent": "faq", "k": 1}),
    ("not json", {}),
    ({"raw": "plain text", "bad": '"unterminated'}, {"raw": "plain text", "bad": '"unterminated'}),
])
def test_metadata_decoding(raw, expected):
    assert _metadata(NS(id="o", metadata=raw)) == expected


def test_snapshot_records_its_fields():
    assert run_snapshot(fake_client([]), "run:x").fields == FIELDS
    assert run_snapshot(fake_client([]), "run:x", fields=FIELDS_WITH_METADATA).fields == FIELDS_WITH_METADATA


def real_observation(**kw):
    from datetime import datetime, timezone

    from langfuse.api import ObservationV2
    data = dict(id="r1", trace_id="t1", start_time=datetime.now(timezone.utc), project_id="p", type="AGENT",
                is_root_observation=True)
    data.update(kw)
    return ObservationV2.model_validate(data)


def test_real_observation_without_metadata_group_defeats_attribute_guard():
    """The server omits `metadata` without its field group, but the pydantic model still has the attribute (None)."""
    o = real_observation()
    assert hasattr(o, "metadata") and o.metadata is None
    assert _metadata(o) == {}  # looks like "learner set no metadata": only the snapshot's fields can tell


def test_module_02_rejects_snapshot_fetched_without_metadata_group():
    client = fake_client([real_observation()])
    with pytest.raises(ValueError, match="FIELDS_WITH_METADATA"):
        module_02_checks("run:x", client, snapshot=run_snapshot(client, "run:x"))


def test_module_02_trusts_custom_snapshot_without_fields():
    from langfuse_lab.verify import RunSnapshot
    assert len(module_02_checks("run:x", fake_client([]), snapshot=RunSnapshot(lambda: []))) == 6


def test_metadata_absent_attribute_still_named_for_fakes():
    with pytest.raises(RuntimeError, match="FIELDS_WITH_METADATA"):
        _metadata(NS(id="o"))


def test_check_module_02_reports(capsys):
    assert check_module_02("run:x", client=fake_client(module_02_run()), retries=1) is True
    assert "MODULE PASSED" in capsys.readouterr().out


def test_check_module_02_retries_until_handoff_trace_ingested(capsys):
    counting, client = counting_client(module_02_run(), hidden_until={"t5": 2})
    assert check_module_02("run:x", client=client, retries=3, sleep=lambda s: None, log=lambda m: None) is True
    assert counting.roots_calls == 2 and counting.trace_calls == 5


def test_check_module_02_retries_until_late_specialist_span_lands(capsys):
    lines = []
    counting, client = counting_client(module_02_run(), hidden_until={"sp5": 2})  # worker span lands after attempt 1
    assert check_module_02("run:x", client=client, retries=3, sleep=lambda s: None, log=lines.append) is True
    assert counting.roots_calls == 2 and len(lines) == 1 and HANDOFF in lines[0] and "users tracked" not in lines[0]


def test_module_02_fetches_with_metadata_fields():
    class Recording(RecordingObservations):
        def get_many(self, **kwargs):
            self.calls.append(kwargs)
            return NS(data=[] if kwargs.get("trace_id") else [root2("t1", "u", "s", "c")])

    rec = Recording()
    check = module_02_checks("run:x", NS(api=NS(observations=rec)), expected_traces=None)[0]
    assert check.fn()[0] is True
    assert len(rec.calls) == 2 and all(c["fields"] == FIELDS_WITH_METADATA for c in rec.calls)


def test_specialist_root_does_not_fill_the_expected_traces_gate():
    """Server flags the worker span as a root (with the trace's tags): 4 turns + that span must not pass a 5 gate."""
    items = [o for o in replace(module_02_run(), "sp5", is_root_observation=True) if o.trace_id != "t4"]
    for c in module_02_checks("run:x", fake_client(items)):
        r = _run_once(c)
        assert r.status == "fail" and r.retryable, (c.name, r)
        assert "4/5 traces tagged 'run:x' ingested so far" in r.detail, (c.name, r)


def test_fetch_run_drops_specialist_roots_and_fetches_each_trace_once():
    items = replace(module_02_run(), "sp5", parent_observation_id=None, is_root_observation=True)  # orphan worker
    counting, client = counting_client(items)
    traces = fetch_run(client, "run:x", expected_traces=5, fields=FIELDS_WITH_METADATA)
    assert [r.id for r, _ in traces] == ["r-t1", "r-t2", "r-t3", "r-t4", "r-t5"]
    assert counting.trace_calls == 5


def test_fetch_run_specialist_only_trace_is_not_a_turn():
    items = [obs(id="sp", trace_id="t9", name="specialist-x", is_root_observation=True)]
    with pytest.raises(NotYet, match="no root observation"):
        fetch_run(fake_client(items), "run:x")
