"""Module 11: the agent-health dashboard, as data. No Langfuse code here.

The notebook's TODO 4 turns these specs into widgets + one dashboard through `api.unstable.dashboard_widgets` /
`api.unstable.dashboards`; verify compares what the server stored with these specs (`widget_problems`).

A spec's `filters` are in the Metrics API / widget filter shape. Widget names on the server are
`PREFIX + WIDGET_PREFIX + spec["name"]` (project-global: give every widget of a dashboard the same prefix).
Server rules (verified live, 4.77): only PIVOT_TABLE may have more than one dimension; unknown dimensions or filter
columns are rejected with 400.
"""
from __future__ import annotations

DASHBOARD = "agent-health"
DESCRIPTION = "Support agent health: traffic, latency, errors, cost and quality per version (module 11)."
WIDGET_PREFIX = "agent-health · "
ROOT_NAME = "support-request"

ROOT_FILTERS = (
    {"column": "isRootObservation", "operator": "=", "type": "boolean", "value": True},
    {"column": "name", "operator": "=", "type": "string", "value": ROOT_NAME},
)
TOOL_FILTER = {"column": "type", "operator": "=", "type": "string", "value": "TOOL"}
FEEDBACK_FILTER = {"column": "name", "operator": "=", "type": "string", "value": "user_feedback"}

WIDGETS = (
    {"key": "requests", "name": "Requests by version", "view": "observations", "dimensions": ["version"],
     "metrics": [["count", "count"]], "filters": list(ROOT_FILTERS), "chart_type": "VERTICAL_BAR"},
    {"key": "request_latency", "name": "Request latency p95 (trend)", "view": "observations", "dimensions": [],
     "metrics": [["latency", "p95"]], "filters": list(ROOT_FILTERS), "chart_type": "LINE_TIME_SERIES"},
    {"key": "tool_latency", "name": "Tool latency p50/p95 by version", "view": "observations",
     "dimensions": ["name", "version"], "metrics": [["latency", "p50"], ["latency", "p95"]],
     "filters": [TOOL_FILTER], "chart_type": "PIVOT_TABLE"},
    {"key": "levels", "name": "Requests by level", "view": "observations", "dimensions": ["level"],
     "metrics": [["count", "count"]], "filters": list(ROOT_FILTERS), "chart_type": "PIE"},
    {"key": "cost", "name": "Cost by version (USD)", "view": "observations", "dimensions": ["version"],
     "metrics": [["totalCost", "sum"]], "filters": [], "chart_type": "VERTICAL_BAR"},
    {"key": "quality", "name": "Pass rate by score and source", "view": "scores-boolean",
     "dimensions": ["name", "source"], "metrics": [["value", "avg"], ["count", "count"]], "filters": [],
     "chart_type": "PIVOT_TABLE"},
    {"key": "feedback", "name": "User feedback", "view": "scores-categorical", "dimensions": ["stringValue"],
     "metrics": [["count", "count"]], "filters": [FEEDBACK_FILTER], "chart_type": "PIE"},
)
BY_KEY = {w["key"]: w for w in WIDGETS}


def widget_name(spec: dict, prefix: str = "") -> str:
    return f"{prefix}{WIDGET_PREFIX}{spec['name']}"


def dashboard_filters(environment: str) -> list[dict]:
    """Dashboard-level filters: every widget only sees the agent's own environment (not `sdk-experiment` or the judge's
    `langfuse-llm-as-a-judge` traces)."""
    return [{"column": "environment", "operator": "any of", "type": "stringOptions", "value": [environment]}]


def _s(v) -> str:
    return str(getattr(v, "value", v))


def _filter_key(f: dict) -> tuple:
    value = f.get("value")
    value = tuple(sorted(map(str, value))) if isinstance(value, (list, tuple)) else str(value).lower()
    return (f.get("column"), _s(f.get("operator")), _s(f.get("type")), value, f.get("key"))


def has_filter(filters, wanted: dict) -> bool:
    return _filter_key(wanted) in {_filter_key(f) for f in filters or ()}


def widget_problems(widget: dict, spec: dict, *, prefix: str = "") -> list[str]:
    """What differs between a stored widget (`DashboardWidget.dict()`: camelCase, enums allowed) and its spec.

    Checked: name, view, chartType, dimension fields (in order), the (measure, agg) set, and that every spec filter is
    present (extra filters are reported too: they silently hide data)."""
    out = []
    if widget.get("name") != widget_name(spec, prefix):
        out.append(f"name {widget.get('name')!r} != {widget_name(spec, prefix)!r}")
    if _s(widget.get("view")) != spec["view"]:
        out.append(f"view {_s(widget.get('view'))} != {spec['view']}")
    if _s(widget.get("chartType")) != spec["chart_type"]:
        out.append(f"chartType {_s(widget.get('chartType'))} != {spec['chart_type']}")
    dims = [d.get("field") for d in widget.get("dimensions") or ()]
    if dims != spec["dimensions"]:
        out.append(f"dimensions {dims} != {spec['dimensions']}")
    metrics = {(m.get("measure"), _s(m.get("agg"))) for m in widget.get("metrics") or ()}
    want = {tuple(m) for m in spec["metrics"]}
    if metrics != want:
        out.append(f"metrics {sorted(metrics)} != {sorted(want)}")
    filters = widget.get("filters") or []
    missing = [f["column"] for f in spec["filters"] if not has_filter(filters, f)]
    if missing:
        out.append(f"missing filter(s) on {missing}")
    extra = [f.get("column") for f in filters if not any(_filter_key(f) == _filter_key(s) for s in spec["filters"])]
    if extra:
        out.append(f"unexpected filter(s) on {extra}")
    return out
