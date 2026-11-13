"""Deployment worker (module 13, TODO 3): one deployment of the support service = one process with ITS OWN client.

Why a process per deployment? The SDK keeps one client per process (module 05's singleton trap): the client a process
builds first fixes its environment, release, batching, timeout and sampling. A real deployment configures them once, at
startup, from its environment variables; here the notebook passes them as `client` in the payload.

stdin:  the first line is the JSON payload: {"run_id", "scenario", "tags", "user_id", "name", "n", "pace_s",
        "flush_each", "exit": "finish" | "killed", "hold", "client": {"base_url", "environment", "release",
        optional "public_key", "secret_key", "flush_at", "flush_interval", "timeout", "sample_rate"}}
stdout: with "hold": first {"held": true, "trace_ids": [...]}, then the worker waits for one more stdin line.
        The LAST line is {"trace_ids", "latencies_ms", "served_s", "exit", "exit_s", "spans_per_request"}.
"""
import json
import os
import sys
import time

from langfuse import Langfuse, propagate_attributes
from langfuse_lab import kb  # importable: ops_lab.start_worker puts ch09/ on PYTHONPATH
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input

QUESTIONS = ("What is your refund policy?", "How long do returns take?", "Where is my order 8103?",
             "Can I change my shipping address?")
SPANS_PER_REQUEST = 3  # the request + guardrail + kb-search


def make_client(cfg: dict) -> Langfuse:
    """TODO 3a: the deployment's Langfuse client, built from `cfg` (the payload's "client").

    Map these keys to the constructor, and only pass the ones that are present and not None (a missing key means
    "the SDK default / the LANGFUSE_* environment variable"): public_key, secret_key, base_url, environment, release,
    flush_at, flush_interval, timeout, sample_rate.
    """
    raise NotImplementedError("TODO 3a in langfuse_mastery/_ops_worker.py: build the deployment's client")


def finish(langfuse: Langfuse) -> None:
    """TODO 3b: what a job (or a server's shutdown hook) must call before its process exits, so that nothing still
    buffered is lost, and the SDK's background threads stop."""
    raise NotImplementedError("TODO 3b in langfuse_mastery/_ops_worker.py: flush and stop the client")


def serve(langfuse: Langfuse, p: dict) -> dict:
    """Given: n cheap requests (no LLM call): support-request -> guardrail + kb-search, 3 observations each. Trace ids
    are seeded (run, scenario, i), so every scenario's ids are known and reproducible."""
    fixtures = load_fixtures()
    ids, latencies = [], []
    started = time.time()
    for i in range(int(p["n"])):
        t0 = time.perf_counter()
        trace_id = Langfuse.create_trace_id(seed=f"{p['run_id']}-{p['scenario']}-{i}")
        question = QUESTIONS[i % len(QUESTIONS)]
        with langfuse.start_as_current_observation(name=p.get("name") or "support-request", as_type="agent",
                                                   trace_context={"trace_id": trace_id}) as root:
            with propagate_attributes(tags=list(p.get("tags") or []), user_id=p.get("user_id"),
                                      session_id=f"{p['run_id']}-{p['scenario']}-{i:03d}"):
                with langfuse.start_as_current_observation(name="guardrail", as_type="guardrail",
                                                           input=question) as guard:
                    guard.update(output=list(check_input(question).flags))
                with langfuse.start_as_current_observation(name="kb-search", as_type="retriever",
                                                           input=question) as search:
                    search.update(output=[a["id"] for a in kb.search(question, fixtures.kb, k=3)])
                root.update(input=question, output="(ops lab: no LLM call)")
        if p.get("flush_each"):
            langfuse.flush()  # the anti-pattern this lab measures: a network round trip inside the request
        latencies.append((time.perf_counter() - t0) * 1000.0)
        ids.append(trace_id)
        if p.get("pace_s"):
            time.sleep(float(p["pace_s"]))
    return {"trace_ids": ids, "latencies_ms": latencies, "served_s": time.time() - started,
            "spans_per_request": SPANS_PER_REQUEST}


if __name__ == "__main__":
    payload = json.loads(sys.stdin.readline())
    langfuse = make_client(payload.get("client") or {})
    out = serve(langfuse, payload)
    if payload.get("hold"):
        print(json.dumps({"held": True, "trace_ids": out["trace_ids"]}), flush=True)
        sys.stdin.readline()  # the parent releases us; until then the spans sit in this process's SDK buffer
    t = time.time()
    if payload.get("exit") == "killed":
        print(json.dumps({**out, "exit": "killed", "exit_s": 0.0}), flush=True)
        os._exit(0)  # what an OOM kill, a SIGKILL or a frozen Lambda does: no atexit hook, no flush
    finish(langfuse)
    print(json.dumps({**out, "exit": "finish", "exit_s": time.time() - t}), flush=True)  # must stay the LAST line
