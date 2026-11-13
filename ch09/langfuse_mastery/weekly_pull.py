"""Module 11: the weekly metrics pull. A script a Monday-morning cron job runs:

    $PY -m langfuse_mastery.weekly_pull --env production --out reports/            # the last two ISO weeks
    $PY -m langfuse_mastery.weekly_pull --tag run:m11-abc123 --env dev \\
        --prev 2026-11-12T08:00:00+00:00 2026-11-12T08:01:30+00:00 \\
        --curr 2026-11-12T08:01:30+00:00 2026-11-12T08:03:00+00:00 --out ...       # the notebook's two windows

It reads Langfuse only (no writes) and writes `weekly-report.json` + `weekly-report.md`.

You implement TODO 2 (`scope_filters`, `health_metrics`: the Metrics API) and TODO 3 (`fetch_observations`,
`rollups`: the public API pull + client-side aggregation). The rest is given. The metric keys are listed in
`analytics.py`.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from langfuse_mastery import analytics

ROOT_NAME = "support-request"
FEEDBACK = "user_feedback"          # module 06's end-user feedback (CATEGORICAL thumbs_up / thumbs_down)
JUDGE = "routing_judge"             # module 07's LLM judge (source EVAL) or module 08's stand-in (source API)
HUMAN = "human_routing_ok"          # module 08's human verdict (source ANNOTATION, config-backed: PREFIX + HUMAN)
TOP_N = 5
LEVELS = {"DEFAULT": 0, "WARNING": 0, "ERROR": 0}   # always report every level and label (0 when absent):
FEEDBACK_LABELS = {"thumbs_up": 0, "thumbs_down": 0}  # a share missing last week can't be compared with this week
JUDGE_EVALUATOR_PREFIX = "stand-in"  # metadata.evaluator of the stand-in judge


@dataclass(frozen=True)
class Scope:
    """What the pull looks at. In production: `environment` only. In the lab: also the run tag."""
    tags: tuple = ()
    environment: str | None = None
    prefix: str = ""                 # PREFIX of the human score's config name
    extra: dict = field(default_factory=dict)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------- given: one Metrics API call (module 03 recap)
def query(langfuse, view: str, metrics, *, window, dimensions=(), filters=(), time_granularity=None,
          order_by=None, row_limit=None) -> list[dict]:
    """One Metrics API v2 query. `metrics` = [(measure, aggregation)]; rows come back with keys like `p95_latency`,
    `count_count`, `sum_totalCost`, `avg_value` (aggregation_measure) plus one key per dimension.
    `order_by` = [(field, "desc")]; `row_limit` (<= 1000). High-cardinality dimensions (userId, sessionId, traceId)
    need BOTH (top-N only)."""
    body = {"view": view, "metrics": [{"measure": m, "aggregation": a} for m, a in metrics],
            "dimensions": [{"field": d} for d in dimensions], "filters": list(filters),
            "fromTimestamp": iso(window[0]), "toTimestamp": iso(window[1])}
    if time_granularity:
        body["timeDimension"] = {"granularity": time_granularity}
    if order_by:
        body["orderBy"] = [{"field": f, "direction": d} for f, d in order_by]
    if row_limit:
        body["config"] = {"row_limit": row_limit}
    return list(langfuse.api.metrics.metrics(query=json.dumps(body)).data)


ROOTS = [{"column": "isRootObservation", "operator": "=", "value": True, "type": "boolean"},
         {"column": "name", "operator": "=", "value": ROOT_NAME, "type": "string"}]


def _type(t: str) -> dict:
    return {"column": "type", "operator": "=", "value": t, "type": "string"}


# ---------------------------------------------------------------- TODO 2: the Metrics API
def scope_filters(scope: Scope) -> list[dict]:
    """TODO 2a. Metrics API filters for `scope` that work on EVERY view (observations and the score views):
    - scope.tags        -> {"column": "tags", "operator": "any of", "value": [...], "type": "arrayOptions"}
    - scope.environment -> {"column": "environment", "operator": "=", "value": ..., "type": "string"}
    Nothing for an empty part. (The observations API calls the same tag column `traceTags`: not here.)"""
    raise NotImplementedError("TODO 2: implement scope_filters")


def health_metrics(langfuse, window, scope: Scope) -> dict:
    """TODO 2b. The window's health, from the Metrics API only. Returns

        {"metrics": {...},        # the Metrics-API keys of analytics.py: requests, latency_ms.request.p50/.p95,
                                  # latency_ms.tool.<tool>.p95, latency_ms.generation.p95, level_share.<LEVEL>,
                                  # error_rate, cost_usd.total, tokens.total, score.<name>.<source>,
                                  # score_count.<name>.<source>, feedback_share.<label>
         "by_version": {version: {"requests", "latency_ms.request.p95", "cost_usd.total"}},
         "top_users": [{"user_id", "cost_usd"}, ...]}   # top TOP_N users by cost, most expensive first

    One `query(...)` per question, every one with `scope_filters(scope)`:
    - requests + request latency: view observations, filters + ROOTS, metrics count/count, latency/p50, latency/p95
    - per tool: dimensions ["name"], filter type = TOOL (`_type("TOOL")`); generations: type = GENERATION
    - levels: roots grouped by "level", starting from LEVELS (every level, 0 when absent) -> analytics.shares +
      analytics.error_rate
    - cost and tokens: all observations, totalCost/sum, totalTokens/sum (None -> 0: an empty sum is None)
    - quality: view scores-boolean, dimensions ["name", "source"], value/avg (= pass rate) and count/count
    - feedback: view scores-categorical, dimensions ["stringValue"], filter name = FEEDBACK, starting from
      FEEDBACK_LABELS (a label nobody clicked last week is a share of 0, not a missing metric: otherwise
      compare() sees None and a jump from 0 to 40% thumbs-down is never flagged)
    - by version: roots grouped by "version" (requests, p95) + all observations grouped by "version" (cost)
    - top users: dimensions ["userId"] is HIGH-CARDINALITY: it needs order_by=[("sum_totalCost", "desc")] AND
      row_limit=TOP_N, otherwise the API answers 400. That is the Metrics API's limit: top-N, never "every user".
    Use analytics.flatten for grouped rows (result keys are aggregation_measure: p95_latency, count_count,
    sum_totalCost, avg_value). A query with no matching rows may return [] or one row of Nones."""
    raise NotImplementedError("TODO 2: implement health_metrics")


# ---------------------------------------------------------------- TODO 3: the public API + client-side aggregation
def fetch_observations(langfuse, window, scope: Scope) -> list[dict]:
    """TODO 3a. Every observation that STARTED in [window[0], window[1]) in scope, as plain dicts (`.dict()`),
    ALL pages. `langfuse.api.observations.get_many(fields="core,basic,usage,metrics", filter=json.dumps([...]),
    limit=1000, cursor=...)`; follow `resp.meta.cursor` until it is None. The filter is a JSON list:
    startTime ">=" from and "<" to (type "datetime", ISO strings), the tags on column `traceTags` (arrayOptions,
    "any of"; NOT `tags` as in the Metrics API) and environment (string "=")."""
    raise NotImplementedError("TODO 3: implement fetch_observations")


def fetch_scores(langfuse, trace_ids) -> dict:
    """Given: {trace_id: [score dicts]} (fields details,subject: comment, metadata, configId, subject)."""
    return {t: [s.dict() for s in langfuse.api.scores.get_many(trace_id=t, fields="details,subject", limit=100).data]
            for t in trace_ids}


def is_judge(score: dict) -> bool:
    """Given: an LLM judge (source EVAL) or the stand-in judge (source API, metadata.evaluator stand-in*)."""
    source = str(getattr(score.get("source"), "value", score.get("source")))
    evaluator = str((score.get("metadata") or {}).get("evaluator") or "")
    return source == "EVAL" or (source == "API" and evaluator.startswith(JUDGE_EVALUATOR_PREFIX))


def rollups(langfuse, window, scope: Scope) -> dict:
    """TODO 3b. What the Metrics API can't answer, from one pull + client-side joins. Returns

        {"metrics": {"resolved_rate", "cost_usd.per_request", "cost_usd.per_resolved",
                     "judge_human.agreement", "judge_human.pairs"},
         "by_user": analytics.rollup(..., by="user_id"), "by_session": analytics.rollup(..., by="session_id")}

    Steps: fetch_observations -> analytics.per_trace -> fetch_scores (given) -> analytics.resolved.
    Judge vs human, per trace: the judge's verdict = analytics.latest_value of the scores that `is_judge` (given)
    named JUDGE; the human's = analytics.latest_value(rows, scope.prefix + HUMAN, sources={"ANNOTATION"});
    analytics.agreement_rate over those pairs ("rate" and "n"). Rates over no traces are None."""
    raise NotImplementedError("TODO 3: implement rollups")


# ---------------------------------------------------------------- given: the report
def window_report(langfuse, window, scope: Scope) -> dict:
    health, roll = health_metrics(langfuse, window, scope), rollups(langfuse, window, scope)
    return {"from": iso(window[0]), "to": iso(window[1]), "metrics": {**health["metrics"], **roll["metrics"]},
            "by_version": health["by_version"], "top_users": health["top_users"],
            "by_user": roll["by_user"], "by_session": roll["by_session"]}


def build_report(langfuse, prev, curr, scope: Scope, rules=analytics.DEFAULT_RULES) -> dict:
    windows = {"prev": window_report(langfuse, prev, scope), "curr": window_report(langfuse, curr, scope)}
    comparison = analytics.compare(windows["prev"]["metrics"], windows["curr"]["metrics"])
    return {"generated_at": iso(datetime.now(timezone.utc)),
            "scope": {"tags": list(scope.tags), "environment": scope.environment},
            "rules": [dict(r) for r in rules], "windows": windows, "comparison": comparison,
            "anomalies": analytics.flag(comparison, rules)}


def _fmt(v, metric: str = "") -> str:
    if v is None:
        return "–"
    if metric.startswith("cost_usd"):
        return f"${v:.5f}"
    if metric.startswith("latency_ms"):
        return f"{v:,.0f} ms"
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def markdown(report: dict) -> str:
    w = report["windows"]
    lines = [f"# Weekly agent health ({report['scope'].get('environment') or 'all environments'})", "",
             f"- previous: {w['prev']['from']} → {w['prev']['to']}",
             f"- current:  {w['curr']['from']} → {w['curr']['to']}", ""]
    anomalies = report["anomalies"]
    lines += ["## Regressions" if anomalies else "## No regressions", ""]
    for a in anomalies:
        pct = "new" if a["pct"] is None else f"{a['pct']:+.0%}"
        lines.append(f"- **{a['metric']}** {a['direction']}: {_fmt(a['prev'], a['metric'])} → "
                     f"{_fmt(a['curr'], a['metric'])} ({pct}; rule `{a['rule']}`)")
    lines += ["", "| metric | previous | current | change |", "|---|---|---|---|"]
    for metric, c in report["comparison"].items():
        pct = "" if c["pct"] is None else f"{c['pct']:+.0%}"
        lines.append(f"| {metric} | {_fmt(c['prev'], metric)} | {_fmt(c['curr'], metric)} | {pct} |")
    lines += ["", "| version | requests | request p95 | cost |", "|---|---|---|---|"]
    versions = {**w["prev"]["by_version"], **w["curr"]["by_version"]}
    for v, row in sorted(versions.items()):
        lines.append(f"| {v} | {row.get('requests', '–')} | {_fmt(row.get('latency_ms.request.p95'), 'latency_ms')} "
                     f"| {_fmt(row.get('cost_usd.total'), 'cost_usd')} |")
    lines += ["", "Top users by cost (current): " + ", ".join(
        f"{u['user_id']} {_fmt(u['cost_usd'], 'cost_usd')}" for u in w["curr"]["top_users"]) or "–"]
    return "\n".join(lines) + "\n"


def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.strip())
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tag", action="append", default=[], help="only traces with this tag (repeatable)")
    p.add_argument("--env", help="only this environment")
    p.add_argument("--prefix", default="", help="PREFIX of the human review score config")
    p.add_argument("--prev", nargs=2, metavar=("FROM", "TO"))
    p.add_argument("--curr", nargs=2, metavar=("FROM", "TO"))
    p.add_argument("--out", default=".", help="folder for weekly-report.json / .md")
    args = p.parse_args(argv)
    if bool(args.prev) != bool(args.curr):
        p.error("--prev and --curr go together")
    prev, curr = ((tuple(map(_dt, args.prev)), tuple(map(_dt, args.curr))) if args.prev
                  else analytics.week_windows(datetime.now(timezone.utc)))
    from langfuse import get_client

    report = build_report(get_client(), prev, curr, Scope(tags=tuple(args.tag), environment=args.env,
                                                          prefix=args.prefix))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "weekly-report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (out / "weekly-report.md").write_text(markdown(report), encoding="utf-8")
    print(f"{len(report['anomalies'])} regression(s); wrote {out / 'weekly-report.md'}")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    from langfuse_lab.config import ENV_FILE
    load_dotenv(ENV_FILE)
    raise SystemExit(main())
