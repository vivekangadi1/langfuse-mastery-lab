"""Verification: each module ends with verify.check_module_N(...) -> prints ✅/❌/⚠️ per requirement."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

ICONS = {"pass": "✅", "fail": "❌", "unavailable": "⚠️"}


class FeatureUnavailable(Exception):
    """Raised by a check when the feature does not exist on this deployment (OSS vs Enterprise/Cloud)."""


class NotYet(Exception):
    """Raised by a check when the data it needs has not been ingested yet (worth retrying)."""


@dataclass(frozen=True)
class Check:
    name: str
    fn: Callable[[], tuple[bool, str]]  # -> (passed, detail)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    retryable: bool = False


def _run_once(check: Check) -> CheckResult:
    try:
        passed, detail = check.fn()
        return CheckResult(check.name, "pass" if passed else "fail", detail, retryable=not passed)
    except FeatureUnavailable as exc:
        return CheckResult(check.name, "unavailable", str(exc))
    except (NotYet, AssertionError) as exc:
        return CheckResult(check.name, "fail", str(exc), retryable=True)
    except Exception as exc:  # bug / bad query / server error: retrying will not help
        return CheckResult(check.name, "fail", f"{type(exc).__name__}: {exc}")


# Identity of the run_checks attempt in progress (None outside run_checks). RunSnapshot keys its memo on it, so a
# new attempt always refetches: there is no reset for a caller to forget.
_ATTEMPT: ContextVar[object | None] = ContextVar("verify_attempt", default=None)


class RunSnapshot:
    """Memoizes one fetch (e.g. `fetch_run`) per run_checks attempt, so every check in an attempt shares it.

    Exceptions are memoized too (NotYet, truncation, server errors): a not-ingested-yet run costs one fetch per
    attempt, not one per check, and every check reports the same error. A new attempt refetches automatically.
    Outside run_checks (e.g. calling `check.fn()` directly) every `get()` fetches fresh.
    `fields`: the observation field groups the fetch requests (None if unknown), so checks can refuse a snapshot
    that cannot carry what they need.
    """

    def __init__(self, fetch: Callable[[], object], *, fields: str | None = None):
        self._fetch = fetch
        self.fields = fields
        self._attempt: object | None = None
        self._value, self._error = None, None

    def get(self):
        attempt = _ATTEMPT.get()
        if attempt is None:
            return self._fetch()
        if attempt is not self._attempt:
            try:
                self._value, self._error = self._fetch(), None
            except Exception as exc:
                self._value, self._error = None, exc
            self._attempt = attempt
        if self._error is not None:
            raise self._error
        return self._value


def _attempt(checks) -> dict[str, CheckResult]:
    """Run `checks` as one attempt: RunSnapshots fetch at most once inside it."""
    token = _ATTEMPT.set(object())
    try:
        return {c.name: _run_once(c) for c in checks}
    finally:
        _ATTEMPT.reset(token)


def run_checks(checks, *, retries: int = 12, delay: float = 5.0, sleep=time.sleep, log=print) -> list[CheckResult]:
    """Re-run not-yet-passing checks (ingestion is async) up to `retries` attempts, `delay` seconds apart.

    Only (False, ...) results and NotYet/AssertionError are retried; any other exception fails immediately.
    Each attempt is a fresh RunSnapshot scope, so retried checks see newly ingested data.
    """
    results = _attempt(checks)
    for attempt in range(2, retries + 1):
        pending = [c for c in checks if results[c.name].retryable]
        if not pending:
            break
        log(f"…waiting for ingestion (attempt {attempt}/{retries}): {', '.join(c.name for c in pending)}")
        sleep(delay)
        results.update(_attempt(pending))
    return [results[c.name] for c in checks]


def print_report(results: list[CheckResult]) -> bool:
    for r in results:
        print(f"{ICONS[r.status]} {r.name}: {r.detail}")
    ok = all(r.status != "fail" for r in results)
    print("\nMODULE PASSED" if ok else "\nMODULE NOT PASSED YET")
    return ok


# ---------------------------------------------------------------- Langfuse queries
# Field groups (verified live, server 4.77): `basic` carries environment/version/user_id/session_id;
# `trace_context` carries tags/trace_name/release; `metrics` carries latency; `metadata` only with its own group.
FIELDS = "core,basic,io,metrics,trace_context"
FIELDS_WITH_METADATA = FIELDS + ",metadata"
WINDOW_HOURS = 24


def get_client():
    from dotenv import load_dotenv
    from langfuse import get_client as _get_client

    from .config import ENV_FILE

    load_dotenv(ENV_FILE)  # LANGFUSE_* keys only; verify must not require Azure settings
    return _get_client()


def _flush(client) -> None:
    """Push spans still buffered in this process before querying for them."""
    if hasattr(client, "flush"):
        client.flush()


def _untruncated(resp, what: str) -> list:
    if getattr(getattr(resp, "meta", None), "cursor", None) is not None:
        raise RuntimeError(f"{what}: results truncated (more pages exist); use a narrower, run-scoped tag")
    return resp.data


def root_observations(client, tag: str, *, hours: int = WINDOW_HOURS, limit: int = 50, fields: str = FIELDS) -> list:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    flt = [
        {"type": "arrayOptions", "column": "traceTags", "operator": "any of", "value": [tag]},
        {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True},
        {"type": "datetime", "column": "startTime", "operator": ">=", "value": since},
    ]
    resp = client.api.observations.get_many(fields=fields, filter=json.dumps(flt), limit=limit)
    return _untruncated(resp, f"root observations tagged {tag!r}")


def trace_observations(client, trace_id: str, *, fields: str = FIELDS) -> list:
    """All observations of one trace. Note: `.input`/`.output` are raw strings (json.loads them to inspect content)."""
    resp = client.api.observations.get_many(trace_id=trace_id, fields=fields, limit=200)
    return _untruncated(resp, f"observations of trace {trace_id}")


# Module 02's worker spans (named "specialist-<team>"). The server flags them as roots and copies the trace's tags
# onto them, so the roots query returns them next to the real turn roots; they are never turns of their own.
SPECIALIST_PREFIX = "specialist-"


def _is_specialist(o) -> bool:
    return (getattr(o, "name", None) or "").startswith(SPECIALIST_PREFIX)


def _turn_roots(roots: list) -> list:
    """First non-specialist root per trace_id, in query order."""
    out = {}
    for r in roots:
        if not _is_specialist(r):
            out.setdefault(r.trace_id, r)
    return list(out.values())


def fetch_run(client, run_tag: str, *, hours: int = WINDOW_HOURS, expected_traces: int | None = None,
              fields: str = FIELDS) -> list[tuple]:
    """[(root, observations)] for every trace tagged `run_tag` (a run sends several requests), one per trace.

    Only turn roots count: specialist-* worker roots are dropped and roots are deduped by trace_id (see _turn_roots).
    Raises NotYet while no root is ingested, or fewer than `expected_traces` (so no check passes on a partial run).
    """
    roots = _turn_roots(root_observations(client, run_tag, hours=hours, fields=fields))
    if not roots:
        raise NotYet(f"no root observation tagged {run_tag!r} in the last {hours}h yet")
    if expected_traces is not None and len(roots) < expected_traces:
        raise NotYet(f"{len(roots)}/{expected_traces} traces tagged {run_tag!r} ingested so far")
    return [(r, trace_observations(client, r.trace_id, fields=fields)) for r in roots]


def run_snapshot(client, run_tag: str, **fetch_kwargs) -> RunSnapshot:
    """One `fetch_run(client, run_tag, **fetch_kwargs)` per attempt, shared by all of a module's checks."""
    return RunSnapshot(lambda: fetch_run(client, run_tag, **fetch_kwargs), fields=fetch_kwargs.get("fields", FIELDS))


def _check_module(checks, client, *, retries: int, **run_kwargs) -> bool:
    """Shared runner for every check_module_N: flush once, run_checks (retries; delay/sleep/log via run_kwargs),
    print the report. Snapshots need no wiring: run_checks scopes them per attempt."""
    _flush(client)
    return print_report(run_checks(checks, retries=retries, **run_kwargs))


# ---------------------------------------------------------------- module 00
def module_00_checks(run_tag: str, client) -> list[Check]:
    def healthy():
        h = client.api.health.health()
        return h.status == "OK", f"server {h.version} status={h.status}"

    def auth():
        return bool(client.auth_check()), "API keys accepted"

    def hello_trace():
        roots = [o for o in root_observations(client, run_tag) if o.trace_name == "hello-langfuse"]
        if not roots:
            raise NotYet(f"no trace named 'hello-langfuse' tagged {run_tag!r} in the last {WINDOW_HOURS}h yet")
        good = [r for r in roots if r.input and r.output]
        if good:
            return True, f"trace {good[0].trace_id} has input+output"
        r = roots[0]
        return False, (f"{len(roots)} hello-langfuse trace(s), none with both; latest {r.trace_id} "
                       f"input={'set' if r.input else 'missing'} output={'set' if r.output else 'missing'}")

    return [Check("server healthy", healthy), Check("credentials valid", auth), Check("hello trace with input+output", hello_trace)]


def check_module_00(run_tag: str, *, client=None, retries: int = 12, **run_kwargs) -> bool:
    """`run_kwargs`: optional run_checks delay/sleep/log overrides (tests); defaults unchanged."""
    client = client or get_client()
    return _check_module(module_00_checks(run_tag, client), client, retries=retries, **run_kwargs)


OSS_PROBES = {
    "Prompt management": lambda api: api.prompts.list(limit=1),
    "Datasets": lambda api: api.datasets.list(limit=1),
    "Score configs": lambda api: api.score_configs.get(limit=1),
    "Annotation queues": lambda api: api.annotation_queues.list_queues(limit=1),
    "LLM connections (for managed judges)": lambda api: api.llm_connections.list(limit=1),
    "Managed evaluators": lambda api: api.evaluators.list(limit=1),
    "Evaluation rules (online eval)": lambda api: api.evaluation_rules.list(limit=1),
    "Model definitions (cost)": lambda api: api.models.list(limit=1),
}


def oss_feature_report(client=None) -> dict:
    """Probe which features this self-hosted deployment exposes via the public API."""
    client = client or get_client()
    report = {}
    for name, probe in OSS_PROBES.items():
        try:
            probe(client.api)
            report[name] = "available"
        except Exception as exc:
            report[name] = f"unavailable ({type(exc).__name__})"
        print(f"{'✅' if report[name] == 'available' else '⚠️'} {name}: {report[name]}")
    return report


# ---------------------------------------------------------------- module 01
SLOW_TOOL_SECONDS = 2.5


def module_01_checks(run_tag: str, client, *, expect_slow_tool: bool = False, expect_warning: bool = False,
                     expected_traces: int | None = None, snapshot: RunSnapshot | None = None) -> list[Check]:
    """All checks share one RunSnapshot, so each run_checks attempt fetches the run once.

    `snapshot`: pass your own to share it with other checks; `expected_traces` then comes from the snapshot
    (the argument here is ignored).
    """
    snapshot = snapshot or run_snapshot(client, run_tag, expected_traces=expected_traces)
    run_traces = snapshot.get

    def has_type(obs_type: str):
        def fn():
            traces = run_traces()
            found = [o for _, items in traces for o in items if o.type == obs_type]
            detail = f"{len(found)} {obs_type} observation(s) across {len(traces)} trace(s)"
            if obs_type == "RETRIEVER" and not found:
                detail += " (the model may have skipped search_kb — rerun the KB request)"
            return bool(found), detail
        return fn

    def root_io():
        traces = run_traces()
        for root, _ in traces:
            if not (root.input and root.output):
                return False, (f"root '{root.name}' trace {root.trace_id} type={root.type} "
                               f"input={'set' if root.input else 'missing'} output={'set' if root.output else 'missing'}")
        return True, f"all {len(traces)} root(s) have input+output"

    def slow_tool_visible():
        slow = [o for _, items in run_traces() for o in items
                if o.type == "TOOL" and float(o.latency or 0) >= SLOW_TOOL_SECONDS]
        return bool(slow), f"{len(slow)} TOOL span(s) >= {SLOW_TOOL_SECONDS}s"

    def warning_root():
        levels = Counter(str(getattr(root.level, "value", root.level) or "DEFAULT") for root, _ in run_traces())
        counts = ", ".join(f"{lvl}={n}" for lvl, n in sorted(levels.items()))
        return levels.get("WARNING", 0) > 0, f"root levels: {counts}"

    checks = [
        Check("root has input+output", root_io),
        Check("generation observation", has_type("GENERATION")),
        Check("tool observation", has_type("TOOL")),
        Check("guardrail observation", has_type("GUARDRAIL")),
        Check("retriever observation", has_type("RETRIEVER")),
    ]
    if expect_slow_tool:
        checks.append(Check("planted slow_tool caught (latency visible)", slow_tool_visible))
    if expect_warning:
        checks.append(Check("tool error surfaced as WARNING", warning_root))
    return checks


def check_module_01(run_tag: str, *, client=None, expect_slow_tool: bool = False, expect_warning: bool = False,
                    expected_traces: int | None = None, retries: int = 12, **run_kwargs) -> bool:
    """`expected_traces`: wait until this many run-tagged traces are ingested, so no check passes on a partial run.
    `run_kwargs`: optional run_checks delay/sleep/log overrides (tests); defaults unchanged."""
    client = client or get_client()
    checks = module_01_checks(run_tag, client, expect_slow_tool=expect_slow_tool, expect_warning=expect_warning,
                              expected_traces=expected_traces)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 02
def _decode(value):
    """Decode a value only if it looks JSON-encoded (SDK v5 may send '"prod"' for prod, '{...}', '[...]').

    Plain values pass through unchanged, so "4242", "true" and "NaN" stay strings; undecodable ones stay as is.
    """
    if isinstance(value, str) and value.lstrip()[:1] in ('"', "{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _metadata(obs) -> dict:
    """An observation's metadata as a dict, tolerating None, a dict, a JSON string, and JSON-encoded values.

    A real ObservationV2 always has the attribute (None when the `metadata` field group was not fetched), so
    module_02_checks guards on the snapshot's fields; the hasattr guard below only catches bare fakes.
    """
    if not hasattr(obs, "metadata"):
        raise RuntimeError(f"observation {getattr(obs, 'id', '?')} has no `metadata` attribute: "
                           "fetch the snapshot with fields=FIELDS_WITH_METADATA")
    raw = _decode(obs.metadata)
    if not isinstance(raw, dict):
        return {}
    return {k: _decode(v) for k, v in raw.items()}


def _under(span, root, by_id: dict) -> bool:
    """True if walking `span`'s parent_observation_id chain (within its trace) reaches `root`."""
    seen, parent = {span.id}, getattr(span, "parent_observation_id", None)
    while parent and parent not in seen:
        if parent == root.id:
            return True
        seen.add(parent)
        parent = getattr(by_id.get(parent), "parent_observation_id", None)
    return False


def module_02_checks(run_tag: str, client, *, expected_traces: int | None = 5, environment: str = "staging",
                     version: str = "v1", snapshot: RunSnapshot | None = None) -> list[Check]:
    """Trace-context checks over every support-request root tagged `run_tag`; one metadata-bearing fetch per attempt.

    `snapshot`: pass your own to share it; it must be fetched with fields=FIELDS_WITH_METADATA (a snapshot that
    does not record its fields is trusted). `expected_traces` then only gates the handoff check's NotYet.
    """
    snapshot = snapshot or run_snapshot(client, run_tag, expected_traces=expected_traces, fields=FIELDS_WITH_METADATA)
    fields = getattr(snapshot, "fields", None)
    if fields is not None and "metadata" not in fields.split(","):
        raise ValueError(f"module 02 needs metadata, but the snapshot was fetched with fields={fields!r}: "
                         "build it with run_snapshot(..., fields=FIELDS_WITH_METADATA)")

    def support_traces():
        """[(root, observations)] one per trace. fetch_run already drops worker roots; this re-filter is defense in
        depth for a custom snapshot built some other way."""
        out = {}
        for root, items in snapshot.get():
            if not _is_specialist(root):
                out.setdefault(root.trace_id, (root, items))
        return list(out.values())

    def roots():
        return [root for root, _ in support_traces()]

    def user_and_session():
        rs = roots()
        bad = [r for r in rs if not (r.user_id and r.session_id)]
        if bad:
            return False, (f"{len(bad)}/{len(rs)} root(s) missing user_id/session_id: " + ", ".join(
                f"{r.trace_id} (user_id={r.user_id or 'missing'}, session_id={r.session_id or 'missing'})"
                for r in bad))
        return True, f"all {len(rs)} root(s) have user_id + session_id"

    def sessions_grouped():
        per_session = Counter(r.session_id for r in roots() if r.session_id)
        multi = sorted(s for s, n in per_session.items() if n >= 2)
        counts = ", ".join(f"{s}={n}" for s, n in sorted(per_session.items())) or "none"
        return len(multi) >= 2, f"{len(multi)} session(s) with >=2 turns (need 2); turns per session: {counts}"

    def users_tracked():
        users = sorted({r.user_id for r in roots() if r.user_id})
        return len(users) >= 3, f"{len(users)} distinct user_id(s) (need 3): {', '.join(users) or 'none'}"

    def business_metadata():
        rs = roots()
        for r in rs:
            md = _metadata(r)
            missing = [k for k in ("intent", "conversation_id") if k not in md]
            if missing:
                got = str(sorted(md)) if md else "none"
                return False, f"root trace {r.trace_id} metadata lacks {', '.join(missing)} (got keys: {got})"
        return True, f"all {len(rs)} root(s) carry intent + conversation_id"

    def env_and_version():
        rs = roots()
        bad = [r for r in rs if r.environment != environment or r.version != version]
        if bad:
            return False, (f"{len(bad)}/{len(rs)} root(s) not environment={environment!r} version={version!r} "
                           "(set via propagate_attributes(environment=..., version=...)): " + ", ".join(
                               f"{r.trace_id} (environment={r.environment!r}, version={r.version!r})" for r in bad))
        return True, f"all {len(rs)} root(s) environment={environment!r} version={version!r}"

    def distributed_handoff():
        traces = support_traces()
        candidates = [(root, items, o) for root, items in traces for o in items if _is_specialist(o)]
        if not candidates:
            if expected_traces is not None and len(traces) < expected_traces:
                raise NotYet(f"no {SPECIALIST_PREFIX}* span yet ({len(traces)}/{expected_traces} traces)")
            return False, (f"no {SPECIALIST_PREFIX}* span in {len(traces)} trace(s) "
                           "(did the worker flush before exiting?)")
        problems = []
        for root, items, span in candidates:
            by_id = {o.id: o for o in items}
            where = f"'{span.name}' in trace {root.trace_id}"
            # Not is_root_observation: the server flags the worker's local root span as a root even when its parent
            # is the support-request root (seen live). The parent chain is the proof.
            if span.id == root.id or not _under(span, root, by_id):
                problems.append(f"{where}: specialist span is not under the support-request root — "
                                "pass parent_span_id in trace_context")
                continue
            root_pid, span_pid = _metadata(root).get("pid"), _metadata(span).get("pid")
            if root_pid is None:
                problems.append(f"{where}: root metadata has no pid — include it in "
                                "propagate_attributes(metadata=...)")
            elif span_pid is None:
                problems.append(f"{where}: span metadata has no pid")
            elif str(root_pid) == str(span_pid):  # int 9616 and "9616" are the same process
                problems.append(f"{where}: ran in the same process as the root (pid {span_pid})")
            else:
                return True, f"{where} ran in pid {span_pid}, root in pid {root_pid}"
        return False, "; ".join(problems)

    return [
        Check("every turn has user + session", user_and_session),
        Check("multi-turn sessions grouped", sessions_grouped),
        Check("users tracked", users_tracked),
        Check("business metadata", business_metadata),
        Check("environment + version", env_and_version),
        Check("handoff continued in another process", distributed_handoff),
    ]


def check_module_02(run_tag: str, *, client=None, expected_traces: int | None = 5, environment: str = "staging",
                    version: str = "v1", retries: int = 12, **run_kwargs) -> bool:
    """`run_kwargs`: optional run_checks delay/sleep/log overrides (tests); defaults unchanged."""
    client = client or get_client()
    checks = module_02_checks(run_tag, client, expected_traces=expected_traces, environment=environment,
                              version=version)
    return _check_module(checks, client, retries=retries, **run_kwargs)



# ---------------------------------------------------------------- module 06
# Verified live (server 4.77): with fields="details,subject", scores come back from api.scores.get_many with
# comment, configId and metadata (the details group) and subject={"kind": trace|observation|session, "id",
# "traceId"?} (the subject group). The score-config list includes archived configs, and the server does NOT
# enforce unique config names.
SCORE_FIELDS = "details,subject"
SCORE_CONFIGS = {  # name: (dataType, min, max, category labels)
    "handoff_correct": ("BOOLEAN", None, None, None),
    "user_feedback": ("CATEGORICAL", None, None, {"thumbs_up", "thumbs_down"}),
    "kb_hit": ("NUMERIC", 0.0, 1.0, None),
    "csat": ("NUMERIC", 1.0, 5.0, None),
}
PLANTED_TAG = "wrong_handoff"


def _plain(v):
    """Enum -> its value (ScoreConfigDataType.NUMERIC -> "NUMERIC"); anything else unchanged."""
    return getattr(v, "value", v)


def _row(item) -> dict:
    """A score/config as a plain camelCase dict (API models via .dict(); fakes may already be dicts)."""
    d = item.dict() if hasattr(item, "dict") else dict(item)
    return {k: _plain(v) for k, v in d.items()}


def scores_for(client, *, fields: str = SCORE_FIELDS, **target) -> list[dict]:
    """Scores on one target. trace_id=... also returns the scores on that trace's observations;
    session_id=... returns the session's own scores."""
    resp = client.api.scores.get_many(fields=fields, limit=100, **target)
    return [_row(s) for s in _untruncated(resp, f"scores for {target}")]


def active_configs(client) -> dict[str, list[dict]]:
    """Non-archived score configs grouped by name. Each value is a list, because names can repeat."""
    out, page = {}, 1
    while True:
        resp = client.api.score_configs.get(page=page, limit=100)
        for c in (_row(x) for x in resp.data):
            if not c.get("isArchived"):
                out.setdefault(c["name"], []).append(c)
        total = getattr(getattr(resp, "meta", None), "total_pages", 1) or 1
        if page >= total:
            return out
        page += 1


def _is_false(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("false", "0")
    return value is not None and not bool(value)


def module_06_checks(run_tag: str, client, *, session_id: str, expected_traces: int | None = 6) -> list[Check]:
    """Score checks over every support-request root tagged `run_tag`. They cover:
    - the score configs,
    - trace, observation and session scores,
    - feedback with a correction,
    - the planted wrong_handoff being caught by the learner's code score.
    """

    def fetch():
        traces = fetch_run(client, run_tag, expected_traces=expected_traces)
        return {
            "traces": traces,
            "scores": {root.trace_id: scores_for(client, trace_id=root.trace_id) for root, _ in traces},
            "session": scores_for(client, session_id=session_id),
            "configs": active_configs(client),
        }

    snapshot = RunSnapshot(fetch, fields=FIELDS)

    def case_traces():
        return [(r, items) for r, items in snapshot.get()["traces"] if r.session_id != session_id]

    def named(trace_id, name):
        return [s for s in snapshot.get()["scores"].get(trace_id, []) if s.get("name") == name]

    def planted():
        return [r for r, _ in case_traces() if PLANTED_TAG in (getattr(r, "tags", None) or [])]

    def configs_defined_once():
        configs, problems = snapshot.get()["configs"], []
        for name, (dtype, lo, hi, labels) in SCORE_CONFIGS.items():
            found = configs.get(name, [])
            if not found:
                problems.append(f"{name}: missing")
                continue
            if len(found) > 1:
                problems.append(f"{name}: {len(found)} active configs (get-or-create created duplicates; "
                                "archive the extras in Settings > Scores)")
                continue
            c = found[0]
            if c.get("dataType") != dtype:
                problems.append(f"{name}: dataType {c.get('dataType')} (want {dtype})")
            if dtype == "NUMERIC" and (c.get("minValue"), c.get("maxValue")) != (lo, hi):
                problems.append(f"{name}: range {c.get('minValue')}..{c.get('maxValue')} (want {lo:g}..{hi:g})")
            if labels is not None:
                got = {cat.get("label") if isinstance(cat, dict) else getattr(cat, "label", None)
                       for cat in (c.get("categories") or [])}
                if got != labels:
                    problems.append(f"{name}: categories {sorted(x for x in got if x)} (want {sorted(labels)})")
        if problems:
            return False, "; ".join(problems)
        return True, f"{len(SCORE_CONFIGS)} configs, one each: " + ", ".join(SCORE_CONFIGS)

    def config_id(name):
        found = snapshot.get()["configs"].get(name, [])
        return found[0]["id"] if len(found) == 1 else None

    def code_score_per_case():
        traces, missing = case_traces(), []
        for r, _ in traces:
            if not [s for s in named(r.trace_id, "handoff_correct") if (s.get("subject") or {}).get("kind") == "trace"]:
                missing.append(r.trace_id)
        if missing:
            raise NotYet(f"{len(missing)}/{len(traces)} case trace(s) have no trace-level handoff_correct score yet: "
                         + ", ".join(missing))
        return True, f"all {len(traces)} case trace(s) have a trace-level handoff_correct score"

    def observation_score():
        for r, items in case_traces():
            kb_ids = {o.id for o in items if getattr(o, "name", None) == "kb-search"}
            for s in named(r.trace_id, "kb_hit"):
                subject = s.get("subject") or {}
                if subject.get("kind") == "observation" and subject.get("id") in kb_ids:
                    return True, f"kb_hit={s.get('value')} on kb-search observation {subject['id']} (trace {r.trace_id})"
                if subject.get("kind") != "observation":
                    return False, (f"kb_hit in trace {r.trace_id} is attached to a {subject.get('kind')}, not the "
                                   "kb-search observation (pass observation_id=...)")
        raise NotYet("no kb_hit score on a kb-search observation yet (does observed_search fill RETRIEVALS?)")

    def session_score():
        rows = [s for s in snapshot.get()["session"] if s.get("name") == "csat"]
        if not rows:
            raise NotYet(f"no csat score on session {session_id!r} yet")
        s = rows[0]
        kind, value = (s.get("subject") or {}).get("kind"), s.get("value")
        if kind != "session":
            return False, f"csat is attached to a {kind}, not the session (pass session_id=...)"
        if not isinstance(value, (int, float)) or not 1 <= value <= 5:
            return False, f"csat value {value!r} is not 1..5"
        return True, f"csat={value:g} on session {session_id}"

    def feedback_and_correction():
        roots = planted()
        if not roots:
            return False, f"no case trace tagged {PLANTED_TAG!r} (tag traces with the case's failures)"
        for r in roots:
            fb = named(r.trace_id, "user_feedback")
            corrections = [s for s in snapshot.get()["scores"].get(r.trace_id, []) if s.get("dataType") == "CORRECTION"]
            if not fb or not corrections:
                raise NotYet(f"trace {r.trace_id}: user_feedback={len(fb)}, corrections={len(corrections)} so far")
            if len(fb) > 1:
                return False, (f"trace {r.trace_id} has {len(fb)} user_feedback scores. A changed vote must update "
                               "the same score (deterministic score_id), not add a second one")
            if fb[0].get("value") != "thumbs_down" or not (fb[0].get("comment") or "").strip():
                return False, (f"trace {r.trace_id}: user_feedback={fb[0].get('value')!r} "
                               f"comment={fb[0].get('comment')!r} (want thumbs_down with a comment)")
            if not str(corrections[0].get("value") or "").strip():
                return False, f"trace {r.trace_id}: CORRECTION score is empty"
        return True, f"{len(roots)} planted trace(s): one thumbs_down with comment + a correction"

    def linked_and_provenance():
        problems, n = [], 0
        for r, _ in case_traces():
            for name in ("handoff_correct", "kb_hit", "user_feedback"):
                for s in named(r.trace_id, name):
                    n += 1
                    if s.get("configId") != config_id(name):
                        problems.append(f"{name} in {r.trace_id}: configId {s.get('configId')} (want the {name} config)")
                    if name != "user_feedback" and not (s.get("metadata") or {}).get("evaluator"):
                        problems.append(f"{name} in {r.trace_id}: metadata has no 'evaluator'")
        if problems:
            return False, "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
        if not n:
            raise NotYet("no config-backed scores yet")
        return True, f"{n} score(s) reference their config; code scores carry metadata.evaluator"

    def planted_caught():
        roots = planted()
        if not roots:
            return False, f"no case trace tagged {PLANTED_TAG!r}"
        values = {}
        for r in roots:
            rows = named(r.trace_id, "handoff_correct")
            if not rows:
                raise NotYet(f"planted trace {r.trace_id} has no handoff_correct score yet")
            values[r.trace_id] = rows[0].get("value")
        missed = [t for t, v in values.items() if not _is_false(v)]
        if missed:
            return False, (f"planted wrong_handoff trace(s) scored handoff_correct=True: {', '.join(missed)}. "
                           "Does your score compare the actual handoff with the expected one?")
        planted_ids = {r.trace_id for r in roots}
        clean_false = [r.trace_id for r, _ in case_traces() if r.trace_id not in planted_ids
                       and any(_is_false(s.get("value")) for s in named(r.trace_id, "handoff_correct"))]
        note = (f"; also flagged {len(clean_false)} clean trace(s), an agent error found by your score: "
                + ", ".join(clean_false)) if clean_false else ""
        return True, f"{len(roots)} planted wrong_handoff trace(s) scored handoff_correct=False{note}"

    return [
        Check("score configs defined once", configs_defined_once),
        Check("every case has a trace-level code score", code_score_per_case),
        Check("observation-level score on kb-search", observation_score),
        Check("session-level score", session_score),
        Check("user feedback + correction (one vote per trace)", feedback_and_correction),
        Check("scores linked to configs + provenance", linked_and_provenance),
        Check("planted wrong_handoff caught by handoff_correct", planted_caught),
    ]


def check_module_06(run_tag: str, *, session_id: str, client=None, expected_traces: int | None = 6,
                    retries: int = 12, **run_kwargs) -> bool:
    client = client or get_client()
    checks = module_06_checks(run_tag, client, session_id=session_id, expected_traces=expected_traces)
    return _check_module(checks, client, retries=retries, **run_kwargs)



# ---------------------------------------------------------------- module 03
# Verified live (server 4.77 / SDK 5.2): generations carry the model name the RESPONSE reports
# (gpt-4.1-mini -> "gpt-4.1-mini-2025-04-14", gpt-5.2-chat -> "gpt-chat-latest"; a failed call keeps the deployment
# name). An unmatched generation keeps usage_details but has cost_details={}, total_cost=None, internal_model_id="".
# Cost is computed at ingestion, so definitions only price traces ingested after they exist.
# GPT-5 reasoning tokens: the LangChain handler moves them out of `output` into `output_reasoning` (they need a price).
COST_FIELDS = FIELDS + ",usage,model"
MODEL_DEFS = ("azure-gpt-4.1-mini", "azure-gpt-5.2-chat")  # two deployments; one deployment: pass model_defs=
REQUIRED_PRICES = ("input", "output", "input_cache_read")
SPIKE_TAG = "cost_spike"


def custom_models(client) -> dict[str, list[dict]]:
    """Project-owned (not Langfuse-managed) model definitions grouped by modelName; names can repeat."""
    out, page = {}, 1
    while True:
        resp = client.api.models.list(page=page, limit=100)
        for m in (_row(x) for x in resp.data):
            if not m.get("isLangfuseManaged"):
                out.setdefault(m["modelName"], []).append(m)
        total = getattr(getattr(resp, "meta", None), "total_pages", 1) or 1
        if page >= total:
            return out
        page += 1


def _prices(model: dict) -> dict:
    """{usage type: USD per unit} from a model row (`prices` maps type -> {"price": x})."""
    out = {}
    for k, v in (model.get("prices") or {}).items():
        price = v.get("price") if isinstance(v, dict) else getattr(v, "price", v)
        if price is not None:
            out[k] = float(price)
    return out


def _cost(o) -> float:
    return float(getattr(o, "total_cost", None) or 0.0)


def _close(a, b, rel: float = 0.01) -> bool:
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    return abs(a - b) <= max(rel * max(abs(a), abs(b)), 1e-9)


def _median(values: list[float]) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def module_03_checks(run_tag: str, client, *, session_costs: dict, user_costs: dict, flagged_sessions,
                     expected_traces: int | None = 7, spike_factor: float = 5.0,
                     model_defs=MODEL_DEFS) -> list[Check]:
    """Cost checks over every support-request root tagged `run_tag`. They cover:
    - the learner's custom model definitions `model_defs` (one each, priced per token, cached input priced): one per
      distinct deployment (`pricing.definitions`), so a single-deployment course has one,
    - every generation priced BY those definitions (not unmatched, not a built-in),
    - usage details incl. cached tokens (and reasoning tokens, when a GPT-5 model produced any),
    - the learner's per-session / per-user cost query, against the observations API's own numbers,
    - the planted cost_spike: visibly more expensive, and flagged by the learner's budget check. (Two deployments:
      the expensive model + a padded prompt. One deployment: a bigger padded prompt that defeats the prompt cache.)
    """
    model_defs = tuple(model_defs)

    def fetch():
        return {"traces": fetch_run(client, run_tag, expected_traces=expected_traces, fields=COST_FIELDS),
                "models": custom_models(client)}

    snapshot = RunSnapshot(fetch, fields=COST_FIELDS)

    def traces():
        return snapshot.get()["traces"]

    def generations():
        # A failed LLM call (level ERROR) has no tokens and no cost by design; it is not a pricing problem.
        gens = [(r, o) for r, items in traces() for o in items if o.type == "GENERATION"
                and str(getattr(getattr(o, "level", None), "value", getattr(o, "level", None))) != "ERROR"]
        if not gens:
            raise NotYet("no GENERATION observations in the run yet")
        return gens

    def trace_cost(items) -> float:
        return sum(_cost(o) for o in items)

    def truth(attr) -> dict:
        out = {}
        for r, items in traces():
            key = getattr(r, attr, None)
            out[key] = out.get(key, 0.0) + trace_cost(items)
        return out

    def definitions_once():
        models, problems = snapshot.get()["models"], []
        for name in model_defs:
            found = models.get(name, [])
            if not found:
                problems.append(f"{name}: missing")
                continue
            if len(found) > 1:
                problems.append(f"{name}: {len(found)} definitions (get-or-create created duplicates; delete the "
                                "extras in Settings > Models)")
                continue
            m, prices = found[0], _prices(found[0])
            if m.get("unit") != "TOKENS":
                problems.append(f"{name}: unit {m.get('unit')} (want TOKENS)")
            missing = [k for k in REQUIRED_PRICES if not prices.get(k)]
            if missing:
                problems.append(f"{name}: no price for {', '.join(missing)}")
            elif prices["input"] > 1e-3:
                problems.append(f"{name}: input price {prices['input']:g} USD per token. Prices are per TOKEN, "
                                "not per 1M tokens (use cost.per_token)")
        if problems:
            return False, "; ".join(problems)
        return True, ", ".join(f"{n} ~ {models[n][0].get('matchPattern')}" for n in model_defs)

    def priced_by_yours():
        mine = {m["id"]: name for name in model_defs for m in snapshot.get()["models"].get(name, [])}
        gens, problems = generations(), {}
        for _, g in gens:
            model = getattr(g, "model", None)
            if not _cost(g):
                problems.setdefault(model, f"'{model}' has no cost: no model definition matches it. Make one of "
                                           "your match_patterns match the name the generation REPORTS")
            elif getattr(g, "internal_model_id", None) not in mine:
                problems.setdefault(model, f"'{model}' was priced by a built-in definition, not yours: your "
                                           "match_pattern does not match it")
        if problems:
            return False, f"{len(problems)} model name(s) not priced by your definitions: " + "; ".join(problems.values())
        used = sorted({mine[g.internal_model_id] for _, g in gens})
        return True, f"all {len(gens)} generation(s) priced by your definitions ({', '.join(used)})"

    def usage_captured():
        gens = generations()
        cached, reasoning, problems = 0, 0, []
        for r, g in gens:
            usage, cost = getattr(g, "usage_details", None) or {}, getattr(g, "cost_details", None) or {}
            if not (usage.get("input") or usage.get("input_cache_read")) or not usage.get("output"):
                problems.append(f"generation {g.id} in {r.trace_id} has no input/output tokens: {usage}")
            if usage.get("input_cache_read"):
                cached += usage["input_cache_read"]
                if not cost.get("input_cache_read"):
                    problems.append(f"generation {g.id} read {usage['input_cache_read']} cached tokens that cost 0: "
                                    "add an input_cache_read price to its definition")
            if usage.get("output_reasoning"):
                reasoning += usage["output_reasoning"]
                if not cost.get("output_reasoning"):
                    problems.append(f"generation {g.id} produced {usage['output_reasoning']} reasoning tokens that "
                                    "cost 0: add an output_reasoning price (the output price) to its definition")
        if problems:
            return False, "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
        note = (f"{cached} cached input token(s), priced" if cached
                else "no cached tokens in this run (Azure caches prompts >= 1024 tokens)")
        if reasoning:
            note += f"; {reasoning} reasoning token(s), priced"
        return True, f"{len(gens)} generation(s) with input/output usage; {note}"

    def matches(label: str, theirs: dict, attr: str):
        def fn():
            want, got = truth(attr), dict(theirs or {})
            missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
            if missing or extra:
                return False, (f"your {label} costs: missing={missing} extra={extra}. Query every {label} of this "
                               "run, scoped by the run tag")
            wrong = [f"{k}: yours {got[k]!r}, actual {want[k]:.6f}" for k in want if not _close(got[k], want[k])]
            if wrong:
                return False, (f"{len(wrong)} {label} cost(s) off by >1%: " + "; ".join(wrong[:3])
                               + " (filter on the run tag AND the id, sum totalCost, treat None as 0. If yours is "
                               "LOWER, you queried before ingestion finished: re-run your cost cell)")
            return True, f"{len(want)} {label}(s) match: " + ", ".join(f"{k}=${v:.4f}" for k, v in want.items())
        return fn

    def spike_caught():
        per_trace = [(r, trace_cost(items)) for r, items in traces()]
        spikes = [(r, c) for r, c in per_trace if SPIKE_TAG in (getattr(r, "tags", None) or [])]
        clean = [c for r, c in per_trace if SPIKE_TAG not in (getattr(r, "tags", None) or [])]
        if not spikes:
            return False, f"no trace tagged {SPIKE_TAG!r} (run the planted v2 conversation with its tags)"
        base = _median(clean)
        weak = [f"{r.trace_id} ${c:.5f}" for r, c in spikes if c <= 0 or c < spike_factor * base]
        if weak:
            return False, (f"{SPIKE_TAG} trace(s) not >= {spike_factor:g}x the clean median ${base:.5f}: "
                           + ", ".join(weak) + " (is the model the spike runs on priced? With one deployment the "
                           "spike is its padded prompt: are its generations' input tokens uncached?)")
        spike_sessions = {r.session_id for r, _ in spikes}
        flagged = set(flagged_sessions or ())
        if flagged != spike_sessions:
            return False, (f"your budget check flagged {sorted(flagged)}, want exactly {sorted(spike_sessions)} "
                           f"(missed {sorted(spike_sessions - flagged)}, false alarms {sorted(flagged - spike_sessions)})")
        ratio = min(c for _, c in spikes) / base if base else float("inf")
        return True, (f"{len(spikes)} {SPIKE_TAG} trace(s), each >= {ratio:.0f}x the clean median ${base:.5f}; "
                      f"your check flagged {sorted(flagged)}")

    return [
        Check("model definitions defined once", definitions_once),
        Check("every generation priced by your definitions", priced_by_yours),
        Check("usage details captured (incl. cached tokens)", usage_captured),
        Check("cost per session (your query)", matches("session", session_costs, "session_id")),
        Check("cost per user (your query)", matches("user", user_costs, "user_id")),
        Check("planted cost_spike caught", spike_caught),
    ]


def check_module_03(run_tag: str, *, session_costs: dict, user_costs: dict, flagged_sessions, client=None,
                    expected_traces: int | None = 7, retries: int = 12, model_defs=MODEL_DEFS, **run_kwargs) -> bool:
    """`model_defs`: the custom definitions' names, one per distinct deployment
    (`[d["model_name"] for d in pricing.definitions(settings)]`); the default is the two-deployment pair."""
    client = client or get_client()
    checks = module_03_checks(run_tag, client, session_costs=session_costs, user_costs=user_costs,
                              flagged_sessions=flagged_sessions, expected_traces=expected_traces,
                              model_defs=model_defs)
    return _check_module(checks, client, retries=retries, **run_kwargs)



# ---------------------------------------------------------------- module 04
# Verified live (server 4.77 / SDK 5.2): the `prompt` field group gives promptId/promptName/promptVersion. Every
# GENERATION linked via propagate_attributes(prompt=...) or LangChain metadata {"langfuse_prompt": p} carries them;
# non-generations have promptName="" and promptVersion=None. `model` gives modelParameters (bound kwargs, e.g.
# {"temperature": 0, "max_tokens": 400}). create_prompt never dedupes: identical content makes a new version.
# Labels are unique per prompt and Langfuse keeps no label history.
PROMPT_FIELDS = FIELDS + ",prompt,model"
PROMPT_PARAMS = ("temperature", "max_tokens")
HANDOFF_TOOL = "create_handoff"
LOGIN_TEAM = "account_security"


def prompt_versions(client, name: str) -> dict[int, dict]:
    """{version: prompt row (camelCase: prompt, config, labels, type, commitMessage, ...)} for every version of `name`.
    Empty dict if the prompt does not exist."""
    meta = [m for m in client.api.prompts.list(name=name, limit=100).data if getattr(m, "name", None) == name]
    versions = sorted({v for m in meta for v in (getattr(m, "versions", None) or [])})
    return {v: _row(client.api.prompts.get(name, version=v)) for v in versions}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _same_config(have, want: dict) -> bool:
    have = have or {}
    return all(k in have and _num(have[k]) == _num(want[k]) for k in want)


def _handoffs(items) -> list[str]:
    """Targets of the successful-or-not create_handoff TOOL calls in one trace (input is a JSON string)."""
    out = []
    for o in items:
        if getattr(o, "type", None) == "TOOL" and getattr(o, "name", None) == HANDOFF_TOOL:
            args = _decode(getattr(o, "input", None))
            if isinstance(args, dict) and args.get("target"):
                out.append(args["target"])
    return out


def module_04_checks(run_tag: str, client, *, prompt_name: str | None = None,
                     expected_traces: int | None = 6, agent_deployment: str | None = None) -> list[Check]:
    """Prompt-management checks over every support-request root tagged `run_tag` (phases via `phase:*` tags):
    - the managed prompt: good + bad versions, created once each (get-or-create by content),
    - labels after the exercise: production = good (rolled back), staging = bad (kept for module 09),
    - every generation linked to a prompt version, with that version's config applied,
    - the planted bad version: its traffic is attributable to it and misroutes; post-rollback traffic is good.
    `agent_deployment` (settings.agent_deployment): "config applied" compares the generation's modelParameters with
    what the agent SENDS that deployment for the config (`config.sent_params`): on a GPT-5 / o-series deployment
    temperature is never sent and max_tokens goes out as max_completion_tokens. None: the config as stored.
    """
    from .config import sent_params
    from .prompts import PROMPT_CONFIG, PROMPT_NAME, SYSTEM_PROMPT_TEMPLATE, WRONG_HANDOFF_TEMPLATE

    name = prompt_name or PROMPT_NAME

    def sent(cfg: dict) -> dict:
        return sent_params(cfg, agent_deployment) if agent_deployment else dict(cfg)

    def fetch():
        return {"traces": fetch_run(client, run_tag, expected_traces=expected_traces, fields=PROMPT_FIELDS),
                "versions": prompt_versions(client, name)}

    snapshot = RunSnapshot(fetch, fields=PROMPT_FIELDS)

    def versions() -> dict[int, dict]:
        found = snapshot.get()["versions"]
        if not found:
            raise AssertionError(f"no prompt named {name!r} in this project yet (run your ensure_version cell)")
        return found

    def same_text(template) -> list[int]:
        return [v for v, row in versions().items() if (row.get("prompt") or "").strip() == template.strip()]

    def matching(template) -> list[int]:
        rows = versions()
        return [v for v in same_text(template) if _same_config(rows[v].get("config"), PROMPT_CONFIG)]

    def good_bad() -> tuple[int | None, int | None]:
        good, bad = matching(SYSTEM_PROMPT_TEMPLATE), matching(WRONG_HANDOFF_TEMPLATE)
        return (good[0] if good else None), (bad[0] if bad else None)

    def by_phase(phase: str):
        return [(r, items) for r, items in snapshot.get()["traces"]
                if f"phase:{phase}" in (getattr(r, "tags", None) or [])]

    def gens(items):
        return [o for o in items if getattr(o, "type", None) == "GENERATION"
                and str(_plain(getattr(o, "level", None))) != "ERROR"]

    def linked_versions(items) -> set:
        return {getattr(o, "prompt_version", None) if getattr(o, "prompt_name", None) == name else None
                for o in gens(items)}

    def fmt(vs) -> str:
        return ", ".join("unlinked" if v is None else f"v{v}" for v in sorted(vs, key=lambda x: (x is None, x or 0)))

    def versions_once():
        rows, problems = versions(), []
        for label, template in (("good (SYSTEM_PROMPT_TEMPLATE)", SYSTEM_PROMPT_TEMPLATE),
                                ("bad (WRONG_HANDOFF_TEMPLATE)", WRONG_HANDOFF_TEMPLATE)):
            texts, same = same_text(template), matching(template)
            if not texts:
                problems.append(f"no version with the {label} text")
            elif not same:
                problems.append(f"{label}: version(s) {texts} lack config {PROMPT_CONFIG} "
                                f"(got {rows[texts[0]].get('config')})")
            elif len(same) > 1:
                problems.append(f"{label}: versions {same} are identical. create_prompt never dedupes, so "
                                "get-or-create must compare text + config before creating")
        not_text = [v for v, row in rows.items() if row.get("type") not in (None, "text")]
        if not_text:
            problems.append(f"version(s) {not_text} are not text prompts")
        if problems:
            return False, "; ".join(problems)
        good, bad = good_bad()
        return True, f"{name!r}: {len(rows)} version(s); good = v{good}, bad = v{bad}, each created once"

    def labels_final():
        rows = versions()
        good, bad = good_bad()
        holder = {lab: v for v, row in rows.items() for lab in (row.get("labels") or [])}
        problems = []
        if holder.get("production") is None:
            problems.append("no version has the production label")
        elif holder["production"] != good:
            problems.append(f"production is on v{holder['production']}, not the good v{good}: roll back by moving "
                            "the production label to the good version")
        if holder.get("staging") != bad:
            where = f"v{holder['staging']}" if holder.get("staging") else "no version"
            problems.append(f"staging is on {where}, want the bad candidate v{bad} "
                            "(module 09 compares production vs staging)")
        if problems:
            return False, "; ".join(problems)
        return True, f"production -> v{good} (good), staging -> v{bad} (bad candidate), latest -> v{holder.get('latest')}"

    def every_generation_linked():
        traces, unlinked, n = snapshot.get()["traces"], [], 0
        for r, items in traces:
            for o in gens(items):
                n += 1
                if getattr(o, "prompt_name", None) != name or not isinstance(getattr(o, "prompt_version", None), int):
                    unlinked.append(f"{r.trace_id}/{o.id} (promptName={getattr(o, 'prompt_name', None)!r})")
        if not n:
            raise NotYet("no GENERATION observations in the run yet")
        if unlinked:
            return False, (f"{len(unlinked)}/{n} generation(s) not linked to {name!r}: " + ", ".join(unlinked[:3])
                           + ". Pass the served prompt via propagate_attributes(prompt=...) or LangChain metadata "
                           "{'langfuse_prompt': prompt} (a fallback prompt is never linked: was the fetch failing?)")
        return True, f"all {n} generation(s) in {len(traces)} trace(s) linked to {name!r}"

    def config_applied():
        rows, problems, n = versions(), [], 0
        for r, items in snapshot.get()["traces"]:
            for o in gens(items):
                cfg = (rows.get(getattr(o, "prompt_version", None)) or {}).get("config") or {}
                want = sent({k: cfg[k] for k in PROMPT_PARAMS if k in cfg})
                if not want:
                    continue
                n += 1
                got = getattr(o, "model_parameters", None) or {}
                off = {k: got.get(k) for k in want if _num(got.get(k)) != _num(want[k])}
                if off:
                    problems.append(f"{o.id} in {r.trace_id}: modelParameters {off}, prompt config says {want}")
        if problems:
            return False, (f"{len(problems)} generation(s) ignore their prompt version's config: "
                           + "; ".join(problems[:2])
                           + ". Pass prompting.llm_params(prompt.config) as llm_params= to agent.invoke")
        if not n:
            return False, "no linked generation's prompt version has a temperature/max_tokens config"
        stored = {k: PROMPT_CONFIG[k] for k in PROMPT_PARAMS}
        shown = sent(stored)
        note = "" if shown == stored else f" (sent to {agent_deployment} as {shown}: it takes no temperature)"
        return True, f"{n} generation(s) ran with their version's config " + str(stored) + note

    def bad_version_caught():
        good, bad = good_bad()
        if bad is None:
            return False, "no bad version (WRONG_HANDOFF_TEMPLATE + config) in the prompt: create it with ensure_version"
        bad_traces, healthy = by_phase("bad"), by_phase("healthy")
        if not bad_traces:
            return False, "no trace tagged phase:bad (tag each turn with case.tags)"
        wrong_link = [f"{r.trace_id} -> {fmt(linked_versions(items))}" for r, items in bad_traces
                      if linked_versions(items) != {bad}]
        if wrong_link:
            return False, (f"phase:bad trace(s) not served by the bad v{bad}: " + ", ".join(wrong_link)
                           + ". Promote it to production and wait until it is served before the bad-phase traffic")
        early = [f"{r.trace_id} -> {fmt(linked_versions(items))}" for r, items in healthy
                 if linked_versions(items) != {good}]
        if early:
            return False, f"phase:healthy trace(s) not served by the good v{good}: " + ", ".join(early)
        routes = {r.trace_id: _handoffs(items) for r, items in bad_traces}
        harmed = [t for t, targets in routes.items() if any(x != LOGIN_TEAM for x in targets)]
        if not harmed:
            return False, (f"bad v{bad} served {len(bad_traces)} login request(s) but none was misrouted "
                           f"(handoffs: {routes}); re-run the bad phase")
        return True, (f"{len(bad_traces)} phase:bad trace(s) all linked to bad v{bad}; {len(harmed)} misrouted: "
                      + ", ".join(f"{t} -> {routes[t]}" for t in harmed))

    def rollback_restored():
        good, _ = good_bad()
        if good is None:
            return False, "no good version (SYSTEM_PROMPT_TEMPLATE + config) in the prompt: create it with ensure_version"
        traces = by_phase("rollback")
        if not traces:
            return False, "no trace tagged phase:rollback"
        problems = []
        for r, items in traces:
            linked, targets = linked_versions(items), _handoffs(items)
            if linked != {good}:
                problems.append(f"{r.trace_id} linked to {fmt(linked)}, not the good v{good}")
            elif not targets or any(x != LOGIN_TEAM for x in targets):
                problems.append(f"{r.trace_id} handed off to {targets or 'nobody'} (want ['{LOGIN_TEAM}'])")
        if problems:
            return False, ("after the rollback: " + "; ".join(problems)
                           + ". Move production back to the good version and wait until it is served")
        return True, f"{len(traces)} phase:rollback trace(s) linked to good v{good}, all handed off to {LOGIN_TEAM}"

    return [
        Check("prompt versions created once (good + bad)", versions_once),
        Check("labels: production = good, staging = bad", labels_final),
        Check("every generation linked to a prompt version", every_generation_linked),
        Check("prompt config applied to the LLM calls", config_applied),
        Check("planted bad version caught by its generation links", bad_version_caught),
        Check("rollback: new traffic served by the good version", rollback_restored),
    ]


def check_module_04(run_tag: str, *, client=None, prompt_name: str | None = None, expected_traces: int | None = 6,
                    retries: int = 12, agent_deployment: str | None = None, **run_kwargs) -> bool:
    client = client or get_client()
    checks = module_04_checks(run_tag, client, prompt_name=prompt_name, expected_traces=expected_traces,
                              agent_deployment=agent_deployment)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 05
# Verified live (server 4.77 / SDK 5.2): the client `mask` callback covers input/output/metadata of every observation
# the SDK writes (incl. the CallbackHandler's generations and tools), but NOT propagate_attributes values (metadata,
# tags, ids), which reach every observation unmasked. Reads truncate each metadata value to 200 chars unless
# `expand_metadata` names the key, so the leak scan fetches twice. Sampling is OpenTelemetry's TraceIdRatioBased on
# the trace id: whole traces are kept or dropped. LangfuseMedia is replaced by "@@@langfuseMedia:type=…|id=…|source=…@@@"
# before masking; api.media.get(id) gives a presigned MinIO url.
ALL_FIELDS = "core,basic,time,io,metadata,model,usage,prompt,metrics,trace_context"
PLACEHOLDERS = {"card": "[REDACTED_CARD]", "email": "[REDACTED_EMAIL]", "phone": "[REDACTED_PHONE]"}
PII_TAG = "pii"
PII_LEAD = "My card number is"
PLANTED_CARD = "4111111111111111"  # traffic.PII_SUFFIX without separators
UPLOAD_TAG = "damaged_item_photo"
KEEP_IN_ROOT_INPUT = {"double_charge": "8129", "s_refund_followup": "8129", "password_reset": "cust_02",
                      UPLOAD_TAG: "8115"}  # ids a correct masker leaves alone (first turns only, see ids_survive)
PHONE_DIGITS = "4155550132"  # traffic.UPLOAD_DEMO's call-back number without separators
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)")
DIGIT_RUN = re.compile(r"(?<![A-Za-z0-9_])\d(?:[ -]?\d)*(?![A-Za-z0-9_])")  # not inside a hex id/uuid token
MEDIA_REF = re.compile(r"@@@langfuseMedia:type=([^|@]+)\|id=([^|@]+)\|source=([^|@]+)@@@")
SAMPLE_SIGMAS = 4.0


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def card_numbers(text: str) -> list[str]:
    """Luhn-valid 13-19 digit numbers in `text` (single space/dash separators), checked on every separator-aligned
    window of a digit run, plus the planted test card in any formatting."""
    text = re.sub(r"\\[nrt]", " ", text)  # JSON escapes: a "\n" before a card must not glue it to an "n"
    found = []
    for run in DIGIT_RUN.finditer(text):
        groups = re.split(r"[ -]", run.group())
        for i in range(len(groups)):
            for j in range(i + 1, len(groups) + 1):
                digits = "".join(groups[i:j])
                if 13 <= len(digits) <= 19 and _luhn(digits):
                    found.append(digits)
    if PLANTED_CARD in re.sub(r"[\s./\\-]", "", text) and PLANTED_CARD not in found:
        found.append(PLANTED_CARD)
    return found


def observation_texts(o) -> dict[str, str]:
    """Every field of an observation as text ({field: text}), so a scan misses nothing the API returns."""
    d = o.dict() if hasattr(o, "dict") else dict(vars(o))
    out = {}
    for key, value in d.items():
        if value is None:
            continue
        out[key] = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return out


def full_trace_observations(client, trace_id: str) -> list:
    """All observations of a trace with every field group and NO metadata truncation (reads cut each metadata value at
    200 chars unless expand_metadata names its key, so this fetches the keys first, then the full values)."""
    first = _untruncated(client.api.observations.get_many(trace_id=trace_id, fields=ALL_FIELDS, limit=200),
                         f"observations of trace {trace_id}")
    keys = sorted({k for o in first if isinstance(getattr(o, "metadata", None), dict) for k in o.metadata})
    if not keys:
        return first
    resp = client.api.observations.get_many(trace_id=trace_id, fields=ALL_FIELDS, limit=200,
                                            expand_metadata=",".join(keys))
    return _untruncated(resp, f"observations of trace {trace_id}")


def _http_get(url: str) -> bytes:
    import httpx

    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def module_05_checks(run_tag: str, client, *, sampling_tag: str, sampled_ids, predicted_ids, sample_rate: float,
                     attachment_sha256: dict, spans_per_sampled_trace: int = 3, expected_traces: int | None = 5,
                     placeholders: dict | None = None, download=None) -> list[Check]:
    """Data-protection checks over every support-request root tagged `run_tag` (4 mask turns + 1 upload turn), the
    sampled batch tagged `sampling_tag`, and the uploaded media. One fetch per attempt."""
    from opentelemetry.sdk.trace.sampling import Decision, TraceIdRatioBased

    ph = {**PLACEHOLDERS, **(placeholders or {})}
    download = download or _http_get

    def fetch():
        roots = _turn_roots(root_observations(client, run_tag))
        if not roots:
            raise NotYet(f"no root observation tagged {run_tag!r} in the last {WINDOW_HOURS}h yet")
        if expected_traces is not None and len(roots) < expected_traces:
            raise NotYet(f"{len(roots)}/{expected_traces} traces tagged {run_tag!r} ingested so far")
        traces = [(r, full_trace_observations(client, r.trace_id)) for r in roots]
        for r, items in traces:
            types = {getattr(o, "type", None) for o in items}
            if not {"GENERATION", "GUARDRAIL"} <= types or not getattr(r, "output", None):
                raise NotYet(f"trace {r.trace_id} is still being ingested ({len(items)} observations so far)")
        flt = [{"type": "arrayOptions", "column": "traceTags", "operator": "any of", "value": [sampling_tag]}]
        sampled = _untruncated(client.api.observations.get_many(fields="core,basic", filter=json.dumps(flt),
                                                                 limit=1000),
                               f"observations tagged {sampling_tag!r}")
        return {"traces": traces, "sampled": sampled}

    snapshot = RunSnapshot(fetch, fields=ALL_FIELDS)

    def tagged(tag):
        return [(r, items) for r, items in snapshot.get()["traces"] if tag in (getattr(r, "tags", None) or [])]

    def texts(items):
        return [(o, field, text) for o in items for field, text in observation_texts(o).items()]

    def root_input(r) -> str:
        return observation_texts(r).get("input") or ""

    def where(o, field):
        return f"{getattr(o, 'type', '?')} '{getattr(o, 'name', '?')}' {field}"

    def pii_traces():
        found = tagged(PII_TAG)
        if not found:
            raise AssertionError(f"no trace tagged {PII_TAG!r} (tag each turn with spec.tags)")
        return found

    def no_card_anywhere():
        traces, leaks, scanned = pii_traces(), [], 0
        for r, items in traces:
            for o, field, text in texts(items):
                scanned += 1
                cards = card_numbers(text)
                if cards:
                    leaks.append(f"{r.trace_id}: {where(o, field)} has {cards[0][:4]}…{cards[0][-4:]}")
        if leaks:
            return False, (f"{len(leaks)} field(s) still carry a card number: " + "; ".join(leaks[:4])
                           + (f" (+{len(leaks) - 4} more)" if len(leaks) > 4 else "")
                           + ". Is the client built with mask=… (check langfuse._mask), and does mask_pii handle that "
                             "shape (tuples, dataclasses, LangChain message objects)? propagate_attributes values are "
                             "never masked: keep free text out of them")
        return True, f"{len(traces)} pii trace(s), {scanned} fields scanned (metadata fully expanded): no card number"

    def placeholder_where_card_was():
        problems, roots_with = [], 0
        lead = re.compile(re.escape(PII_LEAD) + r"\s*(\S{0,40})")
        for r, items in pii_traces():
            in_gen = False
            for o, field, text in texts(items):
                for m in lead.finditer(text):
                    if not m.group(1).startswith(ph["card"]):
                        problems.append(f"{r.trace_id}: {where(o, field)} has '{PII_LEAD} {m.group(1)[:12]}…'")
                    elif getattr(o, "type", None) == "GENERATION" and field == "input":
                        in_gen = True
            if not in_gen:
                problems.append(f"{r.trace_id}: no GENERATION input with '{PII_LEAD} {ph['card']}' (the LLM messages "
                                "were not masked, or the card message is missing from the turn)")
            if f"{PII_LEAD} {ph['card']}" in root_input(r):
                roots_with += 1
        if not roots_with:
            problems.append(f"no pii root input contains '{PII_LEAD} {ph['card']}'")
        if problems:
            return False, ("; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
                           + f". mask_pii must replace the card with exactly {ph['card']!r}")
        return True, (f"'{PII_LEAD} {ph['card']}' in {roots_with} root input(s) and in the LLM messages of every pii "
                      "trace: masking ran (the data was not merely dropped)")

    def guardrail_saw_raw():
        first_turns, problems = 0, []
        for r, items in pii_traces():
            if PII_LEAD not in root_input(r):
                continue  # a follow-up turn: the card is only in the history; the guardrail checks the new message
            first_turns += 1
            guards = [o for o in items if getattr(o, "type", None) == "GUARDRAIL"]
            if not any("pii_card_number" in (observation_texts(g).get("output") or "") for g in guards):
                problems.append(f"{r.trace_id}: GUARDRAIL output has no pii_card_number flag")
            elif not any(ph["card"] in (observation_texts(g).get("input") or "") for g in guards):
                problems.append(f"{r.trace_id}: GUARDRAIL input does not show {ph['card']}")
        if not first_turns:
            return False, f"no pii trace whose root input carries '{PII_LEAD} …'"
        if problems:
            return False, "; ".join(problems)
        return True, (f"{first_turns} pii turn(s): the guardrail flagged pii_card_number on the raw text in-process, "
                      f"while its stored input shows {ph['card']}")

    def emails_phones_masked():
        leaks, traces = [], snapshot.get()["traces"]
        for r, items in traces:
            for o, field, text in texts(items):
                email = EMAIL_RE.search(text)
                if email:
                    leaks.append(f"{r.trace_id}: {where(o, field)} has an email ({email.group()[:3]}…)")
                if PHONE_RE.search(text) or PHONE_DIGITS in re.sub(r"\D", "", text):
                    leaks.append(f"{r.trace_id}: {where(o, field)} has a phone number")
        if leaks:
            return False, (f"{len(leaks)} leak(s): " + "; ".join(leaks[:4])
                           + ". Mask emails and phone numbers too (tool OUTPUTS included)")
        ups = tagged(UPLOAD_TAG)
        if not ups:
            return False, f"no trace tagged {UPLOAD_TAG!r} (run upload_turn with the upload spec's tags)"
        missing = [ph[k] for k in ("email", "phone") if ph[k] not in root_input(ups[0][0])]
        if missing:
            return False, f"the {UPLOAD_TAG} root input lacks {missing} (masking did not run on it?)"
        return True, (f"no email address or phone number in {len(traces)} trace(s); placeholders in the "
                      f"{UPLOAD_TAG} root input")

    def ids_survive():
        problems, checked = [], 0
        for intent, keep in KEEP_IN_ROOT_INPUT.items():
            for r, items in tagged(intent):
                text = root_input(r)
                if intent == "s_refund_followup" and PII_LEAD not in text and ph["card"] not in text:
                    continue  # the follow-up turn ("How long until I see the refund?") names no id
                checked += 1
                if keep not in text:
                    problems.append(f"{intent} root input lost {keep!r}: {text[:90]}")
                for o in items:
                    if getattr(o, "type", None) == "TOOL" and getattr(o, "name", None) == "lookup_order":
                        args = _decode(getattr(o, "input", None))
                        oid = args.get("order_id") if isinstance(args, dict) else None
                        if not (isinstance(oid, str) and oid.isdigit()):
                            problems.append(f"{r.trace_id}: lookup_order input order_id={oid!r}")
        if not checked:
            raise NotYet("no run traces with the demo intents yet")
        if problems:
            return False, ("over-masking: " + "; ".join(problems[:3])
                           + ". Only Luhn-valid 13-19 digit numbers are cards; short ids must survive")
        return True, f"order/customer ids intact in {checked} root input(s) and every lookup_order input"

    def attachment_retrievable():
        ups = tagged(UPLOAD_TAG)
        if not ups:
            return False, f"no trace tagged {UPLOAD_TAG!r} (run upload_turn)"
        root, items = ups[0]
        refs = {m.group(0): (m.group(1), m.group(2)) for _, _, t in texts(items) for m in MEDIA_REF.finditer(t)}
        if not refs:
            hint = ("your mask turned the LangfuseMedia objects into text: return them unchanged"
                    if "langfuse.media.LangfuseMedia object" in root_input(root)
                    else "put LangfuseMedia(content_bytes=…, content_type=…) objects in the root's input")
            return False, f"trace {root.trace_id} references no media: {hint}"
        got = {}
        for content_type, media_id in refs.values():
            media = client.api.media.get(media_id)
            data = download(media.url)
            if getattr(media, "content_length", None) not in (None, len(data)):
                return False, f"media {media_id}: contentLength {media.content_length} != {len(data)} bytes downloaded"
            got.setdefault(content_type, set()).add(hashlib.sha256(data).hexdigest())
        want = dict(attachment_sha256 or {})
        if not want:
            return False, "pass attachment_sha256={content_type: sha256 of the bytes you uploaded}"
        wrong = [ct for ct, sha in want.items() if sha not in got.get(ct, set())]
        if wrong:
            return False, (f"no stored media of type(s) {wrong} with the uploaded bytes (stored types: "
                           f"{sorted(got)})")
        return True, (f"{len(refs)} attachment(s) in {root.trace_id} ({', '.join(sorted(got))}) stored in the media "
                      "bucket and downloaded byte-identical")

    def sampling_per_trace():
        ids, rate = list(sampled_ids or []), float(sample_rate)
        if not 0.0 < rate < 1.0:
            return False, f"sample_rate={sample_rate}: use a rate strictly between 0 and 1"
        if not ids:
            return False, "pass sampled_ids= (the trace ids your sampling worker emitted)"
        sampler = TraceIdRatioBased(rate)
        truth = {t for t in ids if sampler.should_sample(None, int(t, 16), "x").decision == Decision.RECORD_AND_SAMPLE}
        arrived = Counter(o.trace_id for o in snapshot.get()["sampled"])
        stray = sorted(set(arrived) - set(ids))
        if stray:
            return False, f"{len(stray)} trace(s) tagged {sampling_tag!r} that the worker did not report: {stray[:3]}"
        kept_wrongly = sorted(set(arrived) - truth)
        if kept_wrongly:
            return False, (f"{len(kept_wrongly)} trace(s) arrived that a {rate} sampler drops ({kept_wrongly[:2]}): "
                           "was the worker's client really built with sample_rate, before anything else?")
        if truth - set(arrived):
            raise NotYet(f"{len(arrived)}/{len(truth)} kept sampled traces ingested so far")
        partial = {t: c for t, c in arrived.items() if c != spans_per_sampled_trace}
        if partial:
            raise NotYet(f"sampled trace(s) with != {spans_per_sampled_trace} observations: "
                         f"{dict(list(partial.items())[:3])}")
        n, k, mean = len(ids), len(truth), len(ids) * rate
        band = SAMPLE_SIGMAS * math.sqrt(n * rate * (1 - rate)) + 1
        if abs(k - mean) > band:
            return False, f"kept {k}/{n}, expected about {mean:.1f} ± {band:.1f}"
        mine = set(predicted_ids or [])
        if mine != truth:
            return False, (f"your predict_kept is off: {len(mine - truth)} predicted but dropped, {len(truth - mine)} "
                           "kept but not predicted. Keep iff int(trace_id, 16) & (2**64 - 1) < round(rate * 2**64)")
        return True, (f"{k}/{n} traces kept at rate {rate} (expected ≈{mean:.1f} ± {band:.1f}), each complete with "
                      f"{spans_per_sampled_trace} observations; exactly your trace-id prediction")

    return [
        Check("planted pii caught: no card number in any field", no_card_anywhere),
        Check("mask ran: placeholder where the card was", placeholder_where_card_was),
        Check("guardrail saw the raw card, storage did not", guardrail_saw_raw),
        Check("emails and phone numbers masked", emails_phones_masked),
        Check("ids not over-masked", ids_survive),
        Check("attachment stored and retrievable (media API)", attachment_retrievable),
        Check("sampling keeps or drops whole traces, by trace id", sampling_per_trace),
    ]


def check_module_05(run_tag: str, *, sampling_tag: str, sampled_ids, predicted_ids, sample_rate: float,
                    attachment_sha256: dict, client=None, expected_traces: int | None = 5, retries: int = 12,
                    spans_per_sampled_trace: int = 3, placeholders: dict | None = None, download=None,
                    **run_kwargs) -> bool:
    """`run_kwargs`: optional run_checks delay/sleep/log overrides (tests); defaults unchanged."""
    client = client or get_client()
    checks = module_05_checks(run_tag, client, sampling_tag=sampling_tag, sampled_ids=sampled_ids,
                              predicted_ids=predicted_ids, sample_rate=sample_rate,
                              attachment_sha256=attachment_sha256, expected_traces=expected_traces,
                              spans_per_sampled_trace=spans_per_sampled_trace, placeholders=placeholders,
                              download=download)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 07
# Verified live (server 4.77 / SDK 5.2), see docs/plans/2026-11-12-langfuse-module-07.md:
# - LLM-as-a-judge scores: name = evaluator name, source="EVAL", configId=None, comment = the judge's reasoning,
#   subject={"kind": "observation", "id": <matched observation>}, metadata.evaluator_id / evaluation_rule_id / ...
# - rules target OBSERVATIONS; a `traceName` filter never matches unless trace_name is propagated explicitly, so the
#   rule filters on the root observation's `name`. Rules are not retroactive.
# - code evaluators: create answers 412 "Code evaluations are not enabled for this deployment" unless the worker has
#   LANGFUSE_CODE_EVAL_DISPATCHER set.
LIVE_ROOT_NAME = "support-request"
LIVE_NAMES = {  # role -> base name; the notebook prefixes every project-global name with PREFIX
    "connection": "azure-openai",
    "routing": "routing_judge",
    "helpfulness": "helpfulness_judge",
    "rule": "support-live-eval",
    "code": "handoff_code",
    "code_score": "handoff_correct",
}
CODE_EVAL_HINT = ("code evaluators are not enabled on this deployment (create answers 412). The worker needs "
                  "LANGFUSE_CODE_EVAL_DISPATCHER=insecure-local (dev only: TypeScript, runs in the worker process) or "
                  "aws-lambda (production)")
AGREEMENT_KEYS = ("n", "agree", "judge_flagged", "code_flagged", "both_flagged")


def _cursor_pages(list_fn, what: str, limit: int = 100) -> list[dict]:
    """All items of a cursor-paginated v2 list endpoint (evaluators, evaluation rules), as plain dicts."""
    out, cursor = [], None
    for _ in range(50):
        resp = list_fn(limit=limit, cursor=cursor) if cursor else list_fn(limit=limit)
        out += [_row(x) for x in resp.data]
        cursor = getattr(getattr(resp, "meta", None), "cursor", None)
        if not cursor:
            return out
    raise RuntimeError(f"{what}: more than 50 pages")


def llm_connections(client) -> list[dict]:
    out, page = [], 1
    while True:
        resp = client.api.llm_connections.list(page=page, limit=100)
        out += [_row(x) for x in resp.data]
        total = getattr(getattr(resp, "meta", None), "total_pages", 1) or 1
        if page >= total:
            return out
        page += 1


def judge_verdict(value):
    """BOOLEAN judge/code score value -> bool (True/False/1/0/'true'/'false'); anything else -> None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
        return value.strip().lower() in ("true", "1")
    return None


def _filters(rule: dict) -> dict:
    return {f.get("column"): f for f in (rule.get("filter") or [])}


def module_07_checks(run_tag: str, client, *, prefix: str = "", live_tag: str = "live-eval",
                     agreement: dict | None = None, unjudged=("returns_policy_unjudged",),
                     expected_traces: int | None = 5) -> list[Check]:
    """Live-evaluation checks over every support-request root tagged `run_tag`: the LLM connection, the evaluators,
    the rule, judge scores on opted-in roots only, judge-vs-code calibration, the planted wrong_handoff caught by the
    routing judge, and (⚠️ while disabled on the server) a code evaluator.
    `live_tag` is the opt-in tag the rule filters on (PREFIX + "live-eval" in the notebook)."""
    names = {k: prefix + v for k, v in LIVE_NAMES.items()}

    def fetch():
        traces = fetch_run(client, run_tag, expected_traces=expected_traces)
        return {
            "traces": traces,
            "scores": {root.trace_id: scores_for(client, trace_id=root.trace_id) for root, _ in traces},
            "connections": llm_connections(client),
            "evaluators": _cursor_pages(client.api.evaluators.list, "evaluators"),
            "rules": _cursor_pages(client.api.evaluation_rules.list, "evaluation rules"),
        }

    snapshot = RunSnapshot(fetch, fields=FIELDS)

    def tags(root):
        return getattr(root, "tags", None) or []

    def judged_roots():
        return [r for r, _ in snapshot.get()["traces"] if live_tag in tags(r)]

    def unjudged_roots():
        return [r for r, _ in snapshot.get()["traces"]
                if live_tag not in tags(r) and any(u in tags(r) for u in unjudged)]

    def evaluators_named(key):
        return [e for e in snapshot.get()["evaluators"] if e.get("name") == names[key]]

    def one(key):
        found = evaluators_named(key)
        return found[0] if len(found) == 1 else None

    def connection():
        found = [c for c in snapshot.get()["connections"] if c.get("provider") == names["connection"]]
        return found[0] if found else None

    def scores(root, name=None, source=None):
        return [s for s in snapshot.get()["scores"].get(root.trace_id, [])
                if (name is None or s.get("name") == name) and (source is None or s.get("source") == source)]

    def connection_check():
        c = connection()
        if c is None:
            have = [x.get("provider") for x in snapshot.get()["connections"]]
            return False, f"no LLM connection with provider {names['connection']!r} (found {have}); run ensure_llm_connection()"
        if _plain(c.get("adapter")) != "azure":
            return False, f"{names['connection']}: adapter {c.get('adapter')!r} (want 'azure')"
        if not c.get("customModels"):
            return False, f"{names['connection']}: no customModels (list your Azure deployment names)"
        return True, f"provider {c['provider']} (azure), models {c['customModels']}, base {c.get('baseURL')}"

    def evaluators_check():
        problems, conn = [], connection()
        for key, dtype in (("routing", "BOOLEAN"), ("helpfulness", "NUMERIC")):
            found = evaluators_named(key)
            if not found:
                problems.append(f"{names[key]}: missing")
                continue
            if len(found) > 1:
                problems.append(f"{names[key]}: {len(found)} evaluators with this name (a create that timed out on "
                                "the client was still created: list before you create)")
                continue
            e = found[0]
            out = e.get("outputDefinition") or {}
            if _plain(e.get("type")) != "llm_as_judge":
                problems.append(f"{names[key]}: type {e.get('type')!r} (want llm_as_judge)")
            if _plain(out.get("dataType")) != dtype:
                problems.append(f"{names[key]}: output {out.get('dataType')} (want {dtype})")
            if dtype == "NUMERIC" and (out.get("minValue"), out.get("maxValue")) != (0.0, 1.0):
                problems.append(f"{names[key]}: range {out.get('minValue')}..{out.get('maxValue')} (want 0..1)")
            if _plain(e.get("status")) != "active":
                problems.append(f"{names[key]}: status {_plain(e.get('status'))}: {e.get('pausedMessage')}")
            model = e.get("modelConfig") or {}
            if conn is not None and (model.get("provider") != conn.get("provider")
                                     or model.get("model") not in (conn.get("customModels") or [])):
                problems.append(f"{names[key]}: modelConfig {model or None} is not a model of connection "
                                f"{conn.get('provider')} {conn.get('customModels')}")
            if key == "routing" and not any(
                    _plain(m.get("source")) == "metadata" and (m.get("jsonPath") or "").startswith("$")
                    for m in e.get("variableMapping") or []):
                problems.append(f"{names[key]}: no variable mapped from metadata with a jsonPath "
                                "(the judge needs metadata $.handoff_target)")
        if problems:
            return False, "; ".join(problems)
        r, h = one("routing"), one("helpfulness")
        return True, (f"{names['routing']} v{r.get('version')} (BOOLEAN) + {names['helpfulness']} v{h.get('version')} "
                      f"(NUMERIC 0..1), both active on {(r.get('modelConfig') or {}).get('model')}")

    def rule_check():
        found = [x for x in snapshot.get()["rules"] if x.get("name") == names["rule"]]
        if not found:
            return False, f"no evaluation rule named {names['rule']!r}; run ensure_rule()"
        if len(found) > 1:
            return False, f"{len(found)} rules named {names['rule']!r} (get-or-create created duplicates)"
        rule, problems = found[0], []
        f = _filters(rule)
        if not rule.get("enabled"):
            problems.append("rule is disabled")
        if rule.get("sampling") != 1:
            problems.append(f"sampling {rule.get('sampling')} (use 1.0 here so every opted-in trace is judged)")
        tag_f = f.get("tags") or {}
        if _plain(tag_f.get("operator")) != "any of" or live_tag not in (tag_f.get("value") or []):
            problems.append(f"needs filter tags any of [{live_tag!r}] (have {tag_f or None})")
        if (f.get("isRootObservation") or {}).get("value") is not True:
            problems.append("needs filter isRootObservation = true (otherwise every span of a trace is judged)")
        name_f = f.get("name") or {}
        if LIVE_ROOT_NAME not in (name_f.get("value") or []):
            problems.append(f"needs filter name any of [{LIVE_ROOT_NAME!r}] (have {name_f or None})")
        if "traceName" in f:
            problems.append("drop the traceName filter: our observations carry no trace name, so it never matches")
        assigned = {a.get("evaluatorId") for a in rule.get("evaluatorAssignments") or []}
        for key in ("routing", "helpfulness"):
            e = one(key)
            if e is not None and e["id"] not in assigned:
                problems.append(f"{names[key]} is not assigned to the rule")
        if problems:
            return False, "; ".join(problems)
        return True, (f"{names['rule']}: enabled, sampling 1, tags any of [{live_tag}], root name {LIVE_ROOT_NAME}, "
                      f"{len(assigned)} evaluator(s)")

    def judged_check():
        roots = judged_roots()
        if not roots:
            return False, f"no trace in this run is tagged {live_tag!r} (the opt-in tag the rule filters on)"
        missing, problems = [], []
        for r in roots:
            for key in ("routing", "helpfulness"):
                rows = scores(r, names[key], "EVAL")
                if not rows:
                    missing.append(f"{r.trace_id}:{names[key]}")
                    continue
                s, e = rows[0], one(key)
                subject = s.get("subject") or {}
                if subject.get("kind") != "observation" or subject.get("id") != r.id:
                    problems.append(f"{names[key]} in {r.trace_id} is on {subject.get('kind')} {subject.get('id')}, "
                                    f"not the root observation {r.id}")
                if e is not None and (s.get("metadata") or {}).get("evaluator_id") != e["id"]:
                    problems.append(f"{names[key]} in {r.trace_id} came from evaluator "
                                    f"{(s.get('metadata') or {}).get('evaluator_id')}, not {e['id']}")
                if not (s.get("comment") or "").strip():
                    problems.append(f"{names[key]} in {r.trace_id} has no reasoning comment")
        if problems:
            return False, "; ".join(problems[:4])
        if missing:
            raise NotYet(f"{len(missing)} judge score(s) not in yet (the worker calls Azure, ~30 s): "
                         + ", ".join(missing[:4]) + ". If this persists: docker logs --tail 50 ch09_langfuse_worker")
        return True, f"{len(roots)} opted-in trace(s), each judged by both evaluators on its root observation"

    def scope_check():
        roots = unjudged_roots()
        if not roots:
            return False, f"no trace for the not-opted-in case(s) {list(unjudged)} in this run"
        judged = judged_roots()
        if not judged or not all(scores(r, names["routing"], "EVAL") for r in judged):
            raise NotYet("waiting for the opted-in traces to be judged first")
        leaked = [r.trace_id for r in roots if scores(r, source="EVAL")]
        if leaked:
            return False, (f"trace(s) without the {live_tag!r} tag were judged: {', '.join(leaked)}. Your rule "
                           "matches more than the opted-in traffic (check its tags filter)")
        return True, f"{len(roots)} trace(s) without {live_tag!r} have no EVAL score: the filter scopes the rule"

    def calibration_check():
        pairs, problems = [], []
        for r in judged_roots():
            code = scores(r, names["code_score"], "API")
            if not code:
                raise NotYet(f"trace {r.trace_id} has no {names['code_score']} code score yet")
            c = code[0]
            if not c.get("configId") or not (c.get("metadata") or {}).get("evaluator"):
                problems.append(f"{names['code_score']} in {r.trace_id}: configId={c.get('configId')}, metadata."
                                f"evaluator={(c.get('metadata') or {}).get('evaluator')} (send it through send_score)")
            judge = scores(r, names["routing"], "EVAL")
            if not judge:
                raise NotYet(f"trace {r.trace_id} has no {names['routing']} score yet")
            pairs.append((judge_verdict(judge[0].get("value")), judge_verdict(c.get("value"))))
        if problems:
            return False, "; ".join(problems[:4])
        pairs = [(j, k) for j, k in pairs if j is not None and k is not None]
        want = {"n": len(pairs), "agree": sum(j == k for j, k in pairs),
                "judge_flagged": sum(not j for j, _ in pairs), "code_flagged": sum(not k for _, k in pairs),
                "both_flagged": sum(not j and not k for j, k in pairs)}
        if agreement is None:
            return False, f"pass agreement=REPORT (from judging.agreement); the stored scores give {want}"
        got = {k: agreement.get(k) for k in AGREEMENT_KEYS}
        if got != want:
            return False, f"your agreement {got} != recomputed from the stored scores {want}"
        return True, (f"judge agrees with {names['code_score']} on {want['agree']}/{want['n']} trace(s); "
                      f"judge flagged {want['judge_flagged']}, code flagged {want['code_flagged']}")

    def planted_check():
        planted = [r for r in judged_roots() if PLANTED_TAG in tags(r)]
        if not planted:
            return False, f"no opted-in trace tagged {PLANTED_TAG!r} (tag the planted case and opt it in)"
        missed = []
        for r in planted:
            rows = scores(r, names["routing"], "EVAL")
            if not rows:
                raise NotYet(f"planted trace {r.trace_id} has no {names['routing']} score yet")
            if judge_verdict(rows[0].get("value")) is not False:
                missed.append(f"{r.trace_id} ({rows[0].get('comment')!r})")
        if missed:
            return False, (f"the routing judge passed the planted wrong_handoff: {'; '.join(missed)}. Does the "
                           "prompt state the routing policy and receive the handoff target (metadata $.handoff_target)?")
        planted_ids = {r.trace_id for r in planted}
        disagree = [r.trace_id for r in judged_roots() if r.trace_id not in planted_ids
                    and any(judge_verdict(s.get("value")) is False for s in scores(r, names["routing"], "EVAL"))]
        note = (f"; it also judged {len(disagree)} clean trace(s) False, worth a look: "
                + ", ".join(disagree)) if disagree else ""
        return True, f"{len(planted)} planted wrong_handoff trace(s) judged {names['routing']}=False{note}"

    def code_evaluator_check():
        found = evaluators_named("code")
        if not found:
            raise FeatureUnavailable(CODE_EVAL_HINT)
        e = found[0]
        if _plain(e.get("type")) != "code":
            return False, f"{names['code']} has type {e.get('type')!r} (want code)"
        rule = next((x for x in snapshot.get()["rules"] if x.get("name") == names["rule"]), None)
        if rule is None or e["id"] not in {a.get("evaluatorId") for a in rule.get("evaluatorAssignments") or []}:
            return False, f"{names['code']} exists but is not assigned to {names['rule']}"
        mine = [s for r in judged_roots() for s in scores(r, source="EVAL")
                if (s.get("metadata") or {}).get("evaluator_id") == e["id"]]
        if not mine:
            raise NotYet(f"no score from code evaluator {names['code']} yet")
        return True, f"code evaluator {names['code']} produced {len(mine)} score(s)"

    return [
        Check("LLM connection (Azure)", connection_check),
        Check("judge evaluators defined once + active", evaluators_check),
        Check("evaluation rule (filters, sampling, assignments)", rule_check),
        Check("every opted-in trace judged on its root observation", judged_check),
        Check("rule filter leaves other traffic alone", scope_check),
        Check("judge calibrated against the code score", calibration_check),
        Check("planted wrong_handoff caught by the routing judge", planted_check),
        Check("code evaluator (server-side)", code_evaluator_check),
    ]


def check_module_07(run_tag: str, *, client=None, prefix: str = "", live_tag: str = "live-eval",
                    agreement: dict | None = None, expected_traces: int | None = 5, retries: int = 24,
                    **run_kwargs) -> bool:
    """`retries` defaults higher than other modules: every judge score is a worker -> Azure round trip."""
    client = client or get_client()
    checks = module_07_checks(run_tag, client, prefix=prefix, live_tag=live_tag, agreement=agreement,
                              expected_traces=expected_traces)
    return _check_module(checks, client, retries=retries, **run_kwargs)



# ---------------------------------------------------------------- module 08
# Verified live (server 4.77 / SDK 5.2), see docs/plans/2026-11-12-langfuse-module-08.md:
# - annotation queues need >= 1 score config, names are unique, and there is NO update/archive/delete endpoint.
#   Queue items are NOT deduped (the same trace can be queued twice) and the object id is not checked.
# - ANNOTATION scores via api.scores.create need a configId (else dropped silently, worker log only). UI scores carry
#   authorUserId + queueId; API ones carry queueId (if sent) and authorUserId=None. `fields=...,annotation` returns both.
# - comments: object_type upper case (TRACE/OBSERVATION/SESSION/PROMPT); no list endpoint for queue assignments.
REVIEW_SCORE_FIELDS = "details,subject,annotation"
REVIEW_NAMES = {  # role -> base name (the notebook prefixes every project-global name with PREFIX)
    "queue": "routing-review",
    "human": "human_routing_ok",
    "error_type": "review_error_type",
    "routing": LIVE_NAMES["routing"],
    "helpfulness": LIVE_NAMES["helpfulness"],
    "rule": LIVE_NAMES["rule"],
}
ERROR_TYPES = ("wrong_team", "missed_handoff", "unnecessary_handoff", "other")
STAND_IN_PREFIX = "stand-in"  # metadata.evaluator of the given stand-in judge (langfuse_lab.standin_judge.EVALUATOR)
REPORT_KEYS = ("n", "agree", "judge_flagged", "human_flagged", "both_flagged")
ASSIGNMENT_HINT = ("queue assignments cannot be read back through the public API (there is create/delete but no list "
                   "endpoint), so verify cannot see them. Check Annotation Queues -> your queue -> assignees in the UI")


def _paged(list_fn, what: str, limit: int = 100) -> list[dict]:
    """All items of a page-based list endpoint (meta.total_pages), as plain dicts."""
    out, page = [], 1
    while page <= 100:
        resp = list_fn(page=page, limit=limit)
        out += [_row(x) for x in resp.data]
        total = getattr(getattr(resp, "meta", None), "total_pages", 1) or 1
        if page >= total:
            return out
        page += 1
    raise RuntimeError(f"{what}: more than 100 pages")


def is_judge_score(score: dict) -> bool:
    """A live LLM-judge score (source EVAL) or the module 08 stand-in judge (source API, metadata.evaluator stand-in*)."""
    if score.get("source") == "EVAL":
        return True
    evaluator = str((score.get("metadata") or {}).get("evaluator") or "")
    return score.get("source") == "API" and evaluator.startswith(STAND_IN_PREFIX)


def _latest(rows: list[dict]):
    return max(rows, key=lambda s: str(s.get("timestamp") or "")) if rows else None


def _kappa(pairs) -> float | None:
    n = len(pairs)
    if not n:
        return None
    po = sum(j == h for j, h in pairs) / n
    pj, ph = sum(bool(j) for j, _ in pairs) / n, sum(bool(h) for _, h in pairs) / n
    pe = pj * ph + (1 - pj) * (1 - ph)
    return None if pe >= 1 else (po - pe) / (1 - pe)


def _error_label(expected: str | None, actual: str | None) -> str | None:
    """The module 08 error taxonomy (same rules as review.error_type)."""
    e, a = expected or "none", actual or "none"
    if e == a:
        return None
    return "unnecessary_handoff" if e == "none" else "missed_handoff" if a == "none" else "wrong_team"


def module_08_checks(run_tag: str, client, *, prefix: str = "", threshold: float = 0.5, report: dict | None = None,
                     expected_traces: int | None = 5) -> list[Check]:
    """Human-review checks over every support-request root tagged `run_tag`: review configs, the annotation queue,
    judge scores, queue membership (judge-flagged + controls), completed human reviews with ANNOTATION scores,
    comments, error types, the human-vs-judge report, and the planted wrong_handoff confirmed by the human.
    `threshold`: a NUMERIC judge score (helpfulness, live mode) below it flags the trace for review."""
    names = {k: prefix + v for k, v in REVIEW_NAMES.items()}

    def fetch():
        traces = fetch_run(client, run_tag, expected_traces=expected_traces, fields=FIELDS_WITH_METADATA)
        queues = [q for q in _paged(client.api.annotation_queues.list_queues, "annotation queues")
                  if q.get("name") == names["queue"]]
        items = []
        if len(queues) == 1:
            qid = queues[0]["id"]
            items = _paged(lambda **kw: client.api.annotation_queues.list_queue_items(qid, **kw), "queue items")
        ids = [root.trace_id for root, _ in traces]
        return {
            "traces": traces,
            "scores": {t: scores_for(client, trace_id=t, fields=REVIEW_SCORE_FIELDS) for t in ids},
            "comments": {t: [_row(c) for c in client.api.comments.get(object_type="TRACE", object_id=t, limit=100).data]
                         for t in ids},
            "configs": active_configs(client),
            "queues": queues,
            "items": items,
        }

    snapshot = RunSnapshot(fetch, fields=FIELDS_WITH_METADATA)

    def roots():
        return [r for r, _ in snapshot.get()["traces"]]

    def scores(tid, name):
        return [s for s in snapshot.get()["scores"].get(tid, []) if s.get("name") == name]

    def config_id(key):
        found = snapshot.get()["configs"].get(names[key], [])
        return found[0]["id"] if len(found) == 1 else None

    def queue():
        q = snapshot.get()["queues"]
        return q[0] if len(q) == 1 else None

    def judge_scores(tid, key):
        return [s for s in scores(tid, names[key]) if is_judge_score(s)]

    def routing_ok(tid):
        s = _latest(judge_scores(tid, "routing"))
        return None if s is None else judge_verdict(s.get("value"))

    def flags(tid) -> list[str]:
        out = []
        if routing_ok(tid) is False:
            out.append(f"{names['routing']}=False")
        for s in judge_scores(tid, "helpfulness"):
            v = s.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v < threshold:
                out.append(f"{names['helpfulness']}={v:g}<{threshold:g}")
                break
        return out

    def run_items():
        ids = {r.trace_id for r in roots()}
        return [i for i in snapshot.get()["items"] if i.get("objectId") in ids]

    def queued_ids():
        return {i.get("objectId") for i in run_items() if _plain(i.get("objectType")) == "TRACE"}

    def human(tid):
        cid = config_id("human")
        rows = [s for s in scores(tid, names["human"]) if s.get("source") == "ANNOTATION" and s.get("configId") == cid]
        return _latest(rows)

    def human_ok(tid):
        s = human(tid)
        return None if s is None else judge_verdict(s.get("value"))

    def configs_check():
        configs, problems = snapshot.get()["configs"], []
        for key, dtype, labels in (("human", "BOOLEAN", None), ("error_type", "CATEGORICAL", set(ERROR_TYPES))):
            found = configs.get(names[key], [])
            if not found:
                problems.append(f"{names[key]}: missing")
                continue
            if len(found) > 1:
                problems.append(f"{names[key]}: {len(found)} active configs (get-or-create created duplicates)")
                continue
            c = found[0]
            if c.get("dataType") != dtype:
                problems.append(f"{names[key]}: dataType {c.get('dataType')} (want {dtype})")
            if labels is not None:
                got = {cat.get("label") for cat in (c.get("categories") or [])}
                if got != labels:
                    problems.append(f"{names[key]}: categories {sorted(got)} (want {sorted(labels)})")
        if problems:
            return False, "; ".join(problems)
        return True, f"{names['human']} (BOOLEAN) + {names['error_type']} (CATEGORICAL {', '.join(ERROR_TYPES)})"

    def queue_check():
        q = queue()
        if q is None:
            return False, f"no annotation queue named {names['queue']!r}; run ensure_queue()"
        want = {config_id("human"), config_id("error_type")} - {None}
        missing = want - set(q.get("scoreConfigIds") or [])
        if missing:
            return False, (f"queue {names['queue']} lacks score config(s) {sorted(missing)}. Queues cannot be edited "
                           "through the API: fix it in the UI (Annotation Queues -> edit) or use a new queue name")
        return True, f"queue {names['queue']} ({q['id']}) scores with {len(q.get('scoreConfigIds') or [])} config(s)"

    def judged_check():
        missing = [r.trace_id for r in roots() if routing_ok(r.trace_id) is None]
        if missing:
            raise NotYet(f"{len(missing)}/{len(roots())} trace(s) have no {names['routing']} judge score yet: "
                         + ", ".join(missing[:4]) + " (live mode: the worker calls Azure, ~30 s; stand-in mode: did "
                         "write_standin_judgments run?)")
        modes = Counter("live (EVAL)" if s.get("source") == "EVAL" else "stand-in (API)"
                        for r in roots() for s in [_latest(judge_scores(r.trace_id, "routing"))])
        flagged = sum(bool(flags(r.trace_id)) for r in roots())
        return True, (f"{len(roots())} trace(s) judged ({', '.join(f'{k}: {v}' for k, v in modes.items())}); "
                      f"{flagged} flagged for review")

    def membership_check():
        if queue() is None:
            return False, f"no queue {names['queue']!r}"
        ids = [r.trace_id for r in roots()]
        if any(routing_ok(t) is None for t in ids):
            raise NotYet("waiting for every trace to be judged")
        counts = Counter(i.get("objectId") for i in run_items())
        dupes = [t for t, c in counts.items() if c > 1]
        if dupes:
            return False, (f"trace(s) queued more than once: {', '.join(dupes)}. The API does not dedupe items: list "
                           "the queue's items first (review.items_to_add)")
        queued = queued_ids()
        flagged = [t for t in ids if flags(t)]
        unflagged = [t for t in ids if not flags(t)]
        missing = [t for t in flagged if t not in queued]
        if missing:
            if not queued:
                raise NotYet(f"no run trace in queue {names['queue']} yet; run fill_queue()")
            return False, f"judge-flagged trace(s) not queued: {', '.join(missing)}"
        controls = [t for t in unflagged if t in queued]
        if unflagged and not controls:
            return False, ("no control item: also queue a small sample of traces the judge PASSED "
                           "(review.pick_controls), or the judge's misses are invisible")
        cap = math.ceil(len(unflagged) / 2)
        if len(controls) > cap:
            return False, f"{len(controls)}/{len(unflagged)} passed traces queued as controls (keep it to <= {cap})"
        return True, f"{len(flagged)} judge-flagged + {len(controls)} control trace(s) queued, each once"

    def reviewed_check():
        items = run_items()
        if not items:
            raise NotYet(f"no run items in queue {names['queue']} yet")
        pending = [i["objectId"] for i in items if _plain(i.get("status")) != "COMPLETED"]
        if pending:
            raise NotYet(f"{len(pending)}/{len(items)} queue item(s) still PENDING: review them in the UI (Annotation "
                         f"Queues -> {names['queue']} -> Process queue -> Mark Completed) or run review_pending()")
        problems, ui = [], 0
        q = queue() or {}
        for i in items:
            s = human(i["objectId"])
            if s is None:
                raise NotYet(f"trace {i['objectId']} is COMPLETED but has no {names['human']} ANNOTATION score with "
                             "its config yet (API annotation scores without config_id are dropped silently)")
            if s.get("queueId") != q.get("id"):
                problems.append(f"{names['human']} on {i['objectId']}: queueId {s.get('queueId')} (pass queue_id=)")
            if not i.get("completedAt"):
                problems.append(f"item {i['id']} COMPLETED without completedAt")
            ui += bool(s.get("authorUserId"))
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"{len(items)} item(s) COMPLETED with a human verdict ({ui} scored in the UI, "
                      f"{len(items) - ui} via the API)")

    def comments_check():
        reviewed = [t for t in queued_ids() if human(t) is not None]
        if not reviewed:
            raise NotYet("no reviewed trace yet")
        bare = [t for t in reviewed
                if not any((c.get("content") or "").strip() for c in snapshot.get()["comments"].get(t, []))]
        if bare:
            return False, (f"reviewed trace(s) without a comment: {', '.join(bare)} (api.comments.create(object_type="
                           "'TRACE', ...) or the Comment button on the trace)")
        return True, f"all {len(reviewed)} reviewed trace(s) carry a reviewer comment"

    def error_type_check():
        flagged = [t for t in queued_ids() if human_ok(t) is False]
        if not flagged:
            raise NotYet("no trace judged NOT ok by the human yet")
        cid, missing = config_id("error_type"), []
        for t in flagged:
            rows = [s for s in scores(t, names["error_type"])
                    if s.get("source") == "ANNOTATION" and s.get("configId") == cid]
            if not rows:
                missing.append(t)
        if missing:
            return False, f"human-flagged trace(s) without a {names['error_type']} score: {', '.join(missing)}"
        return True, f"{len(flagged)} human-flagged trace(s) labelled with an error type"

    def report_check():
        pairs = []
        for t in queued_ids():
            h = human_ok(t)
            if h is None:
                raise NotYet(f"trace {t} has no human verdict yet")
            pairs.append((routing_ok(t), h))
        pairs = [(j, h) for j, h in pairs if j is not None]
        want = {"n": len(pairs), "agree": sum(j == h for j, h in pairs),
                "judge_flagged": sum(not j for j, _ in pairs), "human_flagged": sum(not h for _, h in pairs),
                "both_flagged": sum(not j and not h for j, h in pairs)}
        kappa = _kappa(pairs)
        if report is None:
            return False, f"pass report=REPORT (from review.agreement); the stored scores give {want}, kappa={kappa}"
        got = {k: report.get(k) for k in REPORT_KEYS}
        if got != want:
            return False, f"your report {got} != recomputed from the stored scores {want}"
        rk = report.get("kappa")
        if (rk is None) != (kappa is None) or (kappa is not None and abs(rk - kappa) > 1e-6):
            return False, f"your kappa {rk} != {kappa} recomputed from the stored scores"
        shown = "n/a" if kappa is None else f"{kappa:.2f}"
        return True, (f"human agrees with the judge on {want['agree']}/{want['n']} reviewed trace(s), kappa={shown}; "
                      f"judge flagged {want['judge_flagged']}, human confirmed {want['both_flagged']}")

    def planted_check():
        planted = [r for r in roots() if PLANTED_TAG in (getattr(r, "tags", None) or [])]
        if not planted:
            return False, f"no trace tagged {PLANTED_TAG!r} (tag the planted case with its failures)"
        labels = []
        for r in planted:
            t = r.trace_id
            if routing_ok(t) is None:
                raise NotYet(f"planted trace {t} not judged yet")
            if not flags(t):
                return False, (f"the judge passed the planted wrong_handoff {t}: it only reaches the queue as a "
                               "control, by luck. In live mode, check the routing judge prompt (module 07)")
            if t not in queued_ids():
                return False, f"planted trace {t} was flagged ({flags(t)}) but is not in the queue"
            if human_ok(t) is None:
                raise NotYet(f"planted trace {t} not reviewed yet")
            if human_ok(t) is not False:
                return False, f"the reviewer marked the planted wrong_handoff {t} as routed correctly"
            et = _latest([s for s in scores(t, names["error_type"]) if s.get("source") == "ANNOTATION"])
            if et is None:
                raise NotYet(f"planted trace {t} has no {names['error_type']} yet")
            actual = _metadata(r).get("handoff_target") or "none"
            want = _error_label(LOGIN_TEAM, actual) or "?"
            if et.get("value") != want:
                return False, (f"planted trace {t}: error type {et.get('value')!r}, but the agent chose {actual} where "
                               f"the policy says {LOGIN_TEAM}: that is {want}")
            labels.append(want)
        return True, (f"{len(planted)} planted wrong_handoff trace(s): judge-flagged, queued, human-confirmed "
                      f"({', '.join(labels)})")

    def assignment_check():
        raise FeatureUnavailable(ASSIGNMENT_HINT)

    return [
        Check("review score configs defined once", configs_check),
        Check("annotation queue (scores with both configs)", queue_check),
        Check("every trace has a judge verdict", judged_check),
        Check("queue = judge-flagged traces + controls, no duplicates", membership_check),
        Check("every queued trace reviewed (ANNOTATION score, COMPLETED)", reviewed_check),
        Check("reviewer comments on reviewed traces", comments_check),
        Check("error type on human-flagged traces", error_type_check),
        Check("human-vs-judge report matches the stored scores", report_check),
        Check("planted wrong_handoff caught: flagged, queued, confirmed", planted_check),
        Check("queue assignment (read back)", assignment_check),
    ]


def check_module_08(run_tag: str, *, client=None, prefix: str = "", threshold: float = 0.5, report: dict | None = None,
                    expected_traces: int | None = 5, retries: int = 18, **run_kwargs) -> bool:
    """`retries` is a little higher than usual: live judge scores and annotation scores are ingested asynchronously."""
    client = client or get_client()
    checks = module_08_checks(run_tag, client, prefix=prefix, threshold=threshold, report=report,
                              expected_traces=expected_traces)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 09
# Verified live (server 4.77 / SDK 5.2, 2026-11-12):
# - create_dataset upserts by name; create_dataset_item with the same id upserts = a NEW item version
#   (item.version = when it became current). dataset_items.list(version=dt) returns the items as of dt and never lists
#   ARCHIVED items; dataset_items.get(id) still returns them. No delete endpoint for datasets.
# - the legacy dataset-run endpoints answer 404 in v4 events_only mode: experiments are read with api.experiments
#   (list: id, name=run_name, itemCount, datasetId, metadata, scores=run-level; list_items: experimentItemId = the
#   dataset item id, experimentItemVersion = the pinned dataset version, input/output/expectedOutput JSON strings,
#   scores = item scores). Experiment traces live in environment "sdk-experiment".
# - the same run_name on the same dataset = the same experiment id: a re-run appends duplicate items.
# - an Evaluation with config_id is stored under the CONFIG's name (Evaluation(name="handoff_correct",
#   config_id=<zz_ref_m09_handoff_correct>) reads back as "zz_ref_m09_handoff_correct").
# - list_items' item scores carry no configId/comment/metadata: read those with scores.get_many(trace_id=...).
# - remote-run URLs are configured in the UI only (not in the public API); UI prompt experiments run in the worker
#   and write traces to environment "langfuse-prompt-experiment".
REGRESSION_NAMES = {  # role -> base name (the notebook prefixes every project-global name with PREFIX)
    "dataset": "support-regression",
    "prompt": "support-agent-system",  # module 04's managed prompt (prompts.PROMPT_NAME)
    "config": "handoff_correct",       # module 06's BOOLEAN score config
    "chat_prompt": "support-agent-chat",
}
EXPERIMENT_NAME = "prompt-routing"
BASELINE_LABEL, CANDIDATE_LABEL = "production", "staging"
ITEM_EVALS = ("concise",)  # + the config-named handoff score (names["config"])
COMPOSITE_EVAL = "quality"
RUN_EVALS = ("handoff_accuracy", "quality_avg")
COMPARISON_KEYS = ("n", "baseline_rate", "candidate_rate", "regressed", "improved")
EXPERIMENT_FIELDS = "core,metadata,scores"
EXPERIMENT_ITEM_FIELDS = "core,dataset,io,metadata,itemMetadata,scores"
PROMPT_EXPERIMENT_ENV = "langfuse-prompt-experiment"
REMOTE_HINT = ("the dataset's remote-run URL is configured in the UI only (Datasets -> dataset -> Run experiment -> "
               "Remote experiment) and is not readable through the public API. On this deployment Langfuse would "
               "also refuse a local URL: it must be http(s) on port 80/443 and pass the SSRF check (private hosts "
               "need LANGFUSE_WEBHOOK_WHITELISTED_HOST / _IPS on the web container). Your handler was exercised by "
               "the simulated, identically signed POST instead")
UI_EXPERIMENT_HINT = ("optional: no UI prompt experiment on the dataset yet (Datasets -> your dataset -> Run "
                      "experiment -> Prompt experiment, with module 07's LLM connection; its traces land in "
                      f"environment {PROMPT_EXPERIMENT_ENV!r})")


def _is_not_found(exc: Exception) -> bool:
    return type(exc).__name__ == "NotFoundError" or getattr(exc, "status_code", None) == 404


def _as_datetime(v):
    if isinstance(v, datetime) or v is None:
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _dictish(v) -> dict:
    v = _decode(v)
    if hasattr(v, "dict"):
        v = v.dict()
    return v if isinstance(v, dict) else {}


def _score_rows(rows) -> list[dict]:
    return [_row(s) for s in (rows or [])]


def module_09_checks(run_tag: str, client, *, prefix: str = "", comparison: dict | None = None, decision=None,
                     expected_traces: int | None = 4, hours: int = WINDOW_HOURS) -> list[Check]:
    """Dataset + experiment checks for the run tagged `run_tag`: the regression dataset built from the run's labelled
    source traces (+ curated items), its versioning (label fix, archived item), the production-vs-staging
    experiments on the pinned dataset (prompt links, item/composite/run evaluators), the learner's comparison and
    gate decision, the planted login regression, the remote trigger and the (optional) UI prompt experiment.
    Experiment metadata must carry run_tag, prompt_label, prompt_version and trigger ("notebook" | "remote")."""
    from . import regression

    names = {k: prefix + v for k, v in REGRESSION_NAMES.items()}
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    fix_case, fix_team = regression.LABEL_FIX

    def item_id(case_id):
        return regression.dataset_item_id(names["dataset"], regression.curated(case_id).message)

    def fetch():
        traces = fetch_run(client, run_tag, expected_traces=expected_traces, fields=FIELDS_WITH_METADATA)
        snap = {"traces": traces, "src_scores": {r.trace_id: scores_for(client, trace_id=r.trace_id) for r, _ in traces},
                "dataset": None, "items": [], "fix_prev": None, "retired": None, "experiments": [], "exp_items": {},
                "generations": {}, "trace_scores": {}, "ui_items": [], "labels": {}, "configs": active_configs(client)}
        for label in (BASELINE_LABEL, CANDIDATE_LABEL):
            try:
                snap["labels"][label] = client.api.prompts.get(names["prompt"], label=label).version
            except Exception as exc:
                if not _is_not_found(exc):
                    raise
        try:
            ds = _row(client.api.datasets.get(names["dataset"]))
        except Exception as exc:
            if _is_not_found(exc):
                return snap
            raise
        snap["dataset"] = ds
        snap["items"] = _paged(lambda **kw: client.api.dataset_items.list(dataset_name=names["dataset"], **kw),
                               "dataset items")
        fix = next((i for i in snap["items"] if i["id"] == item_id(fix_case)), None)
        if fix and fix.get("version"):
            before = _as_datetime(fix["version"]) - timedelta(milliseconds=1)
            prev = _paged(lambda **kw: client.api.dataset_items.list(dataset_name=names["dataset"], version=before,
                                                                     **kw), "dataset items (previous version)")
            snap["fix_prev"] = next((i for i in prev if i["id"] == fix["id"]), None)
        try:
            snap["retired"] = _row(client.api.dataset_items.get(item_id(regression.RETIRED)))
        except Exception as exc:
            if not _is_not_found(exc):
                raise
        exps = _cursor_pages(lambda **kw: client.api.experiments.list(from_start_time=since, dataset_id=ds["id"],
                                                                      fields=EXPERIMENT_FIELDS, **kw), "experiments")
        snap["experiments"] = [e for e in exps if _dictish(e.get("metadata")).get("run_tag") == run_tag]
        for e in snap["experiments"]:
            rows = _cursor_pages(lambda **kw: client.api.experiments.list_items(
                from_start_time=since, experiment_id=e["id"], fields=EXPERIMENT_ITEM_FIELDS, **kw), "experiment items")
            snap["exp_items"][e["id"]] = rows
            for it in rows:
                obs_ = client.api.observations.get_many(trace_id=it["traceId"], fields="core,prompt", limit=200).data
                snap["generations"][it["traceId"]] = [o for o in obs_ if getattr(o, "type", None) == "GENERATION"]
                snap["trace_scores"][it["traceId"]] = scores_for(client, trace_id=it["traceId"])
        resp = client.api.experiments.list_items(from_start_time=since, dataset_id=ds["id"], fields="core", limit=100)
        snap["ui_items"] = [_row(x) for x in resp.data if _row(x).get("environment") == PROMPT_EXPERIMENT_ENV]
        return snap

    snapshot = RunSnapshot(fetch, fields=FIELDS_WITH_METADATA)

    def snap():
        return snapshot.get()

    def dataset():
        ds = snap()["dataset"]
        if ds is None:
            raise NotYet(f"no dataset named {names['dataset']!r} yet; run build_dataset()")
        return ds

    def active():
        return [i for i in snap()["items"] if _plain(i.get("status")) == "ACTIVE"]

    def message(i) -> str:
        return str(_dictish(i.get("input")).get("message") or "").strip()

    def expected(i) -> str | None:
        return _dictish(i.get("expectedOutput")).get("handoff")

    def by_message():
        return {message(i): i for i in active()}

    def runs(label, trigger="notebook"):
        return [e for e in snap()["experiments"]
                if _dictish(e.get("metadata")).get("prompt_label") == label
                and _dictish(e.get("metadata")).get("trigger", "notebook") == trigger]

    def the_run(label):
        found = runs(label)
        if not found:
            raise NotYet(f"no {label!r} experiment with metadata run_tag={run_tag!r} on {names['dataset']} yet; "
                         "run run_variant()")
        if len(found) > 1:
            raise AssertionError(f"{len(found)} {label!r} experiments for this run tag ({[e['name'] for e in found]}); "
                                 "run each variant once per RUN_TAG")
        return found[0]

    def items_of(e):
        return snap()["exp_items"].get(e["id"], [])

    def item_scores(it, name):
        return [s for s in _score_rows(it.get("scores")) if s.get("name") == name]

    def hc(it):
        s = _latest(item_scores(it, names["config"]))
        return None if s is None else judge_verdict(s.get("value"))

    def verdicts(e) -> dict:
        return {it["experimentItemId"]: hc(it) for it in items_of(e)}

    def recomputed():
        base, cand = verdicts(the_run(BASELINE_LABEL)), verdicts(the_run(CANDIDATE_LABEL))
        ids = sorted(i for i in set(base) & set(cand) if base[i] is not None and cand[i] is not None)
        if not ids:
            raise NotYet(f"no experiment item has a {names['config']} score in both runs yet")

        def rate(m):
            return sum(bool(m[i]) for i in ids) / len(ids)

        return {"n": len(ids), "baseline_rate": rate(base), "candidate_rate": rate(cand),
                "regressed": [i for i in ids if base[i] and not cand[i]],
                "improved": [i for i in ids if not base[i] and cand[i]]}

    def dataset_check():
        ds, items = dataset(), active()
        if len(items) < 6:
            raise NotYet(f"{len(items)} active item(s) in {names['dataset']} (want >= 6: 4 from traces + 2 curated)")
        bad = [i["id"] for i in items if not message(i) or not expected(i)]
        if bad:
            return False, f"item(s) without input.message / expected_output.handoff: {', '.join(bad[:4])}"
        dupes = [m for m, c in Counter(message(i) for i in items).items() if c > 1]
        if dupes:
            return False, (f"{len(dupes)} request(s) appear in more than one active item (e.g. {dupes[0]!r}): use "
                           "regression.dataset_item_id(dataset, message) as the item id so a re-add is an upsert")
        return True, f"{ds['name']} ({ds['id']}): {len(items)} active labelled item(s), one per request"

    def from_traces_check():
        dataset()
        index, problems, linked, failures = by_message(), [], 0, 0
        run_ids = {r.trace_id for r, _ in snap()["traces"]}
        for r, _ in snap()["traces"]:
            msg = str(_decode(r.input) or "").strip()
            scores = snap()["src_scores"].get(r.trace_id, [])
            code = _latest([s for s in scores if s.get("name") == names["config"]])
            if code is None:
                raise NotYet(f"source trace {r.trace_id} has no {names['config']} label yet")
            want = (code.get("metadata") or {}).get("expected")
            it = index.get(msg)
            if it is None:
                problems.append(f"no active item for the labelled trace {r.trace_id} ({msg[:40]!r})")
                continue
            if not it.get("sourceTraceId"):
                problems.append(f"item {it['id']} has no source_trace_id (link it to the trace it came from)")
            linked += it.get("sourceTraceId") in run_ids
            if expected(it) != want:
                problems.append(f"item {it['id']} expects {expected(it)!r}, the trace's label says {want!r}")
            failed = judge_verdict(code.get("value")) is False
            failures += failed
            want_label = "confirmed_failure" if failed else "golden"
            label = _dictish(it.get("metadata")).get("label")
            if label != want_label:
                problems.append(f"item {it['id']}: metadata.label {label!r} (want {want_label})")
            correction = _latest([s for s in scores if _plain(s.get("dataType")) == "CORRECTION"])
            reference = _dictish(it.get("expectedOutput")).get("reference_answer")
            if correction is not None and reference != correction.get("value"):
                problems.append(f"item {it['id']}: expected_output.reference_answer is not the trace's CORRECTION")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"{len(snap()['traces'])} labelled trace(s) -> items ({failures} confirmed failure(s)); "
                      f"{linked} linked to this run's traces, the rest to earlier runs (same request, same item)")

    def versioning_check():
        dataset()
        fix = next((i for i in active() if i["id"] == item_id(fix_case)), None)
        if fix is None:
            return False, (f"no active curated item {fix_case!r} (id regression.dataset_item_id(dataset, message)); "
                           "build_dataset adds the curated cases")
        if expected(fix) != fix_team:
            return False, (f"{fix_case} still expects {expected(fix)!r}: the routing policy says invoice -> "
                           f"{fix_team}; run revise_item to fix the label")
        prev = snap()["fix_prev"]
        if prev is None or expected(prev) == fix_team:
            return False, (f"{fix_case}: no earlier item version with the old label (got "
                           f"{None if prev is None else expected(prev)!r}); fix it with an upsert of the same id")
        retired = snap()["retired"]
        if retired is None:
            return False, f"no item for the retired case {regression.RETIRED!r}"
        if _plain(retired.get("status")) != "ARCHIVED":
            return False, f"{regression.RETIRED} is {_plain(retired.get('status'))}, not ARCHIVED"
        return True, (f"{fix_case}: version {fix.get('version')} expects {fix_team} (previous version: "
                      f"{expected(prev)}); {regression.RETIRED} ARCHIVED")

    def variants_check():
        dataset()
        want_ids = {i["id"] for i in active()}
        versions, details = set(), []
        for label in (BASELINE_LABEL, CANDIDATE_LABEL):
            e = the_run(label)
            rows = items_of(e)
            if not rows:
                raise NotYet(f"experiment {e['name']} has no items ingested yet")
            got = Counter(it.get("experimentItemId") for it in rows)
            dupes = [i for i, c in got.items() if c > 1]
            if dupes:
                return False, (f"{e['name']}: {len(dupes)} item(s) run more than once: the same run_name appends to "
                               "the same experiment. Restart with a new RUN_TAG")
            if set(got) != want_ids:
                missing, extra = sorted(want_ids - set(got)), sorted(set(got) - want_ids)
                if missing and not extra:
                    raise NotYet(f"{e['name']}: {len(got)}/{len(want_ids)} items ingested")
                return False, (f"{e['name']} ran on {len(got)} item(s), the dataset has {len(want_ids)} active: "
                               f"missing {missing[:3]}, extra (archived/old) {extra[:3]}. Pin AFTER revising")
            raw = [it.get("experimentItemVersion") for it in rows]
            vs = {str(v) for v in raw}
            if None in raw or len(vs) != 1:
                return False, (f"{e['name']}: items not pinned to one dataset version ({sorted(vs)[:3]}): use "
                               "langfuse.get_dataset(name, version=DATASET_VERSION).run_experiment(...)")
            versions |= vs
            meta = _dictish(e.get("metadata"))
            have = snap()["labels"].get(label)
            if str(meta.get("prompt_version")) != str(have):
                return False, (f"{e['name']}: metadata prompt_version={meta.get('prompt_version')!r}, but "
                               f"{label!r} is on v{have}")
            details.append(f"{label} v{have}: {len(rows)} items")
        if len(versions) != 1:
            return False, f"the two variants ran on different dataset versions {sorted(versions)}"
        return True, f"{'; '.join(details)}; both pinned to dataset version {versions.pop()}"

    def links_check():
        problems, n = [], 0
        for label in (BASELINE_LABEL, CANDIDATE_LABEL):
            e = the_run(label)
            version = snap()["labels"].get(label)
            for it in items_of(e):
                gens = snap()["generations"].get(it["traceId"], [])
                if not gens:
                    raise NotYet(f"no GENERATION in experiment trace {it['traceId']} yet")
                for g in gens:
                    n += 1
                    if getattr(g, "prompt_name", None) != names["prompt"] or getattr(g, "prompt_version", None) != version:
                        problems.append(f"{label}: {it['traceId']}/{g.id} linked to "
                                        f"{getattr(g, 'prompt_name', None)!r} v{getattr(g, 'prompt_version', None)}")
        if problems:
            return False, ("generation(s) not linked to the run's prompt version (link the served prompt inside the "
                           "task: propagate_attributes(prompt=prompt)): " + "; ".join(problems[:3]))
        return True, f"{n} generation(s) in the experiment traces, each linked to its variant's prompt version"

    def evaluators_check():
        cfgs = snap()["configs"].get(names["config"], [])
        cid = cfgs[0]["id"] if len(cfgs) == 1 else None
        problems, n = [], 0
        for label in (BASELINE_LABEL, CANDIDATE_LABEL):
            for it in items_of(the_run(label)):
                n += 1
                for name in (names["config"], *ITEM_EVALS, COMPOSITE_EVAL):
                    if not item_scores(it, name):
                        raise NotYet(f"experiment item {it['experimentItemId']} ({label}) has no {name!r} score yet")
                full = [x for x in snap()["trace_scores"].get(it["traceId"], []) if x.get("name") == names["config"]]
                s = _latest(full) or {}
                if cid is None or s.get("configId") != cid:
                    problems.append(f"{names['config']} on {it['experimentItemId']}: configId {s.get('configId')!r} "
                                    f"(want {names['config']} = {cid}): Evaluation(config_id=...)")
                out, exp = _dictish(it.get("output")), _dictish(it.get("expectedOutput"))
                if hc(it) is not (out.get("handoff") == exp.get("handoff")):
                    problems.append(f"{names['config']}={hc(it)} on {it['experimentItemId']} but output "
                                    f"{out.get('handoff')!r} vs expected {exp.get('handoff')!r}")
                q = _latest(item_scores(it, COMPOSITE_EVAL)).get("value")
                if not isinstance(q, (int, float)) or isinstance(q, bool) or not 0 <= q <= 1:
                    problems.append(f"{COMPOSITE_EVAL}={q!r} on {it['experimentItemId']} (want a number in [0, 1])")
        if problems:
            return False, "; ".join(problems[:3])
        return True, (f"{n} experiment item(s) carry {names['config']} (configId {cid}), "
                      f"{', '.join(ITEM_EVALS)} + composite {COMPOSITE_EVAL}; {names['config']} agrees with output vs expected_output")

    def run_evals_check():
        details = []
        for label in (BASELINE_LABEL, CANDIDATE_LABEL):
            e = the_run(label)
            run_scores = {s.get("name"): s for s in _score_rows(e.get("scores"))}
            missing = [n for n in RUN_EVALS if n not in run_scores]
            if missing:
                raise NotYet(f"{e['name']}: run-level score(s) {missing} not ingested yet (run_evaluators=[...])")
            vals = [v for v in verdicts(e).values() if v is not None]
            want = sum(vals) / len(vals) if vals else None
            got = run_scores["handoff_accuracy"].get("value")
            if want is None or not isinstance(got, (int, float)) or abs(got - want) > 1e-6:
                return False, f"{e['name']}: handoff_accuracy={got!r}, the item scores give {want!r}"
            details.append(f"{label} handoff_accuracy={got:.2f}")
        return True, ", ".join(details)

    def comparison_check():
        want = recomputed()
        if comparison is None:
            return False, f"pass comparison=COMPARISON (from experiments.compare_runs); the stored scores give {want}"
        got = {k: comparison.get(k) for k in COMPARISON_KEYS}
        ok = (got["n"] == want["n"] and sorted(got["regressed"] or []) == want["regressed"]
              and sorted(got["improved"] or []) == want["improved"]
              and all(isinstance(got[k], (int, float)) and abs(got[k] - want[k]) < 1e-6
                      for k in ("baseline_rate", "candidate_rate")))
        if not ok:
            return False, f"your comparison {got} != recomputed from the stored item scores {want}"
        return True, (f"{want['n']} items: baseline {want['baseline_rate']:.2f} -> candidate "
                      f"{want['candidate_rate']:.2f}; {len(want['regressed'])} regressed, "
                      f"{len(want['improved'])} improved")

    def planted_check():
        want = recomputed()
        rows = {it["experimentItemId"]: it for it in items_of(the_run(CANDIDATE_LABEL))}
        # Gate on regressed ITEMS, not on the average: in validation run 2 both runs scored 0.67 (v1 missed the invoice
        # and the vague login; v2 got the invoice right but sent the locked-out login to billing).
        login = [i for i in want["regressed"] if _dictish(rows[i].get("expectedOutput")).get("handoff") == LOGIN_TEAM]
        if not login:
            return False, (f"no login item (expected {LOGIN_TEAM}) regressed from production to staging (regressed: "
                           f"{want['regressed']}; rates {want['baseline_rate']:.2f} -> {want['candidate_rate']:.2f}). "
                           "Is staging the bad candidate (login -> billing)?")
        d = decision[0] if isinstance(decision, (tuple, list)) else decision
        if d != "block":
            return False, f"the gate said {d!r} for a candidate that regressed {len(want['regressed'])} item(s)"

        def case(i):
            it = rows[i]
            meta = _dictish(it.get("experimentItemMetadata")) or _dictish(it.get("metadata"))
            return f"{meta.get('case_id', i)} -> {_dictish(it.get('output')).get('handoff')}"

        other = len(want["regressed"]) - len(login)
        hidden = want["candidate_rate"] >= want["baseline_rate"]
        return True, (f"staging regressed {len(login)} login item(s) ({', '.join(case(i) for i in login)}); gate: block"
                      + (f"; {other} other regression(s)" if other else "")
                      + (f"; the averages ({want['baseline_rate']:.2f} -> {want['candidate_rate']:.2f}) alone would "
                         "have hidden it" if hidden else f"; accuracy {want['baseline_rate']:.2f} -> "
                         f"{want['candidate_rate']:.2f}"))

    def remote_check():
        dataset()
        found = runs(CANDIDATE_LABEL, "remote") + runs(BASELINE_LABEL, "remote")
        if not found:
            return False, ("no experiment with metadata trigger='remote' for this run tag: handle_remote_trigger must "
                           "run run_variant(label, trigger='remote', ...) for a correctly signed request")
        rows = [it for e in found for it in items_of(e)]
        if not rows:
            raise NotYet("remote-triggered experiment has no items ingested yet")
        if any(not item_scores(it, names["config"]) for it in rows):
            raise NotYet("remote-triggered experiment items not scored yet")
        return True, f"{len(found)} remote-triggered run(s) ({found[0]['name']}): {len(rows)} scored item(s)"

    def remote_config_check():
        raise FeatureUnavailable(REMOTE_HINT)

    def ui_experiment_check():
        dataset()
        ui = snap()["ui_items"]
        if not ui:
            raise FeatureUnavailable(UI_EXPERIMENT_HINT)
        return True, f"{len(ui)} UI prompt-experiment item(s) on {names['dataset']} ({ui[0].get('experimentName')})"

    return [
        Check("regression dataset with labelled items", dataset_check),
        Check("items from the run's labelled traces", from_traces_check),
        Check("dataset versioning: label fix + archived item", versioning_check),
        Check("production vs staging on the pinned dataset", variants_check),
        Check("experiment generations linked to the variant's prompt", links_check),
        Check("item + composite evaluators on every item", evaluators_check),
        Check("run evaluators match the item scores", run_evals_check),
        Check("comparison matches the stored scores", comparison_check),
        Check("planted: staging regresses login routing, gate blocks", planted_check),
        Check("remote trigger ran an experiment", remote_check),
        Check("remote-run URL configured in the UI (read back)", remote_config_check),
        Check("UI prompt experiment (optional)", ui_experiment_check),
    ]


def check_module_09(run_tag: str, *, client=None, prefix: str = "", comparison: dict | None = None, decision=None,
                    expected_traces: int | None = 4, retries: int = 18, **run_kwargs) -> bool:
    client = client or get_client()
    checks = module_09_checks(run_tag, client, prefix=prefix, comparison=comparison, decision=decision,
                              expected_traces=expected_traces)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 10
# Verified live (server 4.77 / SDK 5.2, 2026-11-12):
# - create_score(dataset_run_id=<experiment id>, ...) puts a run-level score on an experiment (what run_evaluators do);
#   CATEGORICAL works without a config. experiments.list(fields="scores") returns it WITHOUT comment/metadata;
#   scores.get_many(experiment_id=..., fields="details,subject") has both (lists/dicts intact, subject.kind
#   "experiment").
# - run_experiment(metadata=...) values are stringified (3 -> "3"), not truncated, and copied onto every item.
# - experiments.list filters by id / name / dataset_id only: metadata (run_tag, role, ...) is filtered client-side.
GATE_EXPERIMENT_NAME = "release-gate"
GATE_SCORE = "ci_gate"
GATE_DECISIONS = {"ship": 0, "block": 1, "error": 2}  # decision -> exit code
GATE_META_KEYS = ("git_sha", "branch", "pr", "ci_run", "candidate_label", "prompt_label", "prompt_version",
                  "dataset_version", "trigger", "role", "baseline_experiment_id")
GATE_HOURS = 48  # how far back the snapshot looks for experiments (a cached baseline may be up to 24 h old)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def _stamp(v):
    """A dataset version (datetime or ISO string) as a comparable epoch timestamp (None stays None)."""
    try:
        d = _as_datetime(v)
    except ValueError:
        return str(v)
    return None if d is None else round(d.timestamp(), 3)


def _gate_recompute(base: dict, cand: dict, base_reruns: list[dict], cand_reruns: list[dict], policy: dict) -> dict:
    """The gate decision from stored verdicts, with the same rules as ci_gate.confirm + decide (majority vote over the
    attempts for each suspect; module 09's gate rules on the confirmed comparison; n == 0 -> error)."""
    ids = sorted(i for i in set(base) & set(cand) if base[i] is not None and cand[i] is not None)
    suspects = [i for i in ids if base[i] and not cand[i]]

    def rate(i, attempts):
        vals = [bool(a[i]) for a in attempts if a.get(i) is not None]
        return sum(vals) / len(vals) if vals else None

    rates = {i: (rate(i, [base, *base_reruns]), rate(i, [cand, *cand_reruns])) for i in ids}
    regressed = [i for i in suspects if rates[i][0] > 0.5 and rates[i][1] < 0.5]
    flaky = [i for i in suspects if i not in regressed]
    n = len(ids)
    out = {"n": n, "suspects": suspects, "regressed": regressed, "flaky": flaky, "rates": rates,
           "improved": [i for i in ids if not base[i] and cand[i]]}
    if not n:
        return {**out, "decision": "error", "delta": None}
    delta = sum(rates[i][1] for i in ids) / n - sum(rates[i][0] for i in ids) / n
    max_regressions = int(policy.get("max_regressions", 0))
    max_drop = float(policy.get("max_drop", 0.0))
    block = len(regressed) > max_regressions or delta < -max_drop
    return {**out, "decision": "block" if block else "ship", "delta": delta}


def module_10_checks(run_tag: str, client, *, prefix: str = "", clean_label: str = "candidate",
                     hours: int = GATE_HOURS) -> list[Check]:
    """CI gate checks for the gate runs whose experiment metadata says run_tag=`run_tag` (dry runs excluded): CI
    metadata, the production baseline on the same pinned dataset version, re-runs limited to the suspects, the gate
    decision recorded on the candidate experiment (and recomputed from the stored item scores), the planted staging
    regression blocked, the clean candidate shipped."""
    names = {k: prefix + v for k, v in REGRESSION_NAMES.items()}
    labels_wanted = (BASELINE_LABEL, CANDIDATE_LABEL, clean_label)

    def fetch():
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        snap = {"dataset": None, "labels": {}, "experiments": {}, "mine": [], "items": {}, "gate_scores": {}}
        for label in labels_wanted:
            try:
                snap["labels"][label] = client.api.prompts.get(names["prompt"], label=label).version
            except Exception as exc:
                if not _is_not_found(exc):
                    raise
        try:
            ds = _row(client.api.datasets.get(names["dataset"]))
        except Exception as exc:
            if _is_not_found(exc):
                return snap
            raise
        snap["dataset"] = ds
        exps = _cursor_pages(lambda **kw: client.api.experiments.list(from_start_time=since, dataset_id=ds["id"],
                                                                      fields=EXPERIMENT_FIELDS, **kw), "experiments")
        snap["experiments"] = {e["id"]: e for e in exps}
        snap["mine"] = [e for e in exps if _dictish(e.get("metadata")).get("run_tag") == run_tag
                        and _dictish(e.get("metadata")).get("dry_run") != "true"]
        wanted = {e["id"] for e in snap["mine"]}
        wanted |= {_dictish(e.get("metadata")).get("baseline_experiment_id") for e in snap["mine"]
                   if _dictish(e.get("metadata")).get("role") == "candidate"}
        for eid in sorted(x for x in wanted if x and x in snap["experiments"]):
            snap["items"][eid] = _cursor_pages(lambda **kw: client.api.experiments.list_items(
                from_start_time=since, experiment_id=eid, fields=EXPERIMENT_ITEM_FIELDS, **kw), "experiment items")
        for e in snap["mine"]:
            if _dictish(e.get("metadata")).get("role") == "candidate":
                rows = scores_for(client, experiment_id=e["id"])
                snap["gate_scores"][e["id"]] = [s for s in rows if s.get("name") == GATE_SCORE]
        return snap

    snapshot = RunSnapshot(fetch)

    def snap():
        return snapshot.get()

    def meta(e) -> dict:
        return _dictish(e.get("metadata"))

    def candidate(label):
        if snap()["dataset"] is None:
            raise NotYet(f"no dataset named {names['dataset']!r}: run the given bootstrap cell (or module 09)")
        found = [e for e in snap()["mine"] if meta(e).get("role") == "candidate" and meta(e).get("prompt_label") == label]
        if not found:
            raise NotYet(f"no gate run for candidate {label!r} with metadata run_tag={run_tag!r} yet: run the gate "
                         f"with CANDIDATE_LABEL={label} (metadata role='candidate', prompt_label={label!r})")
        return max(found, key=lambda e: str(e.get("startTime") or ""))

    def items_of(eid) -> list[dict]:
        return snap()["items"].get(eid, [])

    def verdicts(eid) -> dict:
        out = {}
        for it in items_of(eid):
            s = _latest([x for x in _score_rows(it.get("scores")) if x.get("name") == names["config"]])
            out[it["experimentItemId"]] = None if s is None else judge_verdict(s.get("value"))
        return out

    def baseline_of(e):
        return snap()["experiments"].get(meta(e).get("baseline_experiment_id"))

    def reruns_of(e, side):
        return sorted((r for r in snap()["mine"] if meta(r).get("role") == "rerun" and meta(r).get("rerun_of") == e["id"]
                       and meta(r).get("side") == side), key=lambda r: str(meta(r).get("attempt")))

    def gate_score(e):
        rows = snap()["gate_scores"].get(e["id"], [])
        if not rows:
            raise NotYet(f"{e['name']}: no run-level {GATE_SCORE!r} score yet: record_gate() must "
                         f"create_score(dataset_run_id=<candidate experiment id>, name={GATE_SCORE!r}, ...)")
        return _latest(rows)

    def policy_of(e) -> dict:
        return _dictish(_dictish(gate_score(e).get("metadata")).get("policy"))

    def recompute(e) -> dict:
        b = baseline_of(e)
        if b is None:
            raise AssertionError(f"{e['name']}: baseline_experiment_id {meta(e).get('baseline_experiment_id')!r} is not "
                                 f"an experiment on {names['dataset']} in the last {hours}h")
        base, cand = verdicts(b["id"]), verdicts(e["id"])
        if not cand or not base or any(v is None for v in [*cand.values(), *base.values()]):
            raise NotYet(f"{e['name']}: items not ingested/scored yet")
        return _gate_recompute(base, cand, [verdicts(r["id"]) for r in reruns_of(e, "baseline")],
                               [verdicts(r["id"]) for r in reruns_of(e, "candidate")], policy_of(e))

    def item_info(e) -> dict:
        return {it["experimentItemId"]: (_dictish(it.get("experimentItemMetadata")), _dictish(it.get("expectedOutput")))
                for it in items_of(e["id"])}

    def metadata_check():
        details = []
        for label in (CANDIDATE_LABEL, clean_label):
            e = candidate(label)
            m = meta(e)
            missing = [k for k in GATE_META_KEYS if k not in m]
            if missing:
                return False, f"{e['name']}: metadata lacks {missing} (ci_gate.experiment_metadata + the baseline id)"
            if not _SHA.match(m["git_sha"]):
                return False, f"{e['name']}: git_sha={m['git_sha']!r} is not a lower-case hex commit sha"
            if m["git_sha"][:7] not in (e.get("name") or ""):
                return False, f"run name {e.get('name')!r} does not contain the short sha {m['git_sha'][:7]}"
            if m["candidate_label"] != label or not m["ci_run"]:
                return False, f"{e['name']}: candidate_label={m['candidate_label']!r}, ci_run={m['ci_run']!r}"
            have = snap()["labels"].get(label)
            if str(have) != m["prompt_version"]:
                return False, f"{e['name']}: prompt_version={m['prompt_version']!r} but {label!r} is on v{have}"
            if m["trigger"] not in ("ci", "local"):
                return False, f"{e['name']}: trigger={m['trigger']!r} (want 'ci' or 'local')"
            details.append(f"{label} v{have} @ {m['git_sha'][:7]} ({m['trigger']}, run {m['ci_run']}"
                           f"{', branch ' + m['branch'] if m['branch'] else ''}{', PR #' + m['pr'] if m['pr'] else ''})")
        return True, "; ".join(details)

    def baseline_check():
        details, bases = [], set()
        for label in (CANDIDATE_LABEL, clean_label):
            e = candidate(label)
            b = baseline_of(e)
            if b is None:
                return False, (f"{e['name']}: metadata baseline_experiment_id={meta(e).get('baseline_experiment_id')!r} "
                               f"is not an experiment on {names['dataset']}")
            bm = meta(b)
            prod = snap()["labels"].get(BASELINE_LABEL)
            if bm.get("role") != "baseline" or bm.get("prompt_label") != BASELINE_LABEL or bm.get("prompt_version") != str(prod):
                return False, (f"baseline {b['name']}: role={bm.get('role')!r}, {bm.get('prompt_label')!r} "
                               f"v{bm.get('prompt_version')} (want role 'baseline', production v{prod})")
            if bm.get("dry_run") == "true":
                return False, f"baseline {b['name']} is a DRY run: the baseline cache key must tell dry runs apart"
            rows_b, rows_c = items_of(b["id"]), items_of(e["id"])
            if not rows_b or not rows_c:
                raise NotYet(f"{e['name']} / {b['name']}: items not ingested yet")
            ids_b = Counter(it["experimentItemId"] for it in rows_b)
            ids_c = Counter(it["experimentItemId"] for it in rows_c)
            if max(ids_c.values()) > 1 or max(ids_b.values()) > 1:
                return False, (f"{e['name']} or its baseline ran an item twice: the same run name appends to the same "
                               "experiment (make the run name unique: GATE_RUN_ID / GITHUB_RUN_ATTEMPT)")
            if set(ids_c) != set(ids_b):
                if set(ids_c) < set(ids_b) or set(ids_b) < set(ids_c):
                    raise NotYet(f"{e['name']}: {len(ids_c)} items vs {len(ids_b)} in the baseline (still ingesting?)")
                return False, f"{e['name']} and its baseline ran different items"
            versions = {_stamp(it.get("experimentItemVersion")) for it in rows_b + rows_c}
            pinned = {_stamp(m) for m in (meta(e).get("dataset_version"), bm.get("dataset_version"))}
            if len(versions) != 1 or versions != pinned:
                return False, (f"{e['name']}: item versions {sorted(versions)[:3]} vs metadata dataset_version "
                               f"{sorted(pinned)}: run both on get_dataset(name, version=<pin>)")
            bases.add(b["id"])
            details.append(f"{label}: {len(ids_c)} items vs baseline {b['name']!r}")
        shared = " (one baseline run shared by both gates: cached)" if len(bases) == 1 else ""
        return True, "; ".join(details) + shared

    def reruns_check():
        details = []
        for label in (CANDIDATE_LABEL, clean_label):
            e = candidate(label)
            r, policy = recompute(e), policy_of(e)
            capped = len(r["suspects"]) > int(policy.get("max_rerun_items", 10 ** 6))
            want = 0 if not r["suspects"] or capped else int(policy.get("reruns", 0))
            for side in ("baseline", "candidate"):
                runs = reruns_of(e, side)
                for run in runs:
                    ids = {it["experimentItemId"] for it in items_of(run["id"])}
                    extra = ids - set(r["suspects"])
                    if extra:
                        return False, (f"{run['name']} re-ran {len(extra)} item(s) that did not regress: re-run ONLY "
                                       "the suspects (the first comparison's 'regressed')")
                    if ids != set(r["suspects"]):
                        raise NotYet(f"{run['name']}: {len(ids)}/{len(r['suspects'])} suspect(s) ingested")
                if len(runs) != want:
                    return False, (f"{label}: {len(runs)} {side} re-run(s) for {len(r['suspects'])} suspect(s); the "
                                   f"recorded policy {policy} means {want}")
            details.append(f"{label}: {len(r['suspects'])} suspect(s), {want} re-run(s) per side"
                           + (" (too many suspects: no re-run)" if capped else ""))
        return True, "; ".join(details)

    def recorded_check():
        details = []
        for label in (CANDIDATE_LABEL, clean_label):
            e = candidate(label)
            s = gate_score(e)
            sm = _dictish(s.get("metadata"))
            value = s.get("value")
            if value not in GATE_DECISIONS:
                return False, f"{e['name']}: {GATE_SCORE}={value!r} (want one of {sorted(GATE_DECISIONS)}, CATEGORICAL)"
            for k in ("git_sha", "regressed", "flaky", "exit_code", "policy", "reasons"):
                if k not in sm:
                    return False, f"{e['name']}: the {GATE_SCORE} score's metadata lacks {k!r}"
            if sm["git_sha"] != meta(e).get("git_sha"):
                return False, f"{GATE_SCORE} metadata git_sha {sm['git_sha']!r} != the experiment's {meta(e).get('git_sha')!r}"
            if str(sm["exit_code"]) != str(GATE_DECISIONS[value]):
                return False, f"{GATE_SCORE}={value} but exit_code={sm['exit_code']!r} (want {GATE_DECISIONS[value]})"
            if value != "ship" and not (s.get("comment") or "").strip():
                return False, f"{GATE_SCORE}={value} without a comment: say why (the reasons)"
            r = recompute(e)
            if value != r["decision"] or sorted(sm.get("regressed") or []) != sorted(r["regressed"]):
                return False, (f"{e['name']}: recorded {value} / regressed {sm.get('regressed')}, but the stored item "
                               f"scores give {r['decision']} / regressed {r['regressed']} (flaky {r['flaky']})")
            details.append(f"{label}: {value} ({len(r['regressed'])} confirmed, {len(r['flaky'])} flaky)")
        return True, "; ".join(details)

    def planted_check():
        e = candidate(CANDIDATE_LABEL)
        s = gate_score(e)
        r = recompute(e)
        info = item_info(e)
        login = [i for i in r["regressed"] if info.get(i, ({}, {}))[1].get("handoff") == LOGIN_TEAM]
        if not login:
            return False, (f"no CONFIRMED login regression on staging (suspects {r['suspects']}, flaky {r['flaky']}): "
                           "is staging the bad candidate (login -> billing)?")
        if s.get("value") != "block":
            return False, f"the gate recorded {s.get('value')!r} for staging, which regressed {len(login)} login item(s)"
        named = _dictish(s.get("metadata")).get("regressed") or []
        if not set(login) <= set(named):
            return False, f"the {GATE_SCORE} score names {named}, not the regressed login item(s) {login}"
        cases = ", ".join(str(info[i][0].get("case_id", i)) for i in login)
        return True, f"staging blocked (exit 1): {len(login)} confirmed login regression(s): {cases}"

    def clean_check():
        e = candidate(clean_label)
        s = gate_score(e)
        version = snap()["labels"].get(clean_label)
        others = {snap()["labels"].get(BASELINE_LABEL), snap()["labels"].get(CANDIDATE_LABEL)}
        if version in others:
            return False, f"{clean_label!r} is on v{version}, the same version as production or staging"
        if s.get("value") != "ship":
            return False, (f"the clean candidate was {s.get('value')!r}: {s.get('comment')!r} (a false block? look at "
                           "the flaky items and the policy)")
        r = recompute(e)
        return True, (f"{clean_label} v{version} shipped (exit 0): {r['n']} items, {len(r['suspects'])} suspect(s), "
                      f"{len(r['flaky'])} flaky, {len(r['improved'])} improved")

    return [
        Check("gate runs carry the CI metadata", metadata_check),
        Check("baseline = production on the same pinned dataset", baseline_check),
        Check("only the suspects were re-run, on both sides", reruns_check),
        Check("gate decision recorded on the candidate experiment", recorded_check),
        Check("planted: staging blocked, regressed login items named", planted_check),
        Check("clean candidate shipped", clean_check),
    ]


def check_module_10(run_tag: str, *, client=None, prefix: str = "", clean_label: str = "candidate",
                    retries: int = 12, **run_kwargs) -> bool:
    client = client or get_client()
    checks = module_10_checks(run_tag, client, prefix=prefix, clean_label=clean_label)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 11
ANALYTICS_FIELDS = "core,basic,usage,metrics,trace_context"
ANALYTICS_NAMES = {  # role -> name; only the human score is config-backed (so it carries the notebook's PREFIX)
    "resolved": "handoff_correct", "judge": "routing_judge", "human": "human_routing_ok", "feedback": "user_feedback",
    "dashboard": "agent-health",
}
ANALYTICS_WINDOW_VERSIONS = {"prev": "v1", "curr": "v2"}
ANALYTICS_SLOW_TOOLS = ("lookup_order", "lookup_shipment")
ANALYTICS_DRY_TAG = "dry-run"
LATENCY_REGRESSION_MS = 2000.0
ORG_KEY_ENV = ("LANGFUSE_ORG_PUBLIC_KEY", "LANGFUSE_ORG_SECRET_KEY")
BLOB_HINT = ("optional: blob storage integrations need an ORGANIZATION API key (project keys get 403 'projects:read "
             "permission is required'). The OSS plan has the scheduled-blob-exports entitlement, but OSS cannot issue "
             "org keys: creating one needs the Enterprise 'admin-api' entitlement, the 'Organization settings -> API "
             "keys' page is hidden, and every org-key call gets 403 (module 13). On Langfuse Cloud or an Enterprise "
             "license, put an org key in the repo-root .env as LANGFUSE_ORG_PUBLIC_KEY / LANGFUSE_ORG_SECRET_KEY and "
             "run TODO 5b; on this stack the public API pull (TODO 5a) is the export")
BATCH_HINT = ("optional: no UI batch export in MinIO yet. The stack ships with LANGFUSE_S3_BATCH_EXPORT_ENABLED=\"false\" "
              "(ch09/docker-compose.langfuse.yml). To try it: set it to \"true\" for langfuse-web and langfuse-worker, "
              "run docker compose --env-file .env -f ch09/docker-compose.langfuse.yml up -d (recreates the two "
              "containers, keeps all data), then Traces -> filter your run tag -> Export -> JSON")


def run_observations(client, tag: str, *, hours: int = WINDOW_HOURS, fields: str = ANALYTICS_FIELDS) -> list[dict]:
    """Every observation of every trace tagged `tag` (all pages), as plain camelCase dicts (enums -> values)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    flt = json.dumps([{"type": "arrayOptions", "column": "traceTags", "operator": "any of", "value": [tag]},
                      {"type": "datetime", "column": "startTime", "operator": ">=", "value": since}])
    out, cursor = [], None
    for _ in range(50):
        resp = client.api.observations.get_many(fields=fields, filter=flt, limit=1000, cursor=cursor)
        out += [_row(o) for o in resp.data]
        cursor = getattr(getattr(resp, "meta", None), "cursor", None)
        if not cursor:
            return out
    raise RuntimeError(f"observations tagged {tag!r}: more than 50 pages")


def metrics_query(client, view: str, metrics, *, frm, to, dimensions=(), filters=(), order_by=None,
                  row_limit=None) -> list[dict]:
    """verify's own Metrics API v2 call (independent of the learner's). `metrics` = [(measure, aggregation)]."""
    body = {"view": view, "metrics": [{"measure": m, "aggregation": a} for m, a in metrics],
            "dimensions": [{"field": d} for d in dimensions], "filters": list(filters),
            "fromTimestamp": _as_datetime(frm).isoformat(), "toTimestamp": _as_datetime(to).isoformat()}
    if order_by:
        body["orderBy"] = [{"field": f, "direction": d} for f, d in order_by]
    if row_limit:
        body["config"] = {"row_limit": row_limit}
    return list(client.api.metrics.metrics(query=json.dumps(body)).data)


def _tag_filter(tag: str) -> dict:
    return {"column": "tags", "operator": "any of", "value": [tag], "type": "arrayOptions"}


def _in_window(ts, window) -> bool:
    ts = _as_datetime(ts)
    return ts is not None and window[0] <= ts < window[1]


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1")
    return v is not None and bool(v)


def _near(a, b, *, rel: float = 0.01, abs_tol: float = 1e-9) -> bool:
    if a is None or b is None:
        return (a or 0) == (b or 0)
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b)))


def _p(v) -> str:
    return "None" if v is None else f"{v:.6g}" if isinstance(v, float) else str(v)


def _filter_dicts(filters) -> list[dict]:
    return [{k: _plain(v) for k, v in (f.dict() if hasattr(f, "dict") else dict(f)).items() if v is not None}
            for f in filters or ()]


def module_11_checks(run_tag: str, client, *, report: dict | None, export_dir=None, prefix: str = "",
                     environment: str = "dev", allow_dry: bool = False, expected_per_window: int = 7) -> list[Check]:
    """Analytics checks over every observation tagged `run_tag` (two windows of the same requests: `prev` on v1, `curr`
    on v2 with the planted slow_tool), the learner's weekly `report` (weekly-report.json), the agent-health dashboard,
    the JSONL export in `export_dir`, and the optional blob storage / UI batch exports.
    `allow_dry`: accept traffic tagged dry-run (rehearsals with the oracle LLM; never for the graded run)."""
    from . import dashboard_spec as spec_mod
    from . import s3

    names = dict(ANALYTICS_NAMES, human=prefix + ANALYTICS_NAMES["human"], dashboard=prefix + spec_mod.DASHBOARD)
    expected = 2 * expected_per_window

    def fetch():
        obs = run_observations(client, run_tag)
        roots = [o for o in obs if o.get("isRootObservation") and o.get("name") == spec_mod.ROOT_NAME]
        if len(roots) < expected:
            raise NotYet(f"{len(roots)}/{expected} support-request roots tagged {run_tag!r} ingested so far")
        tids = sorted({r["traceId"] for r in roots})
        obs = [o for o in obs if o["traceId"] in tids]
        dashboards = [d for d in _paged(client.api.unstable.dashboards.list, "dashboards")
                      if d.get("name") == names["dashboard"]]
        dashboard = _row(client.api.unstable.dashboards.get(dashboards[0]["id"])) if len(dashboards) == 1 else None
        return {"obs": obs, "roots": roots, "scores": {t: scores_for(client, trace_id=t) for t in tids},
                "dashboards": dashboards, "dashboard": dashboard,
                "widgets": _paged(client.api.unstable.dashboard_widgets.list, "dashboard widgets")}

    snapshot = RunSnapshot(fetch, fields=ANALYTICS_FIELDS)

    def windows() -> dict:
        if not isinstance(report, dict) or not isinstance(report.get("windows"), dict):
            raise AssertionError("no weekly report: pass report=json.load(weekly-report.json) from the weekly pull")
        out = {}
        for w in ANALYTICS_WINDOW_VERSIONS:
            r = report["windows"].get(w) or {}
            try:
                out[w] = (_as_datetime(r["from"]), _as_datetime(r["to"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise AssertionError(f"report.windows.{w} needs ISO 'from'/'to' ({exc})") from None
        return out

    def window_of(o, wins):
        return next((w for w, win in wins.items() if _in_window(o.get("startTime"), win)), None)

    def truth(w: str) -> dict:
        """What the stored data says about window w: roots, observations, scores, per-trace cost, resolved ids."""
        s, wins = snapshot.get(), windows()
        win = wins[w]
        roots = [r for r in s["roots"] if _in_window(r["startTime"], win)]
        tids = {r["traceId"] for r in roots}
        obs = [o for o in s["obs"] if _in_window(o.get("startTime"), win)]
        cost = {t: 0.0 for t in tids}
        for o in obs:
            if o["traceId"] in cost:
                cost[o["traceId"]] += o.get("totalCost") or 0.0
        level = {r["traceId"]: str(r.get("level") or "DEFAULT") for r in roots}
        latest = {t: _latest([x for x in s["scores"].get(t, []) if x.get("name") == names["resolved"]]) for t in tids}
        resolved = {t for t in tids if level[t] == "DEFAULT" and latest[t] is not None
                    and _truthy(latest[t].get("value"))}
        scores = [x for t in tids for x in s["scores"].get(t, []) if _in_window(x.get("timestamp"), win)]
        return {"win": win, "roots": roots, "tids": tids, "obs": obs, "cost": cost, "level": level,
                "resolved": resolved, "scores": scores, "root_by_id": {r["traceId"]: r for r in roots}}

    def learner(w: str, key: str = "metrics"):
        v = (report["windows"].get(w) or {}).get(key)
        if not isinstance(v, (dict, list)):
            raise AssertionError(f"report.windows.{w}.{key} is missing")
        return v

    # 1 ------------------------------------------------------------------------------------------------
    def traffic_check():
        s, wins = snapshot.get(), windows()
        if not wins["prev"][1] <= wins["curr"][0]:
            return False, f"the report's windows overlap: prev ends {wins['prev'][1]}, curr starts {wins['curr'][0]}"
        problems, per = [], Counter()
        for r in s["roots"]:
            tags = set(r.get("tags") or [])
            tagged = next((w for w in ANALYTICS_WINDOW_VERSIONS if f"window:{w}" in tags), None)
            w = window_of(r, wins)
            per[w] += 1
            if ANALYTICS_DRY_TAG in tags and not allow_dry:
                return False, ("this run used the dry-run oracle LLM (tag dry-run): set DRY_RUN = False and re-run the "
                               "notebook with a new RUN_TAG for the graded run")
            if w is None or w != tagged:
                problems.append(f"root {r['traceId'][:8]} (tag window:{tagged}) starts {r['startTime']}, "
                                f"in report window {w}")
            elif r.get("version") != ANALYTICS_WINDOW_VERSIONS[w]:
                problems.append(f"root {r['traceId'][:8]} in {w} has version {r.get('version')!r}, "
                                f"expected {ANALYTICS_WINDOW_VERSIONS[w]}")
            if ("slow_tool" in tags) != (w == "curr"):
                problems.append(f"root {r['traceId'][:8]} in {w}: slow_tool tag "
                                f"{'present' if 'slow_tool' in tags else 'missing'}")
            if r.get("environment") != environment:
                problems.append(f"root {r['traceId'][:8]} environment {r.get('environment')!r} != {environment!r}")
        for w in ANALYTICS_WINDOW_VERSIONS:
            if per[w] != expected_per_window:
                problems.append(f"{per[w]} requests in window {w}, expected {expected_per_window}")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"{per['prev']} requests on v1 then {per['curr']} on v2 (slow_tool), each inside its report "
                      f"window, environment {environment}")

    # 2 ------------------------------------------------------------------------------------------------
    def metrics_check():
        errors, summary = [], []
        for w in ANALYTICS_WINDOW_VERSIONS:
            m, t = learner(w), truth(w)
            n = len(t["roots"])
            levels = Counter(t["level"].values())

            def expect(key, want, *, rel=0.01, missing_is_zero=False):
                if key not in m:
                    if not (missing_is_zero and not want):
                        errors.append(f"{w}: metrics[{key!r}] missing (see the key list in analytics.py)")
                elif not _near(m[key], want, rel=rel):
                    errors.append(f"{w}: {key} = {_p(m[key])}, stored data says {_p(want)}")

            expect("requests", n, rel=0)
            expect("error_rate", None if not n else 1 - levels.get("DEFAULT", 0) / n, rel=1e-6)
            expect("level_share.WARNING", levels.get("WARNING", 0) / n if n else None, rel=1e-6,
                   missing_is_zero=True)
            expect("cost_usd.total", sum(o.get("totalCost") or 0.0 for o in t["obs"]))
            for name, source in ((names["resolved"], "API"), (names["human"], "ANNOTATION")):
                vals = [float(_truthy(x.get("value"))) for x in t["scores"]
                        if x.get("name") == name and x.get("source") == source]
                if vals:
                    expect(f"score.{name}.{source}", sum(vals) / len(vals), rel=1e-6)
                else:
                    errors.append(f"{w}: no stored {name} ({source}) score in this window (the given scoring cell "
                                  "writes them)")
            rows = metrics_query(client, "observations", [("latency", "p95")], frm=t["win"][0], to=t["win"][1],
                                 dimensions=["name"], filters=[_tag_filter(run_tag),
                                                               {"column": "type", "operator": "=", "value": "TOOL",
                                                                "type": "string"}])
            p95 = {r.get("name"): r.get("p95_latency") for r in rows}
            for tool in ANALYTICS_SLOW_TOOLS:
                if tool in p95:
                    expect(f"latency_ms.tool.{tool}.p95", p95[tool], rel=0.02)
            costs = Counter()
            for tid, c in t["cost"].items():
                costs[t["root_by_id"][tid].get("userId")] += c
            top = report["windows"][w].get("top_users")
            if not isinstance(top, list) or not top:
                errors.append(f"{w}: top_users is empty (Metrics API: group by userId with row_limit + orderBy desc)")
            elif costs and max(costs.values()) > 0:
                best = max(costs.values())
                leaders = {u for u, c in costs.items() if _near(c, best, rel=1e-6)}
                if top[0].get("user_id") not in leaders:
                    errors.append(f"{w}: top user by cost is {sorted(leaders)}, report says {top[0].get('user_id')!r}")
            summary.append(f"{w}: {n} requests, error rate {_p(m.get('error_rate'))}, "
                           f"lookup_order p95 {_p(m.get('latency_ms.tool.lookup_order.p95'))} ms, "
                           f"cost ${_p(m.get('cost_usd.total'))}")
        if errors:
            return False, "; ".join(errors[:5])
        return True, " | ".join(summary)

    # 3 ------------------------------------------------------------------------------------------------
    def rollups_check():
        errors, summary = [], []
        for w in ANALYTICS_WINDOW_VERSIONS:
            m, t = learner(w), truth(w)
            for by, field_ in (("by_user", "userId"), ("by_session", "sessionId")):
                want: dict = {}
                for tid, root in t["root_by_id"].items():
                    row = want.setdefault(root.get(field_) or "none", {"requests": 0, "cost_usd": 0.0, "resolved": 0})
                    row["requests"] += 1
                    row["cost_usd"] += t["cost"][tid]
                    row["resolved"] += tid in t["resolved"]
                have = learner(w, by)
                if not isinstance(have, dict) or set(have) != set(want):
                    errors.append(f"{w}.{by}: keys {sorted(have)[:4]}... != stored {sorted(want)[:4]}...")
                    continue
                for k, row in want.items():
                    h = have[k] or {}
                    if h.get("requests") != row["requests"] or h.get("resolved") != row["resolved"] \
                            or not _near(h.get("cost_usd"), row["cost_usd"]):
                        errors.append(f"{w}.{by}[{k}] = {h}, stored data says {row}")
                        break
            n, total = len(t["tids"]), sum(t["cost"].values())
            ok = len(t["resolved"])
            want_m = {"resolved_rate": ok / n if n else None, "cost_usd.per_request": total / n if n else None,
                      "cost_usd.per_resolved": total / ok if ok else None}
            pairs = []
            for tid in t["tids"]:
                rows = snapshot.get()["scores"].get(tid, [])
                j = _latest([x for x in rows if x.get("name") == names["judge"] and is_judge_score(x)])
                h = _latest([x for x in rows if x.get("name") == names["human"] and x.get("source") == "ANNOTATION"])
                if j is not None and h is not None:
                    pairs.append(_truthy(j.get("value")) == _truthy(h.get("value")))
            want_m["judge_human.pairs"] = len(pairs)
            want_m["judge_human.agreement"] = sum(pairs) / len(pairs) if pairs else None
            for key, want in want_m.items():
                if key not in m:
                    errors.append(f"{w}: metrics[{key!r}] missing")
                elif not _near(m[key], want, rel=0.01 if key.startswith("cost_usd") else 1e-6):
                    errors.append(f"{w}: {key} = {_p(m[key])}, stored data says {_p(want)}")
            summary.append(f"{w}: {ok}/{n} resolved, ${_p(want_m['cost_usd.per_resolved'])} per resolved request, "
                           f"judge-human agreement {_p(want_m['judge_human.agreement'])} on {len(pairs)} pair(s)")
        if errors:
            return False, "; ".join(errors[:5])
        return True, " | ".join(summary)

    # 4 ------------------------------------------------------------------------------------------------
    def stored_widgets() -> dict:
        s = snapshot.get()
        return {spec["key"]: [w for w in s["widgets"] if w.get("name") == spec_mod.widget_name(spec, prefix)]
                for spec in spec_mod.WIDGETS}

    def dashboard_check():
        s = snapshot.get()
        if len(s["dashboards"]) != 1:
            return False, (f"{len(s['dashboards'])} dashboard(s) named {names['dashboard']!r}: TODO 4 must "
                           "get-or-create exactly one")
        d = s["dashboard"]
        want_filter = spec_mod.dashboard_filters(environment)[0]
        alt = {"column": "environment", "operator": "=", "type": "string", "value": environment}
        filters = _filter_dicts(d.get("filters"))
        errors = []
        if not (spec_mod.has_filter(filters, want_filter) or spec_mod.has_filter(filters, alt)):
            errors.append(f"dashboard filters {filters} do not restrict environment to {environment!r}")
        placed = Counter(p.get("widget_id") or p.get("widgetId")
                         for p in (d.get("definition") or {}).get("widgets") or [])
        for key, found in stored_widgets().items():
            spec = spec_mod.BY_KEY[key]
            if len(found) != 1:
                errors.append(f"{len(found)} widget(s) named {spec_mod.widget_name(spec, prefix)!r}"
                              + (" (get-or-create created duplicates)" if found else ""))
                continue
            w = dict(found[0], filters=_filter_dicts(found[0].get("filters")))
            problems = spec_mod.widget_problems(w, spec, prefix=prefix)
            if problems:
                errors.append(f"{key}: {'; '.join(problems)}")
            if placed[w["id"]] != 1:
                errors.append(f"{key}: placed {placed[w['id']]} time(s) on the dashboard (want 1)")
        if errors:
            return False, "; ".join(errors[:5])
        return True, (f"{names['dashboard']} ({d['id']}): {len(spec_mod.WIDGETS)} widgets match the spec, each placed "
                      f"once; dashboard filter environment = {environment}")

    # 5 ------------------------------------------------------------------------------------------------
    def widget_rows(w: dict, frm, to) -> list[dict]:
        d = snapshot.get()["dashboard"] or {}
        filters = _filter_dicts(w.get("filters")) + _filter_dicts(d.get("filters")) + [_tag_filter(run_tag)]
        return metrics_query(client, _plain(w["view"]), [(m["measure"], _plain(m["agg"])) for m in w["metrics"]],
                             frm=frm, to=to, dimensions=[x["field"] for x in w.get("dimensions") or ()],
                             filters=filters)

    def planted_dashboard_check():
        s, wins = snapshot.get(), windows()
        found = stored_widgets()
        if s["dashboard"] is None or len(found["tool_latency"]) != 1:
            return False, "needs the agent-health dashboard with its tool-latency widget (TODO 4)"
        frm, to = wins["prev"][0], datetime.now(timezone.utc)
        empty = []
        for key, ws in found.items():
            if len(ws) == 1:
                rows = widget_rows(ws[0], frm, to)
                first = f"{_plain(ws[0]['metrics'][0]['agg'])}_{ws[0]['metrics'][0]['measure']}"
                if key != "cost" and not any(r.get(first) is not None for r in rows):
                    empty.append(key)
        rows = widget_rows(found["tool_latency"][0], frm, to)
        p95 = {(r.get("name"), r.get("version")): r.get("p95_latency") for r in rows}
        gaps = {tool: (p95.get((tool, "v1")), p95.get((tool, "v2"))) for tool in ANALYTICS_SLOW_TOOLS}
        shown = {t: g for t, g in gaps.items() if None not in g and g[1] - g[0] >= LATENCY_REGRESSION_MS}
        detail = ", ".join(f"{t} p95 v1 {_p(a)} ms -> v2 {_p(b)} ms" for t, (a, b) in gaps.items())
        if empty:
            return False, f"widget(s) {empty} show no data for this run (check their filters); {detail}"
        if not shown:
            return False, f"the tool-latency widget does not show the slow_tool regression: {detail}"
        return True, f"tool-latency widget on this run: {detail}; every widget shows data"

    # 6 ------------------------------------------------------------------------------------------------
    def export_check():
        from pathlib import Path

        if export_dir is None:
            return False, "pass export_dir= (the folder TODO 5a wrote observations.jsonl + scores.jsonl to)"
        folder, s = Path(export_dir), snapshot.get()
        got = {}
        for name in ("observations.jsonl", "scores.jsonl"):
            path = folder / name
            if not path.exists():
                return False, f"{path} does not exist (TODO 5a)"
            try:
                got[name] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except ValueError as exc:
                return False, f"{path} is not JSON Lines: {exc}"
        obs_ids = {o.get("id") for o in got["observations.jsonl"] if isinstance(o, dict)}
        score_ids = {x.get("id") for x in got["scores.jsonl"] if isinstance(x, dict)}
        want_obs = {o["id"] for o in s["obs"]}
        want_scores = {x["id"] for rows in s["scores"].values() for x in rows}
        missing_o, missing_s = want_obs - obs_ids, want_scores - score_ids
        if missing_o or missing_s:
            return False, (f"export is incomplete: {len(missing_o)}/{len(want_obs)} observations and "
                           f"{len(missing_s)}/{len(want_scores)} scores of the run are missing (page with the cursor)")
        return True, (f"{len(got['observations.jsonl'])} observations + {len(got['scores.jsonl'])} scores in "
                      f"{folder} (every observation and score of the run)")

    # 7 ------------------------------------------------------------------------------------------------
    def blob_check():
        import os

        roots = snapshot.get()["roots"]
        project_id = roots[0].get("projectId") if roots else None
        keys = [os.getenv(k) for k in ORG_KEY_ENV]
        if not all(keys):
            try:
                client.api.blob_storage_integrations.get_blob_storage_integrations()
            except Exception as exc:  # 403 for project keys (verified live)
                if getattr(exc, "status_code", None) in (401, 403):
                    raise FeatureUnavailable(BLOB_HINT) from None
                raise
            raise FeatureUnavailable(BLOB_HINT)
        from langfuse.api import LangfuseAPI

        base = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "http://localhost:3000"
        org = LangfuseAPI(base_url=base, username=keys[0], password=keys[1])
        try:
            rows = [_row(x) for x in org.blob_storage_integrations.get_blob_storage_integrations().data]
        except Exception as exc:
            if getattr(exc, "status_code", None) in (401, 403):
                raise FeatureUnavailable(f"the org key was refused: {getattr(exc, 'body', exc)}") from None
            raise
        mine = [r for r in rows if project_id is None or r.get("projectId") == project_id]
        if not mine:
            return False, f"no blob storage integration for project {project_id} (TODO 5b)"
        r = mine[0]
        problems = []
        if _plain(r.get("type")) != "S3_COMPATIBLE":
            problems.append(f"type {_plain(r.get('type'))} (want S3_COMPATIBLE for MinIO)")
        if r.get("bucketName") != "langfuse" or not r.get("enabled"):
            problems.append(f"bucket {r.get('bucketName')!r}, enabled={r.get('enabled')}")
        if r.get("prefix") != s3.BLOB_EXPORT_PREFIX:
            problems.append(f"prefix {r.get('prefix')!r} (want {s3.BLOB_EXPORT_PREFIX!r}, away from media/ and events/)")
        if problems:
            return False, "; ".join(problems)
        try:
            files, note = s3.list_objects(s3.BLOB_EXPORT_PREFIX), ""
        except Exception as exc:
            files, note = [], f" (MinIO listing failed: {exc})"
        return True, (f"S3_COMPATIBLE -> langfuse/{r.get('prefix')} every {_plain(r.get('exportFrequency'))}; "
                      f"last sync {r.get('lastSyncAt')}; {len(files)} file(s) in MinIO so far{note}")

    # 8 ------------------------------------------------------------------------------------------------
    def batch_export_check():
        s = snapshot.get()
        started = min(_as_datetime(r["startTime"]) for r in s["roots"])
        try:
            files = [f for f in s3.list_objects(s3.BATCH_EXPORT_PREFIX)
                     if f["last_modified"] and f["last_modified"] >= started]
        except Exception as exc:
            raise FeatureUnavailable(f"MinIO not reachable ({exc}); {BATCH_HINT}") from None
        if not files:
            raise FeatureUnavailable(BATCH_HINT)
        return True, f"{len(files)} UI batch export file(s) in MinIO since the run started, e.g. {files[0]['key']}"

    # 9 ------------------------------------------------------------------------------------------------
    def planted_report_check():
        s, wins = snapshot.get(), windows()
        slow = Counter(window_of(o, wins) for o in s["obs"]
                       if o.get("type") == "TOOL" and o.get("name") in ANALYTICS_SLOW_TOOLS
                       and float(o.get("latency") or 0) >= SLOW_TOOL_SECONDS)
        if not slow.get("curr") or slow.get("prev"):
            return False, (f"the stored data does not show the planted slow_tool only in curr (slow lookup spans: "
                           f"{dict(slow)}): re-run the traffic cell")
        anomalies = report.get("anomalies") if isinstance(report, dict) else None
        if not isinstance(anomalies, list):
            return False, "report.anomalies is missing (build_report: analytics.flag)"
        wanted = {f"latency_ms.tool.{t}.p95" for t in ANALYTICS_SLOW_TOOLS} | {"latency_ms.request.p95"}
        hits = [a for a in anomalies if a.get("metric") in wanted and a.get("direction") == "up"]
        others = [a.get("metric") for a in anomalies if a not in hits]
        if not hits:
            return False, ("the weekly report did not flag the slow_tool latency regression (anomalies: "
                           f"{[a.get('metric') for a in anomalies]}): check analytics.flag / compare and that "
                           "health_metrics fills latency_ms.tool.<tool>.p95")
        detail = ", ".join(f"{a['metric']} {_p(a.get('prev'))} -> {_p(a.get('curr'))} ms" for a in hits)
        return True, f"flagged {detail}" + (f"; also flagged: {others}" if others else "")

    return [
        Check("analytics traffic: two windows (v1, then v2 with slow_tool)", traffic_check),
        Check("Metrics API numbers match the stored data", metrics_check),
        Check("client-side rollups match (per user/session, cost per resolved, judge vs human)", rollups_check),
        Check("agent-health dashboard matches the spec", dashboard_check),
        Check("planted: the dashboard shows the slow_tool regression (v2 vs v1)", planted_dashboard_check),
        Check("portable export (public API -> JSONL) is complete", export_check),
        Check("blob storage integration -> MinIO", blob_check),
        Check("UI batch export -> MinIO", batch_export_check),
        Check("planted: the weekly report flags the slow_tool regression", planted_report_check),
    ]


def check_module_11(run_tag: str, *, report: dict | None, export_dir=None, client=None, prefix: str = "",
                    environment: str = "dev", allow_dry: bool = False, retries: int = 12, **run_kwargs) -> bool:
    """`report`: the weekly pull's weekly-report.json (loaded); `export_dir`: TODO 5a's folder."""
    client = client or get_client()
    checks = module_11_checks(run_tag, client, report=report, export_dir=export_dir, prefix=prefix,
                              environment=environment, allow_dry=allow_dry)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 12
ALERTS_WINDOWS = ("A", "B", "C", "D")  # baseline, clean, deploy (bad prompt), after the rollback
ALERTS_TICKS = {"clean": ("A", "B"), "planted": ("A", "C"), "repeat": ("A", "C"), "recovered": ("A", "D")}
ALERTS_RULE = "routing-quality-drop"
ALERTS_METRIC = "score.handoff_correct.API"
ALERTS_MIN_DROP = 0.15  # the routing rule's min_abs (alerting.ALERT_RULES): the drop the stored data must show
ALERTS_DRY_TAG = "dry-run"
ALERTS_TICK_KEYS = ("requests", ALERTS_METRIC, "cost_usd.per_request", "latency_ms.request.p95")
SPEND_ALERTS_HINT = ("Langfuse Cloud's spend alerts (Organization settings -> Billing: an email when the organization's "
                     "bill crosses an amount) are a Cloud billing feature (entitlement `cloud-spend-alerts`, not in the "
                     "self-hosted `oss` plan). On this stack the cost guard rails are the native `spend-5m` monitor "
                     "(TODO 2) and the DIY `cost-per-request-up` rule")


def _envelope(text):
    try:
        v = json.loads(text) if isinstance(text, (str, bytes)) else text
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def receiver_rows(deliveries) -> list[dict]:
    """The webhook receiver's log (a JSONL path, or rows already read), oldest first, each row with its parsed
    `envelope` (None when the body is not a JSON object)."""
    from pathlib import PurePath

    from .webhook_receiver import read_log

    rows = read_log(deliveries) if isinstance(deliveries, (str, PurePath)) else list(deliveries or [])
    return [dict(r, envelope=_envelope(r.get("body"))) for r in rows]


def signed_with(row: dict, secret: str | None) -> bool:
    """True when the logged delivery carries a valid Langfuse-style signature of its exact body under `secret`
    (the HMAC; freshness was the receiver's job at receipt: a row it rejected has signature_ok False)."""
    from .webhook_receiver import SIGNATURE_HEADER, parse_signature, signature_ok

    header = (row.get("headers") or {}).get(SIGNATURE_HEADER)
    parsed = parse_signature(header)
    return bool(secret and parsed and row.get("signature_ok") is not False
                and signature_ok(row.get("body") or "", header, secret, now=parsed[0]))


def _accepted(row: dict) -> bool:
    return row.get("status") == 200 and not row.get("duplicate") and not row.get("planted_outage")


def _ms(d: datetime) -> datetime:
    return d.replace(microsecond=d.microsecond // 1000 * 1000)


def _ms_window(snap: dict) -> tuple:
    """(from, to) of a tick snapshot, floored to the millisecond the way the server compares them (verified live:
    start times are stored truncated to ms, so a trace started 0.3 ms after a window opened at 13.300512 is stored as
    13.300 and the Metrics API still counts it in the window)."""
    return _ms(_as_datetime(snap["from"])), _ms(_as_datetime(snap["to"]))


def module_12_checks(run_tag: str, client, *, ticks: dict | None, deliveries, diy_secret: str | None, triage=None,
                     prefix: str = "", environment: str = "dev", allow_dry: bool = False,
                     native_secrets: dict | None = None, expected_per_window: int = 4) -> list[Check]:
    """Alerting checks over every observation tagged `run_tag` (the same 4 requests in windows A, B, C (served by the bad
    prompt version after a deploy) and D (after the rollback)), the alert job's four tick reports `ticks`
    ({"clean": A vs B, "planted": A vs C, "repeat": A vs C again, "recovered": A vs D}: what alert_job.run_once
    returns), the webhook receiver's log `deliveries` (its JSONL path), the DIY signing secret, TODO 4's `triage` (trace
    ids or rows with "trace_id") and the native deliveries (when the stack may call this machine).
    `allow_dry`: accept traffic tagged dry-run (rehearsals with the oracle LLM; never for the graded run)."""
    import os

    from . import alert_spec, prompts

    names = alert_spec.names(prefix)
    prompt_name = prefix + prompts.PROMPT_NAME
    score = alert_spec.SCORE_NAME
    expected = len(ALERTS_WINDOWS) * expected_per_window
    secrets = {k: (native_secrets or {}).get(k) or os.getenv(env) for k, env in alert_spec.SECRET_ENV.items()}

    def fetch():
        obs = run_observations(client, run_tag)
        roots = [o for o in obs if o.get("isRootObservation") and o.get("name") == alert_spec.ROOT_NAME]
        if len(roots) < expected:
            raise NotYet(f"{len(roots)}/{expected} support-request roots tagged {run_tag!r} ingested so far")
        tids = sorted({r["traceId"] for r in roots})
        scores = {t: scores_for(client, trace_id=t) for t in tids}
        unscored = [t for t in tids if not any(x.get("name") == score for x in scores[t])]
        if unscored:
            raise NotYet(f"{len(unscored)}/{len(tids)} traces have no {score} score yet")
        return {"obs": [o for o in obs if o["traceId"] in tids], "roots": roots, "scores": scores,
                "prompt": prompt_versions(client, prompt_name)}

    snapshot = RunSnapshot(fetch, fields=ANALYTICS_FIELDS)

    # ------------------------------------------------------------ inputs
    def tick(name: str) -> dict:
        t = ticks.get(name) if isinstance(ticks, dict) else None
        if not isinstance(t, dict):
            raise AssertionError(f"no tick {name!r}: pass ticks={{'clean': ..., 'planted': ..., 'repeat': ..., "
                                 "'recovered': ...}} (the reports alert_job.run_once returns)")
        return t

    def windows() -> dict:
        out = {}
        for name, (base, cur) in ALERTS_TICKS.items():
            for side, w in (("baseline", base), ("current", cur)):
                snap = tick(name).get(side) or {}
                try:
                    win = _ms_window(snap)
                except (KeyError, TypeError, ValueError) as exc:
                    raise AssertionError(f"ticks[{name!r}][{side!r}] needs ISO 'from'/'to' (alert_job.snapshot): "
                                         f"{exc}") from None
                if out.setdefault(w, win) != win:
                    raise AssertionError(f"tick {name!r} uses {win[0]}..{win[1]} as window {w}, another tick "
                                         f"{out[w][0]}..{out[w][1]}: every tick compares with the same baseline A")
        return out

    def truth(w: str) -> dict:
        """What the stored data says about window w."""
        s, win = snapshot.get(), windows()[w]
        roots = [r for r in s["roots"] if _in_window(r["startTime"], win)]
        tids = {r["traceId"] for r in roots}
        cost = sum(o.get("totalCost") or 0.0 for o in s["obs"] if _in_window(o.get("startTime"), win))
        hc = [float(_truthy(x.get("value"))) for t in tids for x in s["scores"].get(t, [])
              if x.get("name") == score and x.get("source") == "API" and _in_window(x.get("timestamp"), win)]
        latest = {t: _latest([x for x in s["scores"].get(t, []) if x.get("name") == score]) for t in tids}
        levels = Counter(str(r.get("level") or "DEFAULT") for r in roots)
        versions = Counter(r.get("version") for r in roots)
        n = len(roots)
        return {"win": win, "roots": roots, "n": n, "versions": versions,
                "version": versions.most_common(1)[0][0] if versions else None,
                ALERTS_METRIC: sum(hc) / len(hc) if hc else None, "requests": n, "cost_usd.total": cost,
                "cost_usd.per_request": cost / n if n else None,
                "error_rate": 1 - levels.get("DEFAULT", 0) / n if n else None,
                "failing": {t for t, x in latest.items() if x is not None and not _truthy(x.get("value"))}}

    def stored(w: str, metric: str, cache: dict):
        """The stored value of one of the alert job's metric keys in window w ("unknown" if verify can't recompute)."""
        if (w, metric) in cache:
            return cache[(w, metric)]
        t = truth(w)
        base = [_tag_filter(run_tag), {"column": "environment", "operator": "=", "value": environment,
                                       "type": "string"}]
        tool = re.fullmatch(r"latency_ms\.tool\.(.+)\.p95", metric or "")
        if metric in t:
            value = t[metric]
        elif metric == "latency_ms.request.p95":
            rows = metrics_query(client, "observations", [("latency", "p95")], frm=t["win"][0], to=t["win"][1],
                                 filters=base + [{"column": "isRootObservation", "operator": "=", "value": True,
                                                  "type": "boolean"},
                                                 {"column": "name", "operator": "=", "value": alert_spec.ROOT_NAME,
                                                  "type": "string"}])
            value = rows[0].get("p95_latency") if rows else None
        elif tool:
            rows = metrics_query(client, "observations", [("latency", "p95")], frm=t["win"][0], to=t["win"][1],
                                 filters=base + [{"column": "type", "operator": "=", "value": "TOOL", "type": "string"},
                                                 {"column": "name", "operator": "=", "value": tool.group(1),
                                                  "type": "string"}])
            value = rows[0].get("p95_latency") if rows else None
        else:
            value = "unknown"
        cache[(w, metric)] = value
        return value

    def tolerance(metric: str) -> float:
        return 0.0 if metric == "requests" else 0.02 if str(metric).startswith(("latency", "cost")) else 1e-6

    def diy_rows() -> list[dict]:
        """This run's DIY deliveries (every attempt), oldest first."""
        return [r for r in receiver_rows(deliveries)
                if r.get("path") == alert_spec.DIY_PATH and (r["envelope"] or {}).get("type") == "agent-alert"
                and run_tag in ((r["envelope"].get("scope") or {}).get("tags") or [])]

    def sent(name: str) -> list[dict]:
        return [s for s in tick(name).get("sent") or [] if isinstance(s, dict)]

    def tick_ids() -> set:
        return {(s.get("payload") or {}).get("id") for name in ALERTS_TICKS for s in sent(name)}

    def planted_payload() -> dict:
        firing = [s["payload"] for s in sent("planted") if (s.get("payload") or {}).get("rule") == ALERTS_RULE
                  and s["payload"].get("status") == "firing"]
        if len(firing) != 1:
            raise AssertionError(f"tick 'planted' (A vs C) sent {len(firing)} firing {ALERTS_RULE} message(s), want 1 "
                                 f"(alerts: {[a.get('rule') for a in tick('planted').get('alerts') or []]})")
        return firing[0]

    def window_of(snap: dict) -> str | None:
        try:
            win = _ms_window(snap)
        except (KeyError, TypeError, ValueError):
            return None
        return next((w for w, x in windows().items() if x == win), None)

    def drop(w: str) -> float | None:
        a, x = truth("A")[ALERTS_METRIC], truth(w)[ALERTS_METRIC]
        return None if a is None or x is None else a - x

    # 1 ------------------------------------------------------------------------------------------------
    def traffic_check():
        s, wins = snapshot.get(), windows()
        problems, per = [], Counter()
        for a, b in zip(ALERTS_WINDOWS, ALERTS_WINDOWS[1:]):
            if not wins[a][1] <= wins[b][0]:
                problems.append(f"windows {a} and {b} overlap")
        for r in s["roots"]:
            tags = set(r.get("tags") or [])
            if ALERTS_DRY_TAG in tags and not allow_dry:
                return False, ("this run used the dry-run oracle LLM (tag dry-run): set DRY_RUN = False and re-run the "
                               "notebook with a new RUN_TAG for the graded run")
            tagged = next((w for w in ALERTS_WINDOWS if f"window:{w}" in tags), None)
            w = next((w for w, win in wins.items() if _in_window(r["startTime"], win)), None)
            per[w] += 1
            rid = r["traceId"][:8]
            if w is None or w != tagged:
                problems.append(f"root {rid} (tag window:{tagged}) starts {r['startTime']}, in tick window {w}")
            if names["tag"] not in tags:
                problems.append(f"root {rid} lacks the stable tag {names['tag']!r} (the native monitors filter on it)")
            if (PLANTED_TAG in tags) != (w == "C"):
                problems.append(f"root {rid} in {w}: {PLANTED_TAG} tag {'present' if PLANTED_TAG in tags else 'missing'}")
            if r.get("environment") != environment:
                problems.append(f"root {rid} environment {r.get('environment')!r} != {environment!r}")
            if alert_spec.prompt_version_of(r.get("version")) is None:
                problems.append(f"root {rid} version {r.get('version')!r} is not '{alert_spec.VERSION_PREFIX}<n>'")
        for w in ALERTS_WINDOWS:
            if per[w] != expected_per_window:
                problems.append(f"{per[w]} requests in window {w}, expected {expected_per_window}")
        if problems:
            return False, "; ".join(problems[:4])
        t = {w: truth(w) for w in ALERTS_WINDOWS}
        mixed = {w: dict(x["versions"]) for w, x in t.items() if len(x["versions"]) != 1}
        if mixed:
            return False, f"window(s) mix versions {mixed}: deploy and roll back BETWEEN windows"
        good, bad = (alert_spec.prompt_version_of(t[w]["version"]) for w in ("A", "C"))
        if not t["A"]["version"] == t["B"]["version"] == t["D"]["version"]:
            return False, (f"A, B and D must be served by the same version: {t['A']['version']}, {t['B']['version']}, "
                           f"{t['D']['version']} (the rollback moves `live` back to A's version)")
        if good == bad:
            return False, (f"C was served by {t['C']['version']} like A: the deploy (move `live` to the bad version) "
                           "did not happen before window C")
        versions = s["prompt"]
        if (versions.get(good) or {}).get("prompt") != prompts.SYSTEM_PROMPT_TEMPLATE \
                or (versions.get(bad) or {}).get("prompt") != prompts.WRONG_HANDOFF_TEMPLATE:
            return False, (f"{prompt_name} v{good} must be the good template and v{bad} the bad one (login -> billing): "
                           "the given prompt cell gets them by content")
        live = sorted(v for v, row in versions.items() if alert_spec.LIVE_LABEL in (row.get("labels") or []))
        if live != [good]:
            return False, f"label {alert_spec.LIVE_LABEL!r} is on {live}, expected [{good}]: roll back after window C"
        return True, (f"{expected_per_window} requests in each of A, B, C, D (environment {environment}); A/B/D served by "
                      f"{prompt_name} v{good}, C by v{bad} (login -> billing, tag {PLANTED_TAG}); "
                      f"`{alert_spec.LIVE_LABEL}` is back on v{good}")

    # 2 ------------------------------------------------------------------------------------------------
    def numbers_check():
        errors, cache = [], {}
        for name, (base, cur) in ALERTS_TICKS.items():
            for side, w in (("baseline", base), ("current", cur)):
                snap = tick(name).get(side) or {}
                m = snap.get("metrics") or {}
                for key in ALERTS_TICK_KEYS:
                    want = stored(w, key, cache)
                    if key not in m:
                        errors.append(f"tick {name}: {side}.metrics[{key!r}] missing (alert_job.snapshot)")
                    elif not _near(m[key], want, rel=tolerance(key)):
                        errors.append(f"tick {name} ({side} = {w}): {key} = {_p(m[key])}, stored data says {_p(want)}")
                if snap.get("version") != truth(w)["version"]:
                    errors.append(f"tick {name} ({side} = {w}): version {snap.get('version')!r}, the stored traces "
                                  f"say {truth(w)['version']!r}")
        if errors:
            return False, "; ".join(errors[:5])
        return True, " | ".join(f"{w}: {truth(w)['n']} requests, pass rate {_p(stored(w, ALERTS_METRIC, cache))}, "
                                f"${_p(stored(w, 'cost_usd.per_request', cache))}/request, request p95 "
                                f"{_p(stored(w, 'latency_ms.request.p95', cache))} ms, {truth(w)['version']}"
                                for w in ALERTS_WINDOWS)

    # 3 ------------------------------------------------------------------------------------------------
    def quiet_check():
        for w in ("B", "D"):
            d = drop(w)
            if d is not None and d >= ALERTS_MIN_DROP:
                return False, (f"the stored data is not clean: window {w}'s pass rate is {_p(d)} below A's on the good "
                               "prompt (an agent flake, not your job's fault): re-run the notebook with a new RUN_TAG")
        clean = tick("clean")
        problems = []
        if clean.get("alerts") or sent("clean"):
            problems.append(f"tick clean (A vs B) raised {[a.get('rule') for a in clean.get('alerts') or []]} and sent "
                            f"{len(sent('clean'))} message(s): B is as healthy as A, nothing may page")
        if any((s.get("payload") or {}).get("status") == "firing" for s in sent("recovered")):
            problems.append("tick recovered (A vs D) sent a firing message: after the rollback only 'resolved' is due")
        loud = [r for r in diy_rows() if r["envelope"].get("status") == "firing" and r.get("status") == 200
                and window_of(r["envelope"].get("current") or {}) in ("B", "D")]
        if loud:
            problems.append(f"{len(loud)} firing delivery(ies) for a clean window: {loud[0]['envelope'].get('title')}")
        if problems:
            return False, "; ".join(problems)
        return True, ("tick clean (A vs B): 0 alerts, nothing sent; tick recovered (A vs D): no firing message; no "
                      "firing delivery for window B or D")

    # 4 ------------------------------------------------------------------------------------------------
    def planted_check():
        a, c, d = truth("A"), truth("C"), drop("C")
        if d is None or d < ALERTS_MIN_DROP:
            return False, (f"the stored data shows no routing drop in C (pass rate A {_p(a[ALERTS_METRIC])} -> C "
                           f"{_p(c[ALERTS_METRIC])}): the deploy did not take effect or the bad version routed "
                           "correctly this time; re-run the notebook with a new RUN_TAG")
        ids = tick_ids()
        rows = [r for r in diy_rows() if r["envelope"].get("id") in ids]
        accepted = [r for r in rows if _accepted(r) and r["envelope"].get("status") == "firing"]
        mine = [r for r in accepted if r["envelope"].get("rule") == ALERTS_RULE]
        if len(mine) != 1:
            return False, (f"{len(mine)} accepted firing deliveries of {ALERTS_RULE} at {alert_spec.DIY_PATH} (want "
                           f"exactly 1; {len(rows)} attempt(s) logged for the ticks' payloads): check deliver and the "
                           "dedup in decide")
        r, p = mine[0], mine[0]["envelope"]
        problems = []
        if not signed_with(r, diy_secret):
            problems.append("the signature does not verify with the DIY secret (sign the exact body bytes you send: "
                            "regression.sign(body, secret, int(clock())), header x-langfuse-signature)")
        if p.get("metric") != ALERTS_METRIC:
            problems.append(f"metric {p.get('metric')!r}, want {ALERTS_METRIC!r}")
        for side, w in (("baseline", "A"), ("current", "C")):
            got = p.get(side) or {}
            if window_of(got) != w:
                problems.append(f"{side} window {got.get('from')}..{got.get('to')} is not window {w}")
            if not _near(got.get("value"), truth(w)[ALERTS_METRIC], rel=1e-6):
                problems.append(f"{side} value {_p(got.get('value'))}, stored pass rate of {w} is "
                                f"{_p(truth(w)[ALERTS_METRIC])}")
        if p.get("version") != c["version"]:
            problems.append(f"version {p.get('version')!r}, C was served by {c['version']!r}")
        cache, also = {}, []
        for o in accepted:
            q = o["envelope"]
            if q.get("rule") == ALERTS_RULE:
                continue
            metric, cur_w = q.get("metric"), window_of(q.get("current") or {})
            if cur_w is None:
                problems.append(f"{q.get('rule')}: its current window is not one of the run's windows")
                continue
            vals = {s: stored(w, metric, cache) for s, w in (("baseline", "A"), ("current", cur_w))}
            if "unknown" in vals.values():
                also.append(f"{q.get('rule')} ({metric}, not re-checked)")
            elif all(_near((q.get(s) or {}).get("value"), v, rel=tolerance(metric)) for s, v in vals.items()):
                also.append(f"{q.get('rule')} ({metric} {_p(vals['baseline'])} -> {_p(vals['current'])}, the stored "
                            "data agrees)")
            else:
                problems.append(f"{q.get('rule')}: {metric} {(q.get('baseline') or {}).get('value')} -> "
                                f"{(q.get('current') or {}).get('value')}, stored data says {_p(vals['baseline'])} -> "
                                f"{_p(vals['current'])}")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"exactly one signed firing delivery: {p.get('title') or p.get('rule')}"
                      + (f"; also fired: {', '.join(also)}" if also else ""))

    # 5 ------------------------------------------------------------------------------------------------
    def retry_check():
        p = planted_payload()
        attempts = [r for r in diy_rows() if r["envelope"].get("id") == p.get("id")]
        ok = [i for i, r in enumerate(attempts) if _accepted(r)]
        if not ok:
            return False, f"the planted firing message {p.get('id')} never got a 200 from the receiver"
        outages = [r for r in attempts[:ok[0]] if r.get("planted_outage")]
        if not outages:
            return False, ("no failed attempt before the accepted one: the notebook plants one outage (RX.fail_next) "
                           "before tick 2, so deliver must get a 503 first and retry")
        if len({r.get("body") for r in attempts}) != 1:
            return False, "the attempts sent different bodies: build the body once, so every retry carries the same bytes"
        d = next(((s.get("delivery") or {}) for s in sent("planted") if (s.get("payload") or {}).get("id") == p["id"]),
                 {})
        if not d.get("ok") or (d.get("attempts") or 0) < ok[0] + 1:
            return False, f"tick planted reports delivery {d} for it, the receiver logged {len(attempts)} attempt(s)"
        return True, (f"{len(outages)} planted 503(s), then accepted on attempt {ok[0] + 1}; same id and body on every "
                      f"attempt ({len(attempts)} logged, {sum(bool(r.get('duplicate')) for r in attempts)} duplicate)")

    # 6 ------------------------------------------------------------------------------------------------
    def cooldown_check():
        fp = planted_payload().get("fingerprint")
        rep = tick("repeat")
        if sent("repeat"):
            titles = [(s.get("payload") or {}).get("title") for s in sent("repeat")]
            return False, (f"tick repeat (A vs C again) sent {titles}: within the cooldown a firing incident is "
                           "suppressed, not sent again")
        if fp not in (rep.get("suppressed") or []):
            return False, f"tick repeat did not report {fp} as suppressed (suppressed: {rep.get('suppressed')})"
        st = (rep.get("state") or {}).get(fp) or {}
        if st.get("status") != "firing" or st.get("seq") != 1:
            return False, f"after tick repeat the state of {fp} is {st}: still firing, seq 1, last_sent unchanged"
        return True, f"tick repeat (the cron-style process): 0 sent, {fp} suppressed by the cooldown, state seq 1"

    # 7 ------------------------------------------------------------------------------------------------
    def resolve_check():
        d = drop("D")
        if d is not None and d >= ALERTS_MIN_DROP:
            return False, f"the stored data shows D still {_p(d)} below A: the rollback did not take effect"
        p = planted_payload()
        fp = p.get("fingerprint")
        rows = [r for r in diy_rows() if r["envelope"].get("id") in tick_ids()]
        firing = [r for r in rows if _accepted(r) and r["envelope"].get("id") == p.get("id")]
        resolved = [r for r in rows if _accepted(r) and r["envelope"].get("fingerprint") == fp
                    and r["envelope"].get("status") == "resolved"]
        if len(resolved) != 1:
            return False, f"{len(resolved)} accepted 'resolved' deliveries for {fp} (want 1, from tick recovered)"
        r, q = resolved[0], resolved[0]["envelope"]
        problems = []
        if firing and str(r.get("received_at")) <= str(firing[0].get("received_at")):
            problems.append("the resolved message arrived before the firing one")
        if not signed_with(r, diy_secret):
            problems.append("its signature does not verify with the DIY secret")
        if window_of(q.get("current") or {}) != "D":
            problems.append("its current window is not D (the resolve shows today's healthy numbers)")
        if not _near((q.get("current") or {}).get("value"), truth("D")[ALERTS_METRIC], rel=1e-6):
            problems.append(f"current value {(q.get('current') or {}).get('value')}, stored pass rate of D is "
                            f"{_p(truth('D')[ALERTS_METRIC])}")
        if q.get("since") != p.get("since"):
            problems.append(f"since {q.get('since')} != the incident's {p.get('since')}")
        st = (tick("recovered").get("state") or {}).get(fp) or {}
        if st.get("status") != "resolved":
            problems.append(f"after tick recovered the state of {fp} is {st.get('status')!r}, want 'resolved'")
        if problems:
            return False, "; ".join(problems)
        return True, (f"one signed resolved delivery after the firing one: {q.get('title') or q.get('rule')} "
                      f"(incident since {q.get('since')})")

    # 8 ------------------------------------------------------------------------------------------------
    def triage_check():
        if triage is None:
            return False, "pass triage=TRIAGE (TODO 4: the failing traces of the firing alert's window)"
        got = {x if isinstance(x, str) else (x.get("trace_id") or x.get("traceId")) for x in triage}
        want = truth("C")["failing"]
        if not want:
            return False, "the stored data has no failing trace in window C (see the planted check)"
        missing, extra = want - got, got - want
        if missing or extra:
            return False, (f"triage returned {len(got)} trace(s), the stored failing traces of C are {len(want)}: "
                           f"missing {sorted(t[:8] for t in missing)}, not failing or outside C "
                           f"{sorted(str(t)[:8] for t in extra)} (latest {score} False, roots in the alert's window)")
        return True, f"{len(got)} failing trace(s) of window C, the same as the stored data: {sorted(t[:8] for t in got)}"

    # 9-10 ---------------------------------------------------------------------------------------------
    def native_rows(path: str, kind: str) -> list[dict]:
        ready, _ = alert_spec.native_webhooks_ready()
        if not ready:
            raise FeatureUnavailable(alert_spec.NATIVE_HINT)
        started = min(_as_datetime(r["startTime"]) for r in snapshot.get()["roots"]) - timedelta(minutes=5)
        return [r for r in receiver_rows(deliveries) if r.get("path") == path and _accepted(r)
                and (r["envelope"] or {}).get("type") == kind and _as_datetime(r.get("received_at")) >= started]

    def native_monitor_check():
        name = names["monitor.routing"]
        rows = [r for r in native_rows(alert_spec.MONITOR_PATH, "monitor-alert")
                if (r["envelope"].get("payload") or {}).get("monitorName") == name]
        c = windows()["C"]
        alerts = [r for r in rows if r["envelope"]["payload"].get("severity") == "ALERT"
                  and _as_datetime(r["envelope"]["payload"].get("fromTimestamp")) <= c[1]
                  and _as_datetime(r["envelope"]["payload"].get("toTimestamp")) >= c[0]]
        if not alerts:
            return False, (f"no ALERT from monitor {name!r} covering window C reached {alert_spec.MONITOR_PATH} "
                           f"({len(rows)} other event(s) from it): is it saved with the automation "
                           f"{names['automation.monitor']!r}, filtered on tag {names['tag']!r}, and were the "
                           "containers recreated after the compose change? It evaluates once a minute")
        r, pl = alerts[0], alerts[0]["envelope"]["payload"]
        if not secrets["monitor"]:
            return False, f"set {alert_spec.SECRET_ENV['monitor']} in .env (the automation's signing secret)"
        if not signed_with(r, secrets["monitor"]):
            return False, f"the monitor delivery's signature does not verify with {alert_spec.SECRET_ENV['monitor']}"
        return True, (f"{name}: ALERT at {pl.get('alertedAt')}, {pl.get('metricValue')} {pl.get('thresholdOperator')} "
                      f"{pl.get('alertThreshold')} over {pl.get('fromTimestamp')}..{pl.get('toTimestamp')}, signed "
                      f"({len(alerts)} ALERT delivery(ies))")

    def native_prompt_check():
        rows = [r for r in native_rows(alert_spec.PROMPT_PATH, "prompt-version")
                if (r["envelope"].get("prompt") or {}).get("name") == prompt_name]
        good, bad = (alert_spec.prompt_version_of(truth(w)["version"]) for w in ("A", "C"))

        def live_on(r, v):
            p = r["envelope"].get("prompt") or {}
            return p.get("version") == v and alert_spec.LIVE_LABEL in (p.get("labels") or [])

        deploy = [r for r in rows if live_on(r, bad)]
        rollback = [r for r in rows if live_on(r, good) and deploy
                    and str(r.get("received_at")) > str(deploy[0].get("received_at"))]
        if not deploy or not rollback:
            return False, (f"prompt events for {prompt_name}: deploy (v{bad} gains {alert_spec.LIVE_LABEL!r}) "
                           f"{'seen' if deploy else 'missing'}, rollback (v{good} regains it) "
                           f"{'seen' if rollback else 'missing'} ({len(rows)} event(s) at {alert_spec.PROMPT_PATH}): "
                           f"check the automation {names['automation.prompt']!r} (Prompt versions, action updated)")
        if not secrets["prompt"]:
            return False, f"set {alert_spec.SECRET_ENV['prompt']} in .env (the automation's signing secret)"
        if not all(signed_with(r, secrets["prompt"]) for r in (deploy[0], rollback[0])):
            return False, f"a prompt delivery's signature does not verify with {alert_spec.SECRET_ENV['prompt']}"
        return True, (f"deploy (v{bad} -> {alert_spec.LIVE_LABEL}) and rollback (v{good}) both delivered, signed "
                      f"({len(rows)} prompt event(s))")

    def spend_check():
        raise FeatureUnavailable(SPEND_ALERTS_HINT)

    return [
        Check("alert traffic: windows A, B, C (deploy: bad prompt), D (rollback)", traffic_check),
        Check("the alert job's numbers match the stored data", numbers_check),
        Check("clean windows stay quiet", quiet_check),
        Check("planted: the routing drop fires exactly once, signed, with the right numbers", planted_check),
        Check("delivery survives the planted receiver outage (retry, same id)", retry_check),
        Check("dedup + cooldown: the repeat tick sends nothing", cooldown_check),
        Check("resolve: one 'resolved' message after the rollback", resolve_check),
        Check("triage: the alert leads to the failing traces", triage_check),
        Check("native score monitor -> webhook", native_monitor_check),
        Check("native prompt webhook on deploy and rollback", native_prompt_check),
        Check("Langfuse Cloud spend alerts", spend_check),
    ]


def check_module_12(run_tag: str, *, ticks: dict | None, deliveries, diy_secret: str | None, triage=None, client=None,
                    prefix: str = "", environment: str = "dev", allow_dry: bool = False,
                    native_secrets: dict | None = None, retries: int = 12, **run_kwargs) -> bool:
    """`ticks`: the alert job's four reports; `deliveries`: the receiver's JSONL log; `triage`: TODO 4's result."""
    client = client or get_client()
    checks = module_12_checks(run_tag, client, ticks=ticks, deliveries=deliveries, diy_secret=diy_secret,
                              triage=triage, prefix=prefix, environment=environment, allow_dry=allow_dry,
                              native_secrets=native_secrets)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 13
OPS_FIELDS = ANALYTICS_FIELDS + ",prompt"
OPS_DRY_TAG = "dry-run"
OPS_PROD_DEPLOYMENT, OPS_STAGING_DEPLOYMENT = "support-api@production", "support-api@staging"
OPS_LATENCY_SLACK_MS = 5.0  # an outage may add at most this (or 4x) to the request path's p95
OPS_REPORT_KEYS = ("requests", "attempts", "batches", "failed_attempts", "dropped_batches", "spans_exported",
                   "delivered", "partial", "lost", "ok")
RETENTION_HINT = ("native data retention is Enterprise (entitlement `data-retention`; plan `oss` has only trace-deletion, "
                  "scheduled-blob-exports, in-app-agent): Project settings -> Data retention is not offered, and "
                  "projects.update(retention=) needs an organization key, which OSS cannot issue. Live: this project "
                  "has retention_days={days} (keep forever). The OSS equivalent is your retention job (ops.expired + "
                  "delete_traces), checked above")
ORG_API_HINT = ("organization-level administration is Enterprise on self-hosted (entitlement `admin-api`): organization "
                "API keys cannot be issued on OSS (the 'Organization settings -> API keys' page is hidden and the "
                "create call is refused), and every org-key request is rejected with 403 'This feature is not "
                "available on your current plan'. That rules out the org management API (projects, project API keys, "
                "memberships) and SCIM; project-level roles (`rbac-project-roles`), audit logs (`audit-logs`) and "
                "protected prompt labels (`prompt-protected-labels`) are Enterprise too. Live, with the project key: "
                "{live}. On OSS manage them in the UI and keep ops_plan.PLAN as the reviewed source of truth (TODO 2). "
                "Cloud spend alerts: see module 12 (Cloud only)")
STAGING_HINT = ("optional (TODO 5, in the UI): create the plan's non-production project (this stack is OSS, so in its "
                "own organization: New organization -> New project), then Project settings -> API keys -> Create with "
                "the permission Ingestion; put the pair in the repo-root .env as M13_STAGING_PUBLIC_KEY / "
                "M13_STAGING_SECRET_KEY, put the project id into ops_plan.PLAN, restart the kernel and run TODO 5")


def _nearest_rank(values, p: float):
    v = sorted(values)
    return v[max(0, math.ceil(p * len(v)) - 1)] if v else None


def _ok_status(row: dict) -> bool:
    return 200 <= int(row.get("status") or 0) < 300


def expected_delivery(sent: dict, exports: list[dict], stored: dict, spans_per_request: int) -> dict:
    """verify's own delivery report (same definition as ops.delivery_report), from the tap log and stored data."""
    lat = list(sent.get("latencies_ms") or [])
    ids = list(sent.get("trace_ids") or [])
    batches = {}
    for r in exports:
        batches.setdefault(r.get("batch"), []).append(r)
    accepted = [rows for rows in batches.values() if any(_ok_status(r) for r in rows)]
    counts = {t: int(stored.get(t) or 0) for t in ids}
    partial = sorted(t for t, n in counts.items() if 0 < n < spans_per_request)
    lost = sorted(t for t, n in counts.items() if n == 0)
    return {"requests": len(ids), "p50_ms": _nearest_rank(lat, 0.5), "p95_ms": _nearest_rank(lat, 0.95),
            "max_ms": max(lat) if lat else None, "attempts": len(exports), "batches": len(batches),
            "failed_attempts": sum(not _ok_status(r) for r in exports),
            "dropped_batches": len(batches) - len(accepted),
            "spans_exported": sum(int(rows[0].get("spans") or 0) for rows in accepted),
            "delivered": sum(n == spans_per_request for n in counts.values()), "partial": partial, "lost": lost,
            "ok": not partial and not lost}


def _api_with(public_key: str, secret_key: str):
    """A bare API client for another key pair (the singleton SDK client stays on the .env keys)."""
    import os

    from langfuse.api import LangfuseAPI

    base = os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST") or "http://localhost:3000"
    return LangfuseAPI(base_url=base, username=public_key, password=secret_key)


def module_13_checks(run_tag: str, client, *, plan: dict | None, traffic: list | None, sdk: dict | None, tap_log,
                     erasure: dict | None, retention: dict | None, staging: dict | None = None,
                     allow_dry: bool = False) -> list[Check]:
    """Production-ops checks over every observation tagged `run_tag`: the two deployments' traffic (`traffic`: the
    notebook's rows {"trace_id", "user_id", "deployment"} for every agent request it sent), the separation `plan`
    (ops_plan.PLAN), the SDK lab (`sdk`: {scenario: {"sent": worker result, "report": ops.delivery_report}} + the tap's
    JSONL `tap_log`), the GDPR `erasure` ({"subject", "passes": [receipt], "straggler": [trace ids], "final": [...]}),
    the retention job's dry run (`retention`: {"now", "policy", "expired"}) and the optional staging project
    (`staging`: TODO 5's result). `allow_dry`: accept traffic tagged dry-run (rehearsals; never for the graded run)."""
    import os

    from . import ops_lab, traffic as traffic_mod
    from .otlp_tap import read_exports

    run_id = run_tag.split(":", 1)[-1]
    plan = plan if isinstance(plan, dict) else {}
    deployments = plan.get("deployments") or {}
    rows_in = [r for r in (traffic or []) if isinstance(r, dict) and r.get("trace_id")]
    subject = (erasure or {}).get("subject")
    want_subject = traffic_mod.ops_subject(run_id)

    def fetch():
        obs = run_observations(client, run_tag, fields=OPS_FIELDS)
        roots = [o for o in obs if o.get("isRootObservation") and o.get("name") == ops_lab.ROOT_NAME
                 and o.get("environment") != ops_lab.SDK_ENVIRONMENT]
        kept = [r for r in rows_in if r.get("user_id") != want_subject]
        if len({r["traceId"] for r in roots}) < len(kept):
            raise NotYet(f"{len(roots)}/{len(kept)} kept support-request roots tagged {run_tag!r} ingested so far")
        tids = sorted({r["traceId"] for r in roots})
        scores = {t: scores_for(client, trace_id=t) for t in tids}
        unscored = [t for t in tids if not any(x.get("name") == ops_lab.SCORE_NAME for x in scores[t])]
        if unscored:
            raise NotYet(f"{len(unscored)}/{len(tids)} kept traces have no {ops_lab.SCORE_NAME} score yet")
        project = client.api.projects.get().data[0]
        return {"obs": obs, "roots": roots, "scores": scores, "project": project,
                "by_trace": Counter(o["traceId"] for o in obs)}

    snapshot = RunSnapshot(fetch, fields=OPS_FIELDS)

    def need(name, value, hint):
        if not value:
            raise AssertionError(f"pass {name}= ({hint})")
        return value

    def dep_of(tags) -> str | None:
        return next((t.split(":", 1)[1] for t in tags or [] if str(t).startswith("deployment:")), None)

    def sdk_parts(name: str) -> tuple[dict, dict, list[dict]]:
        s = need("sdk", sdk, "the SDK lab's results: {scenario: {'sent', 'report'}}").get(name)
        if not isinstance(s, dict) or not isinstance(s.get("sent"), dict):
            raise AssertionError(f"sdk[{name!r}] is missing: run the SDK lab cell")
        ids = set(s["sent"].get("trace_ids") or [])
        exports = [r for r in read_exports(tap_log) if ids.intersection(r.get("trace_ids") or [])]
        return s["sent"], s.get("report") or {}, exports

    def stored_for(sent: dict) -> dict:
        by = snapshot.get()["by_trace"]
        return {t: by.get(t, 0) for t in sent.get("trace_ids") or []}

    def delivery(name: str) -> tuple[dict, dict, list[dict], dict]:
        sent, report, exports = sdk_parts(name)
        return sent, report, exports, expected_delivery(sent, exports, stored_for(sent), ops_lab.SPANS_PER_REQUEST)

    # 1 ------------------------------------------------------------------------------------------------
    def traffic_check():
        s = snapshot.get()
        need("traffic", rows_in, "the agent requests you sent: [{'trace_id', 'user_id', 'deployment'}]")
        problems, per_dep, prompts_by_env = [], Counter(), {}
        roots = {r["traceId"]: r for r in s["roots"]}
        for o in s["obs"]:
            if o.get("type") == "GENERATION" and o["traceId"] in roots and o.get("promptName"):
                prompts_by_env.setdefault(roots[o["traceId"]].get("environment"), set()).add(
                    (o.get("promptName"), o.get("promptVersion")))
        for r in rows_in:
            if r.get("user_id") == want_subject:
                continue
            root = roots.get(r["trace_id"])
            rid = r["trace_id"][:8]
            if root is None:
                problems.append(f"request {rid} ({r.get('deployment')}) is missing from Langfuse")
                continue
            dep = deployments.get(r.get("deployment")) or {}
            if OPS_DRY_TAG in (root.get("tags") or []) and not allow_dry:
                return False, ("this run used the dry-run oracle LLM (tag dry-run): set DRY_RUN = False and re-run the "
                               "notebook with a new RUN_TAG for the graded run")
            if dep_of(root.get("tags")) != r.get("deployment"):
                problems.append(f"{rid}: tag deployment:{dep_of(root.get('tags'))}, sent by {r.get('deployment')}")
            if root.get("environment") != dep.get("environment"):
                problems.append(f"{rid} ({r.get('deployment')}): environment {root.get('environment')!r}, the plan "
                                f"says {dep.get('environment')!r}")
            if dep.get("release") and root.get("version") != dep.get("release"):
                problems.append(f"{rid}: version {root.get('version')!r}, the plan's release is {dep.get('release')!r} "
                                "(propagate_attributes(version=...): a per-trace release in a shared process)")
            if not str(root.get("userId") or "").startswith(f"cust-{run_id}-"):
                problems.append(f"{rid}: user {root.get('userId')!r} is not run-scoped (cust-{run_id}-NN)")
            per_dep[r.get("deployment")] += 1
        if problems:
            return False, "; ".join(problems[:4])
        wanted = Counter(r.get("deployment") for r in rows_in if r.get("user_id") != want_subject)
        if per_dep != wanted or set(wanted) != {OPS_PROD_DEPLOYMENT, OPS_STAGING_DEPLOYMENT}:
            return False, f"kept requests per deployment {dict(per_dep)}, expected {dict(wanted)} for both deployments"
        shared = set.intersection(*prompts_by_env.values()) if len(prompts_by_env) >= 2 else set()
        if not shared:
            return False, (f"the generations of both environments should be linked to the same prompt version "
                           f"(propagate_attributes(prompt=...)): {prompts_by_env}")
        name, version = sorted(shared)[0]
        return True, (f"{sum(per_dep.values())} kept requests {dict(per_dep)} in environments "
                      f"{sorted(prompts_by_env)} with the plan's releases as versions, run-scoped users; both environments served "
                      f"{name} v{version} (prompts are project-global: one label move changes both)")

    # 2 ------------------------------------------------------------------------------------------------
    def separation_check():
        s = snapshot.get()
        roots = s["roots"]
        frm = min(_as_datetime(r["startTime"]) for r in roots) - timedelta(minutes=1)
        to = max(_as_datetime(r["startTime"]) for r in roots) + timedelta(minutes=5)
        base = [_tag_filter(run_tag), {"column": "isRootObservation", "operator": "=", "value": True, "type": "boolean"},
                {"column": "name", "operator": "=", "value": ops_lab.ROOT_NAME, "type": "string"}]
        problems, out = [], []
        total = metrics_query(client, "observations", [("count", "count")], frm=frm, to=to, filters=base)
        all_n = int((total[0] if total else {}).get("count_count") or 0)
        for env in sorted({r.get("environment") for r in roots}):
            flt = {"column": "environment", "operator": "=", "value": env, "type": "string"}
            stored_n = sum(r.get("environment") == env for r in roots)
            rows = metrics_query(client, "observations", [("count", "count")], frm=frm, to=to, filters=base + [flt])
            got_n = int((rows[0] if rows else {}).get("count_count") or 0)
            vals = [float(_truthy(x.get("value"))) for r in roots if r.get("environment") == env
                    for x in s["scores"].get(r["traceId"], []) if x.get("name") == ops_lab.SCORE_NAME]
            sc = metrics_query(client, "scores-boolean", [("value", "avg")], frm=frm, to=to,
                               filters=[_tag_filter(run_tag), flt,
                                        {"column": "name", "operator": "=", "value": ops_lab.SCORE_NAME,
                                         "type": "string"}])
            got_avg = (sc[0] if sc else {}).get("avg_value")
            want_avg = sum(vals) / len(vals) if vals else None
            if got_n != stored_n:
                problems.append(f"{env}: Metrics API counts {got_n} requests, stored {stored_n}")
            if not _near(got_avg, want_avg, rel=1e-6):
                problems.append(f"{env}: Metrics API pass rate {_p(got_avg)}, stored {_p(want_avg)}")
            out.append(f"{env}: {stored_n} requests, pass rate {_p(want_avg)}")
        if all_n < len(roots):
            problems.append(f"without an environment filter the Metrics API counts {all_n} < {len(roots)}")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (" | ".join(out) + f"; without the environment filter the same query counts {all_n} (every "
                      "environment of the run, sdk-lab included): environments separate views, not access, keys or "
                      "prompts")

    # 3 ------------------------------------------------------------------------------------------------
    def plan_check():
        s = snapshot.get()
        need("plan", plan, "ops_plan.PLAN (TODO 2)")
        problems = []
        edition = ops_lab.stack_edition()[0]
        if plan.get("edition") != edition:
            problems.append(f"plan edition {plan.get('edition')!r}, this stack runs {edition!r}")
        projects = plan.get("projects") or {}
        prod = deployments.get(OPS_PROD_DEPLOYMENT) or {}
        live = s["project"]
        if (projects.get(prod.get("project")) or {}).get("id") != live.id:
            problems.append(f"the production deployment's project id is "
                            f"{(projects.get(prod.get('project')) or {}).get('id')!r}, the live project is {live.id!r}")
        tiers = {}
        for name, d in deployments.items():
            tiers.setdefault(d.get("project"), set()).add(d.get("tier"))
            keys = [(plan.get("keys") or {}).get(k) or {} for k in d.get("keys") or []]
            if d.get("tier") == "production" and any(k.get("permission") == "ADMIN" for k in keys) \
                    and "delete_traces" not in (d.get("needs") or []):
                problems.append(f"{name} runs in production with a full-access key it does not need")
        mixed = sorted(str(p) for p, t in tiers.items() if {"production", "non-production"} <= t)
        if mixed:
            problems.append(f"project(s) {mixed} host production and non-production deployments")
        envs = {r.get("environment") for r in s["roots"]} | {ops_lab.SDK_ENVIRONMENT}
        listed = {e for p in projects.values() for e in (p.get("environments") or {})}
        uncovered = sorted(e for e in envs if e not in listed) if plan.get("retention_default_days") is None else []
        if uncovered:
            problems.append(f"environment(s) {uncovered} of this run have no retention in the plan (keep forever?)")
        if edition == "oss" and plan.get("retention_by") == "native":
            problems.append("retention_by 'native' on OSS: native retention is Enterprise, plan a job")
        if problems:
            return False, "; ".join(problems[:4])
        staging = deployments.get(OPS_STAGING_DEPLOYMENT) or {}
        nonprod = projects.get(staging.get("project")) or {}
        return True, (f"edition {edition}; production -> {live.id} ({prod.get('environment')}), staging -> project "
                      f"{staging.get('project')} ({'id ' + nonprod['id'] if nonprod.get('id') else 'not created yet'}"
                      f"); no project mixes tiers; live retention_days={live.retention_days}: the retention job "
                      f"enforces {len(listed)} per-environment retention value(s)")

    # 4 ------------------------------------------------------------------------------------------------
    def sdk_check():
        problems, got = [], {}
        for sc in ops_lab.SDK_SCENARIOS:
            sent, report, _, want = delivery(sc["name"])
            got[sc["name"]] = want
            if len(sent.get("trace_ids") or []) != sc["n"]:
                problems.append(f"{sc['name']}: the worker sent {len(sent.get('trace_ids') or [])} requests, "
                                f"expected {sc['n']}")
            for k in OPS_REPORT_KEYS + ("p50_ms", "p95_ms", "max_ms"):
                if k not in report:
                    problems.append(f"{sc['name']}: report[{k!r}] missing (ops.delivery_report)")
                elif isinstance(want[k], float) and not _near(report[k], want[k], rel=1e-9):
                    problems.append(f"{sc['name']}: {k} = {_p(report[k])}, the latencies say {_p(want[k])}")
                elif not isinstance(want[k], float) and report[k] != want[k]:
                    problems.append(f"{sc['name']}: {k} = {report[k]!r}, the tap log + stored data say {want[k]!r}")
        if problems:
            return False, "; ".join(problems[:4])
        d, t, f = got["defaults"], got["tuned"], got["flush-per-request"]
        flush_at = ops_lab.scenario("tuned")["client"]["flush_at"]
        big = [r.get("spans") for r in sdk_parts("tuned")[2] if (r.get("spans") or 0) > flush_at]
        if big:
            problems.append(f"tuned: batches of {big} spans exceed flush_at={flush_at}")
        if t["batches"] < 2 * d["batches"]:
            problems.append(f"tuned made {t['batches']} batches vs {d['batches']} with the defaults: does make_client "
                            "pass flush_at / flush_interval?")
        if f["attempts"] < ops_lab.scenario("flush-per-request")["n"]:
            problems.append(f"flush-per-request exported {f['attempts']} times for {f['requests']} requests")
        if not f["p50_ms"] or not d["p95_ms"] or f["p50_ms"] <= d["p95_ms"]:
            problems.append(f"flush-per-request p50 {_p(f['p50_ms'])} ms is not above the defaults' p95 "
                            f"{_p(d['p95_ms'])} ms")
        for name in ("defaults", "tuned", "flush-per-request", "job"):
            if not got[name]["ok"]:
                problems.append(f"{name} lost {len(got[name]['lost'])} trace(s) with no outage")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"defaults: {d['batches']} batch(es), {d['spans_exported']} spans; tuned (flush_at={flush_at}): "
                      f"{t['batches']} batches; flush per request: {f['attempts']} exports, request p50 "
                      f"{_p(f['p50_ms'])} ms vs {_p(d['p50_ms'])} ms; all 7 reports = the tap log + stored data")

    # 5 ------------------------------------------------------------------------------------------------
    def outage_check():
        _, report, exports, out = delivery("outage")
        _, _, hexports, hard = delivery("outage-hardened")
        tuned = delivery("tuned")[3]
        if not any(r.get("outage") for r in exports) or not any(r.get("outage") for r in hexports):
            return False, ("the tap logged no planted 503 for one of the outage scenarios: run them through the tap "
                           "(base_url = TAP.url) with TAP.outage(...) armed")
        if not out["lost"] and not out["partial"]:
            return False, ("the outage cost no data with the default retry budget this time (no dropped batch): "
                           "re-run the SDK lab cell")
        if (report.get("lost"), report.get("partial")) != (out["lost"], out["partial"]):
            return False, (f"your report lists lost {report.get('lost')} / partial {report.get('partial')}, Langfuse is "
                           f"missing {out['lost']} / partial {out['partial']}")
        if not hard["ok"]:
            return False, (f"outage-hardened lost {len(hard['lost'])} trace(s): does make_client pass timeout, so the "
                           "exporter keeps retrying through the outage?")
        budget = max(4 * (tuned["p95_ms"] or 0), (tuned["p95_ms"] or 0) + OPS_LATENCY_SLACK_MS)
        slow = [(n, r["p95_ms"]) for n, r in (("outage", out), ("outage-hardened", hard)) if (r["p95_ms"] or 0) > budget]
        if slow:
            return False, f"the request path slowed down during the outage: {slow} vs p95 {_p(tuned['p95_ms'])} ms"
        return True, (f"planted 503 for {ops_lab.scenario('outage')['outage']['seconds']:g} s: request p95 "
                      f"{_p(out['p95_ms'])} ms (no outage: {_p(tuned['p95_ms'])} ms); timeout=5 dropped "
                      f"{out['dropped_batches']} batch(es) = {len(out['lost']) + len(out['partial'])} trace(s) and your "
                      f"report found exactly them; timeout={ops_lab.scenario('outage-hardened')['client']['timeout']} "
                      f"lost none ({hard['failed_attempts']} failed attempts retried)")

    # 6 ------------------------------------------------------------------------------------------------
    def killed_check():
        ksent, kreport, kexports, killed = delivery("killed-job")
        _, _, jexports, job = delivery("job")
        if ksent.get("exit") != "killed":
            return False, "the killed-job scenario did not die with os._exit (exit != 'killed')"
        if kexports or killed["delivered"]:
            return False, (f"the killed job exported {len(kexports)} time(s) and {killed['delivered']} trace(s) "
                           "arrived: with flush_interval=60 nothing should leave before the kill")
        if kreport.get("lost") != killed["lost"] or len(killed["lost"]) != len(ksent.get("trace_ids") or []):
            return False, f"your report for killed-job lists lost {kreport.get('lost')}, every sent trace is lost"
        if not job["ok"] or not jexports:
            return False, (f"the job that calls finish() lost {len(job['lost'])} trace(s): finish() must flush "
                           "(and shut down) the client before the process exits")
        return True, (f"killed (os._exit): 0 exports, all {len(killed['lost'])} traces lost and reported; with "
                      f"finish(): {job['attempts']} export(s) at exit, {job['delivered']}/{job['requests']} delivered")

    # 7 ------------------------------------------------------------------------------------------------
    def erasure_check():
        e = need("erasure", erasure, "{'subject', 'passes', 'straggler', 'final'} from TODO 4")
        if subject != want_subject:
            return False, f"the subject is {subject!r}, this run's customer is {want_subject} (traffic.ops_subject)"
        flt = json.dumps([{"type": "string", "column": "userId", "operator": "=", "value": subject}])
        left = client.api.observations.get_many(fields="core", filter=flt, limit=100).data
        if left:
            raise NotYet(f"{len(left)} observation(s) of {subject} still stored (a deletion takes up to ~2.5 min)")
        passes = [p for p in e.get("passes") or [] if isinstance(p, dict)]
        if not passes:
            return False, "no erasure receipts: erasure['passes'] = [erase_user(...), ...]"
        mine = {r["trace_id"] for r in rows_in if r.get("user_id") == subject}
        others = [r for r in rows_in if r.get("user_id") != subject]
        straggler = set(e.get("straggler") or [])
        first = set(passes[0].get("trace_ids") or [])
        later = set().union(*(set(p.get("trace_ids") or []) for p in passes[1:]))
        problems = []
        if not mine or not mine <= first:
            problems.append(f"pass 1 deleted {sorted(t[:8] for t in first)}, the subject's requests are "
                            f"{sorted(t[:8] for t in mine)}")
        if (first | later) & {r["trace_id"] for r in others}:
            problems.append("an erasure pass deleted another user's trace")
        if not straggler:
            problems.append("erasure['straggler'] is empty: the held background job's trace ids")
        elif straggler & first:
            problems.append("the straggler was already there in pass 1: it must arrive AFTER the erasure (release the "
                            "job after pass 1)")
        elif not straggler <= later:
            problems.append(f"the straggler {sorted(t[:8] for t in straggler)} re-created the subject's data and no "
                            "later pass deleted it: re-check after a grace period and erase again")
        by = snapshot.get()["by_trace"]
        still = [t for t in mine | straggler if by.get(t) or scores_for(client, trace_id=t)]
        if still:
            problems.append(f"{len(still)} of the subject's traces still have observations or scores")
        missing = [r["trace_id"][:8] for r in others if not by.get(r["trace_id"])]
        if missing:
            problems.append(f"other users' traces were deleted: {missing}")
        if e.get("final"):
            problems.append(f"erasure['final'] still lists {e.get('final')}")
        if problems:
            return False, "; ".join(problems[:4])
        secs = [round(float(p.get("seconds") or 0)) for p in passes]
        return True, (f"{subject}: pass 1 deleted {len(first)} trace(s) in every environment, the held job's straggler "
                      f"arrived afterwards and pass {len(passes)} deleted it; nothing of the subject left, "
                      f"{len(others)} other requests untouched; each pass waited {secs} s for the async deletion")

    # 8 ------------------------------------------------------------------------------------------------
    def retention_check():
        r = need("retention", retention, "{'now', 'policy', 'expired'} from the retention cell")
        projects = plan.get("projects") or {}
        policy = {env: days for p in projects.values() for env, days in (p.get("environments") or {}).items()}
        policy["*"] = plan.get("retention_default_days")
        if dict(r.get("policy") or {}) != policy:
            return False, f"retention['policy'] {r.get('policy')} is not the plan's {policy} (ops.retention_policy)"
        now = _as_datetime(r.get("now"))
        oldest, env = {}, {}
        for o in snapshot.get()["obs"]:
            if o.get("isRootObservation"):
                t, ts = o["traceId"], _as_datetime(o["startTime"])
                env.setdefault(t, o.get("environment"))
                oldest[t] = min(ts, oldest.get(t, ts))
        want = []
        for t, ts in oldest.items():
            days = policy.get(env[t], policy.get("*"))
            if days is not None and (now - ts).total_seconds() > days * 86400:
                want.append(t)
        want, got = sorted(want), sorted(r.get("expired") or [])
        if got != want:
            return False, (f"the dry run selected {len(got)} trace(s), the plan's policy at {now:%Y-%m-%d} selects "
                           f"{len(want)} of the run's stored traces: missing {sorted(set(want) - set(got))[:3]}, extra "
                           f"{sorted(set(got) - set(want))[:3]}")
        gone = set(want)
        return True, (f"dry run at {now:%Y-%m-%d}: deletes {dict(Counter(env[t] for t in want))}, keeps "
                      f"{dict(Counter(env[t] for t in oldest if t not in gone))} (per environment; native retention "
                      "would be one value per project and is Enterprise)")

    # 9 ------------------------------------------------------------------------------------------------
    def staging_check():
        keys = [os.getenv(k) for k in ops_lab.STAGING_KEY_ENV]
        if not all(keys):
            raise FeatureUnavailable(STAGING_HINT)
        st = need("staging", staging, "TODO 5's result {'trace_ids', ...}")
        api = _api_with(*keys)
        problems = []
        try:
            proj = api.projects.get().data[0]
        except Exception as exc:
            return False, f"the staging key cannot read its own project (project:read): {exc}"
        live = snapshot.get()["project"]
        if proj.id == live.id:
            return False, "the M13_STAGING_* key belongs to the production project: create it in the new project"
        planned = ((plan.get("projects") or {}).get((deployments.get(OPS_STAGING_DEPLOYMENT) or {}).get("project"))
                   or {}).get("id")
        if planned != proj.id:
            problems.append(f"the plan's staging project id is {planned!r}, the key's project is {proj.id!r}")
        try:
            api.observations.get_many(fields="core", limit=1)
            problems.append("the staging key can READ traces: create it with the Ingestion permission (least privilege)")
        except Exception as exc:
            if getattr(exc, "status_code", None) not in (401, 403):
                raise
        ids = set(st.get("trace_ids") or [])
        exp = [r for r in read_exports(tap_log) if ids.intersection(r.get("trace_ids") or [])]
        if not ids or not exp or not all(_ok_status(r) for r in exp):
            problems.append(f"the staging deployment's exports through the tap: {[r.get('status') for r in exp]} "
                            "(want every export accepted)")
        leaked = [t for t in ids if snapshot.get()["by_trace"].get(t)]
        if leaked:
            problems.append(f"{len(leaked)} staging trace(s) are visible in the production project")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"staging project {proj.name} ({proj.id}) != production {live.id}; its Ingestion key sent "
                      f"{len(ids)} traces ({len(exp)} accepted export(s)), cannot read (403), and the production "
                      "project does not see them")

    # 10 -----------------------------------------------------------------------------------------------
    def native_retention_check():
        live = snapshot.get()["project"]
        if ops_lab.stack_edition()[0] != "enterprise":
            raise FeatureUnavailable(RETENTION_HINT.format(days=live.retention_days))
        want = ((plan.get("projects") or {}).get((deployments.get(OPS_PROD_DEPLOYMENT) or {}).get("project"))
                or {}).get("environments", {})
        days = max([d for d in want.values() if d], default=None)
        return live.retention_days == days, f"retention_days={live.retention_days}, the plan says {days}"

    # 11 -----------------------------------------------------------------------------------------------
    def org_api_check():
        try:
            client.api.organizations.get_organization_projects()
            live = "organizations.get_organization_projects() answered (an organization key?)"
        except Exception as exc:
            body = getattr(exc, "body", None)
            msg = (body.get("error") or body.get("message") or body.get("detail")) if isinstance(body, dict) else body
            live = (f"organizations.get_organization_projects() -> {getattr(exc, 'status_code', '?')} "
                    f"{str(msg or exc).rstrip('.')}")
        raise FeatureUnavailable(ORG_API_HINT.format(live=live))

    return [
        Check("deployments: production + staging traffic per the plan (prompt shared)", traffic_check),
        Check("environment separation in the Metrics API", separation_check),
        Check("the plan matches the live stack", plan_check),
        Check("SDK batching: your reports = the tap log + stored data", sdk_check),
        Check("planted outage: the request path doesn't notice, the lost traces are found", outage_check),
        Check("planted killed job: its buffer is lost; finish() delivers everything", killed_check),
        Check("GDPR erasure + planted straggler: nothing of the subject left, nobody else touched", erasure_check),
        Check("retention job (dry run) = the plan's policy on the stored data", retention_check),
        Check("staging project + Ingestion key (least privilege, isolation)", staging_check),
        Check("native data retention", native_retention_check),
        Check("organization API, SCIM, project-level roles, audit logs, protected labels", org_api_check),
    ]


def check_module_13(run_tag: str, *, plan: dict | None, traffic: list | None, sdk: dict | None, tap_log,
                    erasure: dict | None, retention: dict | None, staging: dict | None = None, client=None,
                    allow_dry: bool = False, retries: int = 12, **run_kwargs) -> bool:
    """See module_13_checks for the inputs (all produced by the notebook)."""
    client = client or get_client()
    checks = module_13_checks(run_tag, client, plan=plan, traffic=traffic, sdk=sdk, tap_log=tap_log, erasure=erasure,
                              retention=retention, staging=staging, allow_dry=allow_dry)
    return _check_module(checks, client, retries=retries, **run_kwargs)


# ---------------------------------------------------------------- module 14
LOOP_WINDOWS = ("baseline", "incident", "recovered")  # approved version, after the planted deploy, after the promotion
LOOP_TICKS = {"fire": ("baseline", "incident"), "resolve": ("baseline", "recovered")}
LOOP_FIELDS = "core,basic,io,metrics,trace_context,metadata"
LOOP_DRY_TAG = "dry-run"
LOOP_MIN_DROP = 0.15  # loop.LOOP_RULES' judge rule (min_abs): the drop the stored data must show in the incident
LOOP_PER_WINDOW = 3
LOOP_INCIDENT_TAGS = ("login_reset", "locked_out")  # traffic.LOOP_INCIDENT_CASES: what the bad deploy misroutes


def _loop_message(value) -> str:
    v = _decode(value)
    if isinstance(v, dict):
        v = v.get("message", "")
    return str(v or "").strip()


def module_14_checks(run_tag: str, client, *, prefix: str = "", environment: str = "production", deliveries=None,
                     alert_secret: str | None = None, ticks: dict | None = None, queued=None, items=None, pin=None,
                     releases: dict | None = None, evidence=None, allow_dry: bool = False,
                     hours: int = GATE_HOURS) -> list[Check]:
    """Capstone checks: the production loop on one planted regression. Inputs (all from the notebook): the alert job's
    tick reports `ticks` ({"fire": baseline vs incident, "resolve": baseline vs recovered}), the receiver's log
    `deliveries` and the alert signing secret, TODO 2's queued rows, TODO 3's items + `pin`, TODO 4's `releases`
    ({"quick": ..., "fix": ...}) and TODO 5's evidence file (`evidence.json`). Everything is compared with what
    Langfuse stored: the run's traces and scores, the prompt's versions + audit comments, the annotation queue, the
    dataset items, the gate experiments (+ items, re-runs, ci_gate scores) of this run tag.
    `allow_dry`: accept dry-run traffic and dry gate runs (rehearsals only, never the graded run)."""
    import os
    from pathlib import Path

    from . import alert_spec, loop_spec, prompts, regression

    names = loop_spec.names(prefix)
    judge, incident = names["judge"], loop_spec.incident_id(run_tag.split(":", 1)[-1])
    skew = timedelta(seconds=loop_spec.SKEW_S)
    expected = len(LOOP_WINDOWS) * LOOP_PER_WINDOW
    native_secret = os.getenv(alert_spec.SECRET_ENV["prompt"])

    def is_judge(s: dict) -> bool:
        return s.get("name") == judge and is_judge_score(s)

    def fetch():
        obs = run_observations(client, run_tag, fields=LOOP_FIELDS)
        roots = [o for o in obs if o.get("isRootObservation") and o.get("name") == loop_spec.ROOT_NAME]
        if len(roots) < expected:
            raise NotYet(f"{len(roots)}/{expected} support-request roots tagged {run_tag!r} ingested so far")
        tids = sorted({r["traceId"] for r in roots})
        scores = {t: scores_for(client, trace_id=t, fields=REVIEW_SCORE_FIELDS) for t in tids}
        unjudged = [t[:8] for t in tids if not any(is_judge(s) for s in scores[t])]
        if unjudged:
            raise NotYet(f"{len(unjudged)}/{len(tids)} traces have no {judge} judge score yet: {unjudged}")
        versions = prompt_versions(client, names["prompt"])
        audits = []
        for v, row in versions.items():
            if not row.get("id"):
                continue
            for c in client.api.comments.get(object_type="PROMPT", object_id=row["id"], limit=100).data:
                c = _row(c)
                parsed = loop_spec.parse_audit(c.get("content"))
                if parsed and parsed[0] == run_tag:
                    audits.append({"action": parsed[1], "text": parsed[2], "version": v,
                                   "at": _as_datetime(c.get("createdAt"))})
        audits.sort(key=lambda a: a["at"])
        queue = next((q for q in _paged(lambda **kw: client.api.annotation_queues.list_queues(**kw), "queues")
                      if q.get("name") == names["queue"]), None)
        q_items = [] if queue is None else [
            i for i in _paged(lambda **kw: client.api.annotation_queues.list_queue_items(queue["id"], **kw),
                              "queue items") if i.get("objectId") in tids]
        try:
            ds = _row(client.api.datasets.get(names["dataset"]))
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            ds = None
        d_items, exps, mine, exp_items, gate_scores = {}, {}, [], {}, {}
        if ds is not None:
            wanted = {regression.dataset_item_id(names["dataset"], _loop_message(r.get("input"))) for r in roots}
            wanted |= {i.get("id") for i in (items or []) if isinstance(i, dict) and i.get("id")}
            for iid in sorted(wanted):
                try:
                    d_items[iid] = _row(client.api.dataset_items.get(iid))
                except Exception as exc:
                    if not _is_not_found(exc):
                        raise
            since = datetime.now(timezone.utc) - timedelta(hours=hours)
            listed = _cursor_pages(lambda **kw: client.api.experiments.list(
                from_start_time=since, dataset_id=ds["id"], fields=EXPERIMENT_FIELDS, **kw), "experiments")
            exps = {e["id"]: e for e in listed}
            mine = [e for e in listed if _dictish(e.get("metadata")).get("run_tag") == run_tag
                    and (allow_dry or _dictish(e.get("metadata")).get("dry_run") != "true")]
            want = {e["id"] for e in mine} | {_dictish(e.get("metadata")).get("baseline_experiment_id") for e in mine}
            for eid in sorted(x for x in want if x and x in exps):
                exp_items[eid] = _cursor_pages(lambda **kw: client.api.experiments.list_items(
                    from_start_time=since, experiment_id=eid, fields=EXPERIMENT_ITEM_FIELDS, **kw), "experiment items")
            for e in mine:
                if _dictish(e.get("metadata")).get("role") == "candidate":
                    gate_scores[e["id"]] = [s for s in scores_for(client, experiment_id=e["id"])
                                            if s.get("name") == GATE_SCORE]
        return {"obs": obs, "roots": roots, "scores": scores, "versions": versions, "audits": audits,
                "queue": queue, "queue_items": q_items, "configs": active_configs(client), "dataset": ds,
                "dataset_items": d_items, "experiments": exps, "mine": mine, "exp_items": exp_items,
                "gate_scores": gate_scores}

    snapshot = RunSnapshot(fetch, fields=LOOP_FIELDS)

    def snap() -> dict:
        return snapshot.get()

    # ------------------------------------------------------------ windows and the stored truth
    def tick(name: str) -> dict:
        t = ticks.get(name) if isinstance(ticks, dict) else None
        if not isinstance(t, dict):
            raise AssertionError(f"no tick {name!r}: pass ticks={{'fire': ..., 'resolve': ...}} (alert_job.run_once's "
                                 "reports)")
        return t

    def windows() -> dict:
        out = {}
        for name, (base, cur) in LOOP_TICKS.items():
            for side, w in (("baseline", base), ("current", cur)):
                try:
                    win = _ms_window(tick(name).get(side) or {})
                except (KeyError, TypeError, ValueError) as exc:
                    raise AssertionError(f"ticks[{name!r}][{side!r}] needs ISO 'from'/'to': {exc}") from None
                if out.setdefault(w, win) != win:
                    raise AssertionError(f"tick {name!r} uses another {w} window: both ticks compare with the same "
                                         "baseline")
        return out

    def window_of(snap_: dict) -> str | None:
        try:
            win = _ms_window(snap_)
        except (KeyError, TypeError, ValueError):
            return None
        return next((w for w, x in windows().items() if x == win), None)

    def latest_judge(t: str):
        return _latest([s for s in snap()["scores"].get(t, []) if is_judge(s)])

    def truth(w: str) -> dict:
        s, win = snap(), windows()[w]
        roots = sorted((r for r in s["roots"] if _in_window(r["startTime"], win)), key=lambda r: str(r["startTime"]))
        tids = [r["traceId"] for r in roots]
        vals = [judge_verdict(x.get("value")) for t in tids for x in s["scores"].get(t, [])
                if is_judge(x) and _in_window(x.get("timestamp"), win)]
        vals = [v for v in vals if v is not None]
        verdicts = {t: judge_verdict((latest_judge(t) or {}).get("value")) for t in tids}
        versions = Counter(r.get("version") for r in roots)
        return {"win": win, "roots": roots, "tids": tids, "n": len(roots), "versions": versions,
                "version": versions.most_common(1)[0][0] if versions else None,
                "rate": sum(vals) / len(vals) if vals else None, "verdicts": verdicts,
                "flagged": [t for t in tids if verdicts[t] is False]}

    def served() -> dict:
        """{window: prompt version (int)} from the stored traces."""
        return {w: alert_spec.prompt_version_of(truth(w)["version"]) for w in LOOP_WINDOWS}

    def drop(w: str):
        a, x = truth("baseline")["rate"], truth(w)["rate"]
        return None if a is None or x is None else a - x

    def labels_on(version) -> list:
        return list((snap()["versions"].get(version) or {}).get("labels") or [])

    def audits(action: str, version=None) -> list[dict]:
        return [a for a in snap()["audits"] if a["action"] == action and (version is None or a["version"] == version)]

    def alert_rows() -> list[dict]:
        return [r for r in receiver_rows(deliveries) if r.get("path") == loop_spec.ALERT_PATH
                and (r["envelope"] or {}).get("type") == "agent-alert"
                and run_tag in ((r["envelope"].get("scope") or {}).get("tags") or [])]

    def sent_ids(name: str) -> set:
        return {(s.get("payload") or {}).get("id") for s in tick(name).get("sent") or [] if isinstance(s, dict)}

    def firing_row() -> dict:
        rows = [r for r in alert_rows() if _accepted(r) and r["envelope"].get("status") == "firing"
                and r["envelope"].get("rule") == loop_spec.JUDGE_RULE and r["envelope"].get("id") in sent_ids("fire")]
        if len(rows) != 1:
            raise AssertionError(f"{len(rows)} accepted firing deliveries of {loop_spec.JUDGE_RULE} from tick 'fire' at "
                                 f"{loop_spec.ALERT_PATH} (want exactly 1): alert rules = loop.LOOP_RULES?")
        return rows[0]

    def config_id(role: str):
        rows = snap()["configs"].get(names[role]) or []
        return rows[0]["id"] if rows else None

    def human(t: str):
        """The latest ANNOTATION label per review config on trace t: {role: score dict}."""
        out = {}
        for role in ("human", "expected", "error_type"):
            out[role] = _latest([s for s in snap()["scores"].get(t, []) if s.get("name") == names[role]
                                 and s.get("source") == "ANNOTATION"])
        return out

    def confirmed() -> dict:
        """{trace_id: expected team} for every queued trace a human labelled NOT ok, with an expected team that is not
        what the agent did (what loop.incident_items turns into tests)."""
        roots = {r["traceId"]: r for r in snap()["roots"]}
        out = {}
        for i in snap()["queue_items"]:
            t = i["objectId"]
            h = human(t)
            if h["human"] and judge_verdict(h["human"].get("value")) is False and h["expected"]:
                exp = h["expected"].get("value")
                did = _dictish(roots.get(t, {}).get("metadata")).get("handoff_target") or "none"
                if exp and exp != did:
                    out[t] = exp
        return out

    def incident_ids() -> dict:
        """{dataset item id: trace id} of the confirmed failures (the incident's tests)."""
        roots = {r["traceId"]: r for r in snap()["roots"]}
        out = {}
        for t in confirmed():
            out.setdefault(regression.dataset_item_id(names["dataset"], _loop_message(roots[t].get("input"))), t)
        return out

    # ------------------------------------------------------------ the gate runs
    def meta(e) -> dict:
        return _dictish(e.get("metadata"))

    def candidates() -> list[dict]:
        if snap()["dataset"] is None:
            raise NotYet(f"no dataset named {names['dataset']!r}: run the given setup cell (harness.bootstrap)")
        return [e for e in snap()["mine"] if meta(e).get("role") == "candidate"
                and meta(e).get("prompt_label") == loop_spec.HOTFIX_LABEL]

    def exp_verdicts(eid) -> dict:
        out = {}
        for it in snap()["exp_items"].get(eid, []):
            s = _latest([x for x in _score_rows(it.get("scores")) if x.get("name") == names["config"]])
            out[it["experimentItemId"]] = None if s is None else judge_verdict(s.get("value"))
        return out

    def reruns_of(e, side):
        return sorted((r for r in snap()["mine"] if meta(r).get("role") == "rerun" and meta(r).get("rerun_of") == e["id"]
                       and meta(r).get("side") == side), key=lambda r: str(meta(r).get("attempt")))

    def gate_score(e):
        rows = snap()["gate_scores"].get(e["id"], [])
        if not rows:
            raise NotYet(f"{e['name']}: no {GATE_SCORE!r} score yet (module 10's record_gate)")
        return _latest(rows)

    def recompute(e) -> dict:
        b = snap()["experiments"].get(meta(e).get("baseline_experiment_id"))
        if b is None:
            raise AssertionError(f"{e['name']}: its baseline {meta(e).get('baseline_experiment_id')!r} is not an "
                                 f"experiment on {names['dataset']}")
        base, cand = exp_verdicts(b["id"]), exp_verdicts(e["id"])
        if not base or not cand or any(v is None for v in [*base.values(), *cand.values()]):
            raise NotYet(f"{e['name']}: items not ingested/scored yet")
        policy = _dictish(_dictish(gate_score(e).get("metadata")).get("policy"))
        r = _gate_recompute(base, cand, [exp_verdicts(x["id"]) for x in reruns_of(e, "baseline")],
                            [exp_verdicts(x["id"]) for x in reruns_of(e, "candidate")], policy)
        return {**r, "baseline": b, "policy": policy}

    def expected_of(e) -> dict:
        return {it["experimentItemId"]: _dictish(it.get("expectedOutput")).get("handoff")
                for it in snap()["exp_items"].get(e["id"], [])}

    def gates() -> dict:
        """{"quick": the blocked candidate run, "fix": the shipped one} (by the releases' versions when given)."""
        runs = candidates()
        if len(runs) < 2:
            raise NotYet(f"{len(runs)} gate run(s) for candidate label {loop_spec.HOTFIX_LABEL!r} with run_tag "
                         f"{run_tag!r} so far (want the quick fix and the fix: TODO 4)")
        out = {}
        for kind in ("quick", "fix"):
            rel = (releases or {}).get(kind) if isinstance(releases, dict) else None
            if rel and rel.get("version") is not None:
                found = [e for e in runs if meta(e).get("prompt_version") == str(rel["version"])]
            else:
                want = "block" if kind == "quick" else "ship"
                found = [e for e in runs if (gate_score(e) or {}).get("value") == want]
            if not found:
                raise AssertionError(f"no gate run for the {kind} release (version {(rel or {}).get('version')})")
            out[kind] = max(found, key=lambda e: str(e.get("startTime") or ""))
        return out

    # 1 ------------------------------------------------------------------------------------------------
    def traffic_check():
        s, wins = snap(), windows()
        problems, per = [], Counter()
        for a, b in zip(LOOP_WINDOWS, LOOP_WINDOWS[1:]):
            if not wins[a][1] <= wins[b][0]:
                problems.append(f"windows {a} and {b} overlap")
        for r in s["roots"]:
            tags = set(r.get("tags") or [])
            if LOOP_DRY_TAG in tags and not allow_dry:
                return False, ("this run used the dry-run oracle LLM (tag dry-run): set DRY_RUN = False and re-run the "
                               "notebook with a new RUN_TAG for the graded run")
            tagged = next((w for w in LOOP_WINDOWS if f"window:{w}" in tags), None)
            w = next((w for w, win in wins.items() if _in_window(r["startTime"], win)), None)
            per[w] += 1
            rid = r["traceId"][:8]
            if w is None or w != tagged:
                problems.append(f"root {rid} (tag window:{tagged}) starts {r['startTime']}, in tick window {w}")
            if names["tag"] not in tags:
                problems.append(f"root {rid} lacks the stable tag {names['tag']!r}")
            if r.get("environment") != environment:
                problems.append(f"root {rid} environment {r.get('environment')!r} != {environment!r}")
            if alert_spec.prompt_version_of(r.get("version")) is None:
                problems.append(f"root {rid} version {r.get('version')!r} is not 'prompt-v<n>'")
        problems += [f"{per[w]} requests in window {w}, expected {LOOP_PER_WINDOW}" for w in LOOP_WINDOWS
                     if per[w] != LOOP_PER_WINDOW]
        if problems:
            return False, "; ".join(problems[:4])
        mixed = {w: dict(truth(w)["versions"]) for w in LOOP_WINDOWS if len(truth(w)["versions"]) != 1}
        if mixed:
            return False, f"window(s) mix versions {mixed}: move labels BETWEEN windows"
        v = served()
        good, bad, fix = v["baseline"], v["incident"], v["recovered"]
        if len({good, bad, fix}) != 3:
            return False, (f"baseline v{good}, incident v{bad}, recovered v{fix}: three different versions expected "
                           "(approved, planted deploy, promoted fix)")
        versions = s["versions"]
        if (versions.get(good) or {}).get("prompt") != prompts.SYSTEM_PROMPT_TEMPLATE:
            return False, f"baseline v{good} is not the approved template (the start state moves production + live to it)"
        if (versions.get(bad) or {}).get("prompt") != prompts.WRONG_HANDOFF_TEMPLATE:
            return False, f"incident v{bad} is not the planted bad template (login -> billing)"
        missing = [lab for lab in (loop_spec.PRODUCTION_LABEL, loop_spec.LIVE_LABEL) if lab not in labels_on(fix)]
        if missing:
            return False, f"{missing} not on v{fix} (the version that served the recovered window): promote = both labels"
        return True, (f"{LOOP_PER_WINDOW} requests in each window ({environment}): baseline on v{good} (approved), "
                      f"incident on v{bad} (planted deploy), recovered on v{fix}; production + live are on v{fix}")

    # 2 ------------------------------------------------------------------------------------------------
    def judged_check():
        s = snap()
        mode = "live (EVAL)" if any(x.get("source") == "EVAL" for t in s["scores"] for x in s["scores"][t]
                                    if is_judge(x)) else "stand-in"
        wrong_env = sorted({str(x.get("environment")) for t in s["scores"] for x in s["scores"][t]
                            if is_judge(x) and x.get("environment") != environment})
        if wrong_env:
            return False, (f"judge scores in environment(s) {wrong_env}, not {environment!r}: the alert's Metrics query "
                           "filters on the environment, so it can't see them (create_score(environment=...))")
        inc = truth("incident")
        if not inc["flagged"]:
            return False, ("the judge flagged nothing in the incident window: the planted deploy routed correctly this "
                           "time (an LLM flake) or the judge missed it; re-run the notebook with a new RUN_TAG")
        d = drop("incident")
        if d is None or d < LOOP_MIN_DROP:
            return False, (f"judge pass rate baseline {_p(truth('baseline')['rate'])} -> incident {_p(inc['rate'])}: "
                           f"a drop under {LOOP_MIN_DROP} would not page")
        login = [r["traceId"] for r in inc["roots"] if set(r.get("tags") or []) & set(LOOP_INCIDENT_TAGS)]
        return True, (f"judge mode {mode}; pass rate baseline {_p(truth('baseline')['rate'])}, incident {_p(inc['rate'])} "
                      f"({len(inc['flagged'])} flagged, {sum(t in inc['flagged'] for t in login)}/{len(login)} login), "
                      f"recovered {_p(truth('recovered')['rate'])}")

    # 3 ------------------------------------------------------------------------------------------------
    def alert_check():
        r, p = firing_row(), firing_row()["envelope"]
        problems = []
        if not signed_with(r, alert_secret):
            problems.append("its signature does not verify with the alert secret (alerting.deliver signs the exact body)")
        if not str(p.get("metric") or "").startswith("score.") or judge not in str(p.get("metric")):
            problems.append(f"metric {p.get('metric')!r} is not the judge's pass rate")
        for side, w in (("baseline", "baseline"), ("current", "incident")):
            got = p.get(side) or {}
            if window_of(got) != w:
                problems.append(f"{side} window {got.get('from')}..{got.get('to')} is not the {w} window")
            if not _near(got.get("value"), truth(w)["rate"], rel=1e-6):
                problems.append(f"{side} value {_p(got.get('value'))}, stored judge pass rate of {w} is "
                                f"{_p(truth(w)['rate'])}")
        if p.get("version") != truth("incident")["version"]:
            problems.append(f"version {p.get('version')!r}, the incident was served by {truth('incident')['version']!r}")
        if problems:
            return False, "; ".join(problems)
        others = [a.get("rule") for a in tick("fire").get("alerts") or [] if a.get("rule") != loop_spec.JUDGE_RULE]
        return True, (f"one signed firing delivery: {p.get('title')}" + (f" (also: {others})" if others else ""))

    # 4 ------------------------------------------------------------------------------------------------
    def queue_check():
        s = snap()
        if s["queue"] is None:
            return False, f"no annotation queue named {names['queue']!r} (the given setup cell creates it)"
        ids = {config_id(r) for r in ("human", "error_type", "expected")}
        if None in ids or not ids <= set(s["queue"].get("scoreConfigIds") or []):
            return False, (f"queue {names['queue']} lacks the review configs {names['human']}, {names['error_type']}, "
                           f"{names['expected']}")
        objs = Counter(i["objectId"] for i in s["queue_items"])
        if not objs:
            raise NotYet(f"no item of this run in {names['queue']} yet (TODO 2: fill_review_queue)")
        dup = [t[:8] for t, n in objs.items() if n > 1]
        if dup:
            return False, f"trace(s) queued twice: {dup} (the API doesn't dedupe: review.items_to_add)"
        inc = truth("incident")
        outside = [t[:8] for t in objs if t not in inc["tids"]]
        if outside:
            return False, f"queued trace(s) outside the alert's window: {outside} (alerting.alert_scope gives the window)"
        missing = [t[:8] for t in inc["flagged"] if t not in objs]
        if missing:
            return False, f"judge-flagged request(s) of the incident window not queued: {missing}"
        controls = [t for t in objs if t not in inc["flagged"]]
        unflagged = [t for t in inc["tids"] if t not in inc["flagged"]]
        if unflagged and not controls:
            return False, "no control: queue at least one request the judge passed (review.pick_controls, min_n=1)"
        rows = {q.get("trace_id"): q for q in (queued or []) if isinstance(q, dict)}
        if set(rows) != set(objs):
            return False, f"TODO 2 returned {len(rows)} row(s), the queue holds {len(objs)} of this run's traces"
        bad = [t[:8] for t in controls if rows[t].get("reason") not in ("control", "unjudged")]
        bad += [t[:8] for t in inc["flagged"] if rows[t].get("reason") in ("control", None)]
        if bad:
            return False, f"reasons don't match the stored judge verdicts for {bad}"
        return True, (f"{len(objs)} item(s) in {names['queue']}: {len(inc['flagged'])} judge-flagged + "
                      f"{len(controls)} control(s), no duplicates, all from the alert's window")

    # 5 ------------------------------------------------------------------------------------------------
    def review_check():
        s = snap()
        items_ = s["queue_items"]
        if not items_:
            raise NotYet("no queue items of this run yet")
        pending = [i for i in items_ if i.get("status") != "COMPLETED"]
        if pending:
            raise NotYet(f"{len(pending)} item(s) still PENDING in {names['queue']}: review them in the UI (Annotation "
                         "Queues -> Process queue) or let the scripted reviewer finish")
        problems, ui = [], 0
        for i in items_:
            t, h = i["objectId"], human(i["objectId"])
            if h["human"] is None:
                problems.append(f"{t[:8]}: no {names['human']} ANNOTATION score")
                continue
            if h["human"].get("configId") != config_id("human") or h["human"].get("queueId") != s["queue"]["id"]:
                problems.append(f"{t[:8]}: its {names['human']} score lacks the config or the queue id")
            ui += bool(h["human"].get("authorUserId"))
            if judge_verdict(h["human"].get("value")) is False:
                for role in ("expected", "error_type"):
                    if h[role] is None:
                        problems.append(f"{t[:8]}: labelled not ok but no {names[role]} (the expected team turns the "
                                        "label into a test)")
                if h["expected"] and h["expected"].get("value") not in loop_spec.EXPECTED_TEAMS:
                    problems.append(f"{t[:8]}: expected team {h['expected'].get('value')!r}")
        if problems:
            return False, "; ".join(problems[:4])
        n_bad = sum(judge_verdict(human(i["objectId"])["human"].get("value")) is False for i in items_)
        return True, (f"{len(items_)} item(s) COMPLETED: {n_bad} confirmed failure(s); {ui} labelled in the UI, "
                      f"{len(items_) - ui} through the API")

    # 6 ------------------------------------------------------------------------------------------------
    def dataset_check():
        s = snap()
        conf, ids = confirmed(), incident_ids()
        if not conf:
            return False, "no human-confirmed failure (human_routing_ok False + expected_handoff): nothing to test"
        problems = []
        for iid, t in ids.items():
            it = s["dataset_items"].get(iid)
            if it is None:
                problems.append(f"no dataset item {iid} for the confirmed failure {t[:8]} (id = dataset_item_id(name, "
                                "message))")
                continue
            m = _dictish(it.get("metadata"))
            if _plain(it.get("status")) != "ACTIVE":
                problems.append(f"{iid} is {_plain(it.get('status'))}")
            if _dictish(it.get("expectedOutput")).get("handoff") != conf[t]:
                problems.append(f"{iid} expects {_dictish(it.get('expectedOutput')).get('handoff')!r}, the human said "
                                f"{conf[t]!r}")
            if it.get("sourceTraceId") != t:
                problems.append(f"{iid}: sourceTraceId {it.get('sourceTraceId')!r} is not the trace {t[:8]}")
            if m.get("incident") != incident or m.get("source") != "incident":
                problems.append(f"{iid}: metadata incident/source {m.get('incident')!r}/{m.get('source')!r}")
            labelled = _as_datetime(human(t)["human"]["timestamp"])
            if _as_datetime(it.get("createdAt")) + skew < labelled:
                problems.append(f"{iid} was written before the human labelled {t[:8]}")
        others = [t[:8] for t in {i["objectId"] for i in s["queue_items"]} - set(conf)
                  if any(_dictish(x.get("metadata")).get("incident") == incident and x.get("sourceTraceId") == t
                         for x in s["dataset_items"].values())]
        if others:
            problems.append(f"item(s) for requests the human did NOT confirm as failures: {others}")
        got = {i.get("id") for i in (items or []) if isinstance(i, dict)}
        if got != set(ids):
            problems.append(f"TODO 3 returned items {sorted(got)}, the confirmed failures are {sorted(ids)}")
        versions = [_as_datetime(s["dataset_items"][i].get("version")) for i in ids if i in s["dataset_items"]]
        if pin is None or (versions and _as_datetime(pin) < max(versions)):
            problems.append(f"the pin {pin} does not include the incident items (pin = max(item.version))")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"{len(ids)} confirmed failure(s) -> {len(ids)} item(s) in {names['dataset']} (expected = the "
                      f"human's team, source trace, incident {incident}), pinned at {_as_datetime(pin).isoformat()}")

    # 7 ------------------------------------------------------------------------------------------------
    def planted_check():
        g = gates()["quick"]
        sc, r = gate_score(g), recompute(g)
        version = int(meta(g).get("prompt_version"))
        problems = []
        if sc.get("value") != "block" or r["decision"] != "block":
            problems.append(f"the quick fix (v{version}) was recorded {sc.get('value')!r}, the stored scores give "
                            f"{r['decision']!r}: it regresses the double charge and must be BLOCKED")
        exp, inc = expected_of(g), set(incident_ids())
        outside = [i for i in r["regressed"] if i not in inc]
        if not outside:
            problems.append(f"no confirmed regression outside the incident's tests (regressed {r['regressed']}, flaky "
                            f"{r['flaky']})")
        promoted = [lab for lab in (loop_spec.PRODUCTION_LABEL, loop_spec.LIVE_LABEL) if lab in labels_on(version)]
        if promoted or audits("promote", version):
            problems.append(f"the quick fix v{version} was promoted ({promoted or 'audit comment'})")
        if version in served().values():
            problems.append(f"the quick fix v{version} served traffic")
        rel = (releases or {}).get("quick") or {}
        if rel and (rel.get("exit_code") != 1 or rel.get("promoted")):
            problems.append(f"release record: exit {rel.get('exit_code')}, promoted {rel.get('promoted')} (want 1, False)")
        if problems:
            return False, "; ".join(problems[:4])
        fixed = [i for i in inc if (r["rates"].get(i) or (None, None))[1] == 1.0]
        teams = sorted({str(exp.get(i)) for i in outside})
        return True, (f"quick fix v{version} BLOCKED by the gate (exit 1): {len(outside)} confirmed regression(s) "
                      f"outside the incident ({', '.join(teams)}), although it passes {len(fixed)}/{len(inc)} incident "
                      "test(s); never promoted, never served")

    # 8 ------------------------------------------------------------------------------------------------
    def fix_check():
        g = gates()["fix"]
        sc, r = gate_score(g), recompute(g)
        version, m, inc = int(meta(g).get("prompt_version")), meta(g), set(incident_ids())
        problems = []
        if sc.get("value") != "ship" or r["decision"] != "ship":
            problems.append(f"the fix v{version}: recorded {sc.get('value')!r}, stored scores give {r['decision']!r}")
        if version != served()["recovered"]:
            problems.append(f"the shipped fix is v{version}, the recovered window was served by v{served()['recovered']}")
        ran = set(expected_of(g))
        if not inc <= ran:
            problems.append(f"the gate did not run the incident's tests {sorted(inc - ran)}: pin the new dataset version")
        if pin is not None and _stamp(m.get("dataset_version")) != _stamp(pin):
            problems.append(f"the gate ran dataset version {m.get('dataset_version')}, TODO 3 pinned {pin}")
        weak = [i for i in inc if i in r["rates"] and not ((r["rates"][i][1] or 0) > 0.5)]
        if weak:
            problems.append(f"incident test(s) {weak} don't pass on the fix (pass rate <= 0.5): it isn't fixed")
        if str(r["policy"].get("max_regressions")) not in ("0", "0.0"):
            problems.append(f"the gate ran with max_regressions={r['policy'].get('max_regressions')}: an incident fix may "
                            "not break anything (LOOP_POLICY)")
        b = r["baseline"]
        if meta(b).get("prompt_label") != BASELINE_LABEL or meta(b).get("prompt_version") != str(served()["baseline"]):
            problems.append(f"baseline {b.get('name')}: {meta(b).get('prompt_label')} v{meta(b).get('prompt_version')} "
                            f"(want production v{served()['baseline']}, the approved version)")
        rel = (releases or {}).get("fix") or {}
        if rel and (rel.get("exit_code") != 0 or not rel.get("promoted")):
            problems.append(f"release record: exit {rel.get('exit_code')}, promoted {rel.get('promoted')} (want 0, True)")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"fix v{version} shipped (exit 0) vs production v{served()['baseline']} on the pinned version: "
                      f"{r['n']} items incl. {len(inc)} incident test(s), all passing; {len(r['suspects'])} suspect(s), "
                      f"policy {dict(r['policy'])}")

    # 9 ------------------------------------------------------------------------------------------------
    def promotion_check():
        v, s = served(), snap()
        fix, bad = v["recovered"], v["incident"]
        problems = []
        deploy = [a for a in audits("deploy", bad) if loop_spec.LIVE_LABEL in a["text"]]
        if not deploy:
            problems.append(f"no audit comment for the planted deploy on v{bad} (move(BAD, [LIVE], 'deploy', ...))")
        else:
            last_base = max(_as_datetime(x["startTime"]) for x in truth("baseline")["roots"])
            first_inc = min(_as_datetime(x["startTime"]) for x in truth("incident")["roots"])
            if not (last_base - skew <= deploy[0]["at"] <= first_inc + skew):
                problems.append(f"the deploy audit ({deploy[0]['at']}) is not between the baseline and the incident")
        promote = audits("promote", fix)
        cand = audits("candidate", fix)
        if not promote:
            problems.append(f"no 'promote' audit comment on v{fix}: promote = move(version, [PRODUCTION, LIVE], ...)")
        if not cand:
            problems.append(f"no 'candidate' audit comment on v{fix} (the fix goes up for review under `hotfix` first)")
        if promote:
            g = gates()["fix"]
            shipped = _as_datetime(gate_score(g).get("timestamp"))
            first_rec = min(_as_datetime(x["startTime"]) for x in truth("recovered")["roots"])
            if promote[0]["at"] + skew < shipped:
                problems.append(f"promoted at {promote[0]['at']}, before the gate shipped it ({shipped})")
            if first_rec + skew < promote[0]["at"]:
                problems.append("the recovered window started before the promotion")
        if problems:
            return False, "; ".join(problems[:4])
        return True, (f"deploy (live -> v{bad}) at {deploy[0]['at']:%H:%M:%S}, fix v{fix} up for review at "
                      f"{cand[0]['at']:%H:%M:%S}, promoted (production + live) at {promote[0]['at']:%H:%M:%S} after the "
                      f"gate shipped it; {len(s['audits'])} audit comment(s) on the prompt")

    # 10 -----------------------------------------------------------------------------------------------
    def recovered_check():
        d = drop("recovered")
        if d is not None and d >= LOOP_MIN_DROP:
            return False, f"the recovered window is still {_p(d)} below the baseline: the promoted fix isn't healthy"
        fire = firing_row()
        fp = fire["envelope"].get("fingerprint")
        rows = [r for r in alert_rows() if _accepted(r) and r["envelope"].get("status") == "resolved"
                and r["envelope"].get("fingerprint") == fp]
        if len(rows) != 1:
            return False, f"{len(rows)} accepted resolved deliveries for {fp} (want 1, from tick 'resolve')"
        r, q = rows[0], rows[0]["envelope"]
        problems = []
        if q.get("id") not in sent_ids("resolve"):
            problems.append("the resolved delivery is not one tick 'resolve' sent")
        if str(r.get("received_at")) <= str(fire.get("received_at")):
            problems.append("resolved arrived before the firing alert")
        if not signed_with(r, alert_secret):
            problems.append("its signature does not verify with the alert secret")
        if window_of(q.get("current") or {}) != "recovered":
            problems.append("its current window is not the recovered window")
        if not _near((q.get("current") or {}).get("value"), truth("recovered")["rate"], rel=1e-6):
            problems.append(f"current value {(q.get('current') or {}).get('value')}, stored {_p(truth('recovered')['rate'])}")
        if problems:
            return False, "; ".join(problems)
        return True, (f"recovered judge pass rate {_p(truth('recovered')['rate'])} (baseline "
                      f"{_p(truth('baseline')['rate'])}); one signed resolved delivery: {q.get('title')}")

    # 11 -----------------------------------------------------------------------------------------------
    def evidence_check():
        if evidence is None:
            return False, "pass evidence=PATHS['evidence'] (TODO 5: collect_evidence + the given report cell)"
        path = Path(evidence)
        if not path.exists():
            return False, f"{path} does not exist (run the report cell after TODO 5)"
        e = json.loads(path.read_text(encoding="utf-8"))
        problems = list(loop_spec.evidence_problems(e)) + [f"the trail itself reports: {p}" for p in e.get("problems") or []]
        for f in ("markdown", "html"):
            if not (path.parent / loop_spec.FILES[f]).exists():
                problems.append(f"{loop_spec.FILES[f]} missing next to {path.name}")
        if problems:
            return False, "; ".join(problems[:4])
        if e.get("incident") != incident or e.get("run_tag") != run_tag or e.get("prompt") != names["prompt"]:
            return False, f"incident/run_tag/prompt {e.get('incident')}/{e.get('run_tag')}/{e.get('prompt')} are not this run's"
        by = {}
        for x in e["events"]:
            by.setdefault(x["stage"], []).append(x)
        v, s = served(), snap()
        bad = []

        def near_time(a, b) -> bool:
            return a is not None and b is not None and abs(_as_datetime(a) - _as_datetime(b)) <= skew

        for w in ("baseline", "recovered"):
            n, t = by[w][0]["numbers"], truth(w)
            if n.get("requests") != t["n"] or not _near(n.get("judge_pass_rate"), t["rate"], rel=1e-6) \
                    or n.get("version") != t["version"]:
                bad.append(f"{w}: {n} vs stored {t['n']} requests, pass rate {_p(t['rate'])}, {t['version']}")
            if not near_time(by[w][0]["at"], t["roots"][0]["startTime"]):
                bad.append(f"{w}: at {by[w][0]['at']} is not the window's first request")
        dep = audits("deploy", v["incident"])
        if by["deploy"][0]["numbers"].get("to_version") != v["incident"] or not dep \
                or not near_time(by["deploy"][0]["at"], dep[0]["at"]):
            bad.append(f"deploy: {by['deploy'][0]['numbers']} at {by['deploy'][0]['at']} vs the audit comment")
        j, inc = by["judge"][0], truth("incident")
        links = " ".join(str(u) for u in (j.get("links") or {}).values())
        if set(j["refs"].get("trace_ids") or []) != set(inc["flagged"]) or j["numbers"].get("flagged") != len(inc["flagged"]):
            bad.append(f"judge: flagged {j['refs'].get('trace_ids')} vs stored {inc['flagged']}")
        elif not all(t in links for t in inc["flagged"]):
            bad.append("judge: every flagged trace needs its trace link")
        fire = firing_row()
        a = by["alert"][0]
        if a["refs"].get("id") != fire["envelope"].get("id") or not near_time(a["at"], fire.get("received_at")):
            bad.append(f"alert: id {a['refs'].get('id')} at {a['at']} vs the delivery {fire['envelope'].get('id')}")
        rv = by["review"][0]
        if rv["refs"].get("queue_id") != (s["queue"] or {}).get("id") or rv["numbers"].get("queued") != len(s["queue_items"]) \
                or rv["numbers"].get("confirmed_failures") != len(confirmed()):
            bad.append(f"review: {rv['numbers']} / queue {rv['refs'].get('queue_id')} vs stored {len(s['queue_items'])} "
                       f"item(s), {len(confirmed())} confirmed")
        last_done = max((_as_datetime(i.get("completedAt")) for i in s["queue_items"] if i.get("completedAt")), default=None)
        if not near_time(rv["at"], last_done):
            bad.append(f"review: at {rv['at']} is not the last completedAt ({last_done})")
        ds = by["dataset"][0]
        if set(ds["refs"].get("item_ids") or []) != set(incident_ids()) or ds["refs"].get("dataset_id") != (s["dataset"] or {}).get("id"):
            bad.append(f"dataset: items {ds['refs'].get('item_ids')} vs the confirmed failures' {sorted(incident_ids())}")
        g = gates()
        if {x["numbers"].get("version") for x in by["fix"]} != {int(meta(g[k]).get("prompt_version")) for k in g}:
            bad.append(f"fix: versions {[x['numbers'].get('version') for x in by['fix']]} vs the gate runs'")
        if {x["refs"].get("candidate_experiment_id") for x in by["experiment"]} != {g[k]["id"] for k in g}:
            bad.append("experiment: candidate experiment ids are not this run's two gate runs")
        for x in by["experiment"]:
            if not all(str(x["refs"].get(k)) in str(x["links"].get(lk)) for k, lk in
                       (("candidate_experiment_id", "candidate"), ("baseline_experiment_id", "baseline"))):
                bad.append("experiment: each link must point at its experiment")
                break
        for x in by["gate"]:
            eid = x["refs"].get("experiment_id")
            run = next((g[k] for k in g if g[k]["id"] == eid), None)
            if run is None or x["numbers"].get("decision") != gate_score(run).get("value"):
                bad.append(f"gate: {eid} decision {x['numbers'].get('decision')} vs stored")
        if by["promote"][0]["numbers"].get("version") != v["recovered"]:
            bad.append(f"promote: v{by['promote'][0]['numbers'].get('version')} vs the promoted v{v['recovered']}")
        res = [r for r in alert_rows() if _accepted(r) and r["envelope"].get("status") == "resolved"
               and r["envelope"].get("fingerprint") == fire["envelope"].get("fingerprint")]
        if not res or by["resolved"][0]["refs"].get("id") != res[0]["envelope"].get("id"):
            bad.append("resolved: id is not the resolved delivery's")
        first = {st: min(_as_datetime(x["at"]) for x in xs) for st, xs in by.items()}
        ttd = (first["alert"] - first["deploy"]).total_seconds()
        if not _near((e.get("durations") or {}).get("time_to_detect_s"), ttd, rel=0.01, abs_tol=1.0) or ttd <= 0:
            bad.append(f"durations: time_to_detect_s {(e.get('durations') or {}).get('time_to_detect_s')} vs {ttd:.1f}")
        for x in e["events"]:
            if any(not str(u).startswith("http") for u in (x.get("links") or {}).values()):
                bad.append(f"{x['stage']}: a link is not a URL")
                break
        if bad:
            return False, "; ".join(bad[:4])
        d = e["durations"]
        return True, (f"{len(e['events'])} events, every stage matches the stored data; detect "
                      f"{d['time_to_detect_s']:.0f} s, fix {d['time_to_fix_s']:.0f} s, recover (MTTR) "
                      f"{d['time_to_recover_s']:.0f} s; {path.parent / loop_spec.FILES['html']}")

    # 12 -----------------------------------------------------------------------------------------------
    def native_prompt_check():
        ready, _ = alert_spec.native_webhooks_ready()
        if not ready:
            raise FeatureUnavailable(alert_spec.NATIVE_HINT)
        if not native_secret:
            raise FeatureUnavailable(f"optional: module 12's prompt automation ({alert_spec.SECRET_ENV['prompt']} in "
                                     ".env) would announce this loop's deploy and promotion of `live`")
        v = served()
        started = min(_as_datetime(r["startTime"]) for r in snap()["roots"]) - timedelta(minutes=5)
        rows = [r for r in receiver_rows(deliveries) if r.get("path") == alert_spec.PROMPT_PATH and _accepted(r)
                and (r["envelope"] or {}).get("type") == "prompt-version"
                and (r["envelope"].get("prompt") or {}).get("name") == names["prompt"]
                and _as_datetime(r.get("received_at")) >= started]

        def live_on(r, version):
            p = r["envelope"].get("prompt") or {}
            return p.get("version") == version and loop_spec.LIVE_LABEL in (p.get("labels") or [])

        dep = [r for r in rows if live_on(r, v["incident"])]
        pro = [r for r in rows if live_on(r, v["recovered"])]
        if not dep or not pro:
            return False, (f"native prompt events: deploy (v{v['incident']} gains live) {'seen' if dep else 'missing'}, "
                           f"promotion (v{v['recovered']}) {'seen' if pro else 'missing'}")
        if not all(signed_with(r, native_secret) for r in (dep[0], pro[0])):
            return False, "a native prompt delivery's signature does not verify"
        return True, f"Langfuse announced the deploy (v{v['incident']}) and the promotion (v{v['recovered']}), signed"

    return [
        Check("traffic: baseline (approved), incident (planted deploy), recovered (promoted fix)", traffic_check),
        Check("live traffic judged: the judge sees the planted regression", judged_check),
        Check("the alert fires once, signed, with the stored judge pass rates", alert_check),
        Check("review queue = the alert window's judge flags + a control, no duplicates", queue_check),
        Check("human review complete (UI or API), labels with config + queue", review_check),
        Check("confirmed failures became labelled dataset items (the incident's tests)", dataset_check),
        Check("planted: the quick fix is blocked by the gate and never promoted", planted_check),
        Check("the fix ships through the gate on the incident's tests", fix_check),
        Check("promotion: production + live moved to the fix, audited, in order", promotion_check),
        Check("recovered: healthy traffic and one resolved alert", recovered_check),
        Check("evidence trail matches the stored data", evidence_check),
        Check("native prompt webhook on the deploy and the promotion (module 12)", native_prompt_check),
    ]


def check_module_14(run_tag: str, *, prefix: str = "", environment: str = "production", deliveries=None,
                    alert_secret: str | None = None, ticks: dict | None = None, queued=None, items=None, pin=None,
                    releases: dict | None = None, evidence=None, client=None, allow_dry: bool = False,
                    retries: int = 12, **run_kwargs) -> bool:
    """See module_14_checks for the inputs (all produced by the notebook)."""
    client = client or get_client()
    checks = module_14_checks(run_tag, client, prefix=prefix, environment=environment, deliveries=deliveries,
                              alert_secret=alert_secret, ticks=ticks, queued=queued, items=items, pin=pin,
                              releases=releases, evidence=evidence, allow_dry=allow_dry)
    return _check_module(checks, client, retries=retries, **run_kwargs)
