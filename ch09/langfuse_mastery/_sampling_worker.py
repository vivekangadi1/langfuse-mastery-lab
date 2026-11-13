"""Sampling worker (module 05, TODO 4a): a high-volume service, run in its OWN process.

Why a separate process? The sampler is installed on the OpenTelemetry TracerProvider by the FIRST Langfuse client of a
process, and a client created later never changes it (verified live: `Langfuse(sample_rate=0.0)` built after
`get_client()` still exported its trace). Your notebook kernel already has a client, so the sampled "service" gets
a fresh process, exactly like a real deployment configures sampling once, at startup.

stdin:  {"tag": ..., "run_id": ..., "n": 40, "rate": 0.25}
stdout: the LAST line is {"trace_ids": [...]}: every trace id this worker created, kept or not.
"""
import json
import sys

from langfuse import Langfuse, propagate_attributes
from langfuse_lab import kb  # importable: run_worker puts ch09/ on PYTHONPATH
from langfuse_lab.fixtures import load_fixtures
from langfuse_lab.guardrail import check_input

QUESTIONS = ("What is your refund policy?", "How long do returns take?", "Where is my order 8103?",
             "Can I change my shipping address?")


def make_client(rate: float) -> Langfuse:
    """TODO 4a: return a Langfuse client that keeps only `rate` (0 < rate < 1) of all traces.

    - It must be the FIRST client this process creates (see the module docstring).
    - The keys and base URL come from the environment (LANGFUSE_PUBLIC_KEY / _SECRET_KEY / _BASE_URL), as usual.
    - Two equivalent ways: a constructor argument, or the LANGFUSE_SAMPLE_RATE environment variable read when the
      client is built. Pick one.
    """
    raise NotImplementedError("TODO 4a in langfuse_mastery/_sampling_worker.py: build the sampled client")


def emit(langfuse: Langfuse, *, tag: str, run_id: str, n: int) -> list[str]:
    """Given: n cheap traces (no LLM call): support-request -> guardrail + kb-search, 3 observations each.

    Trace ids are seeded (`create_trace_id(seed=...)`), so which ones the sampler keeps is reproducible.
    """
    fixtures = load_fixtures()
    trace_ids = []
    for i in range(n):
        trace_id = Langfuse.create_trace_id(seed=f"{run_id}-sample-{i}")
        question = QUESTIONS[i % len(QUESTIONS)]
        with langfuse.start_as_current_observation(name="support-request", as_type="agent",
                                                   trace_context={"trace_id": trace_id}) as root:
            with propagate_attributes(tags=[tag], session_id=f"{run_id}-sample-{i:03d}"):
                with langfuse.start_as_current_observation(name="guardrail", as_type="guardrail",
                                                           input=question) as guard:
                    guard.update(output=list(check_input(question).flags))
                with langfuse.start_as_current_observation(name="kb-search", as_type="retriever",
                                                           input=question) as search:
                    search.update(output=[a["id"] for a in kb.search(question, fixtures.kb, k=3)])
                root.update(input=question, output="(sampling demo: no LLM call)")
        trace_ids.append(trace_id)
    return trace_ids


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    langfuse = make_client(float(payload["rate"]))
    ids = emit(langfuse, tag=payload["tag"], run_id=payload["run_id"], n=int(payload["n"]))
    langfuse.flush()
    print(json.dumps({"trace_ids": ids}))  # must stay the LAST stdout line
