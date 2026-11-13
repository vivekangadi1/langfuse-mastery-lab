"""Module 11 verify checks against fakes shaped like the live API (server 4.77 / SDK 5.2, probed 2026-11-12):
observations as camelCase dicts (latency in SECONDS, level enum), Metrics API rows (`p95_latency` in ms, `count_count`,
`sum_totalCost`), dashboards/widgets from api.unstable, scores with fields details,subject."""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from langfuse_lab import dashboard_spec as ds
from langfuse_lab import s3
from langfuse_lab.verify import FeatureUnavailable, NotYet, module_11_checks

TAG = "run:m11-test"
T0 = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)
PREV = (T0, T0 + timedelta(minutes=2))
CURR = (T0 + timedelta(minutes=2), T0 + timedelta(minutes=4))
CASES = [("double_charge", "cust_00", "lookup_order", True), ("shipping_status", "cust_01", "lookup_shipment", True),
         ("unknown_order", "cust_04", "lookup_shipment", False), ("login_reset", "cust_02", "reset_password", True),
         ("refund_policy", "cust_05", "search_kb", True), ("cancel_subscription", "cust_03", "cancel_subscription",
                                                           True),
         ("injection", "cust_06", None, True)]


class Enum:
    def __init__(self, value):
        self.value = value


class Row:
    def __init__(self, d):
        self._d = d

    def dict(self):
        return dict(self._d)


def build(*, dry=False, slow_curr=True):
    """14 traces: root + one tool span (+ one generation) each; scores; returns (obs, scores)."""
    obs, scores = [], {}
    for w, (start, _) in (("prev", PREV), ("curr", CURR)):
        for i, (case, user, tool, handoff_ok) in enumerate(CASES):
            tid = f"{w}-{case}"
            t = start + timedelta(seconds=5 + 10 * i)
            level = "WARNING" if case in ("unknown_order", "injection") else "DEFAULT"
            tags = [TAG, case, f"window:{w}"] + (["slow_tool"] if w == "curr" else []) + (["dry-run"] if dry else [])
            common = {"traceId": tid, "userId": user, "sessionId": f"s-{tid}", "version": "v1" if w == "prev" else "v2",
                      "environment": "dev", "tags": tags, "projectId": "p1", "traceName": "support-request"}
            slow = w == "curr" and slow_curr and tool in ("lookup_order", "lookup_shipment")
            obs.append({**common, "id": f"r-{tid}", "name": "support-request", "type": "AGENT",
                        "isRootObservation": True, "parentObservationId": None, "startTime": t,
                        "latency": 5.0 if slow else 2.0, "level": Enum(level), "totalCost": None})
            if tool:
                obs.append({**common, "id": f"tool-{tid}", "name": tool, "type": "TOOL", "isRootObservation": False,
                            "parentObservationId": f"r-{tid}", "startTime": t + timedelta(seconds=1),
                            "latency": 3.001 if slow else 0.002, "level": Enum("DEFAULT"), "totalCost": None})
                obs.append({**common, "id": f"gen-{tid}", "name": "AzureChatOpenAI", "type": "GENERATION",
                            "isRootObservation": False, "parentObservationId": f"r-{tid}",
                            "startTime": t + timedelta(seconds=1.5), "latency": 0.8, "level": Enum("DEFAULT"),
                            "totalCost": 0.001 * (i + 1)})
            ts = t + timedelta(seconds=3)
            judge_ok = case != "refund_policy"   # the stand-in judge's false alarm
            rows = [{"id": f"hc-{tid}", "name": "handoff_correct", "value": handoff_ok, "source": Enum("API"),
                     "dataType": "BOOLEAN", "timestamp": ts, "metadata": {"evaluator": "code:v1"}},
                    {"id": f"j-{tid}", "name": "routing_judge", "value": judge_ok, "source": Enum("API"),
                     "dataType": "BOOLEAN", "timestamp": ts, "metadata": {"evaluator": "stand-in:keyword-v1"}},
                    {"id": f"fb-{tid}", "name": "user_feedback", "value": "thumbs_up" if handoff_ok else "thumbs_down",
                     "source": Enum("API"), "dataType": "CATEGORICAL", "timestamp": ts, "metadata": {}}]
            if not judge_ok or i == 0:
                rows.append({"id": f"h-{tid}", "name": "human_routing_ok", "value": handoff_ok,
                             "source": Enum("ANNOTATION"), "dataType": "BOOLEAN", "timestamp": ts + timedelta(seconds=30),
                             "metadata": {}})
            scores[tid] = rows
    return obs, scores


def _plain(v):
    return getattr(v, "value", v)


def _passes(o, f):
    col, op, val = f["column"], f["operator"], f.get("value")
    have = o.get(col)
    if col == "tags":
        return any(v in (o.get("tags") or []) for v in val)
    if op == "any of":
        return _plain(have) in val
    return str(_plain(have)).lower() == str(val).lower()


class FakeMetrics:
    """A tiny Metrics API v2 engine over the fake observations and scores (p95 = max, ms)."""

    def __init__(self, obs, scores):
        self.obs, self.scores = obs, scores
        self.queries = []

    def metrics(self, query):
        q = json.loads(query)
        self.queries.append(q)
        frm, to = (datetime.fromisoformat(q[k]) for k in ("fromTimestamp", "toTimestamp"))
        if q["view"] == "observations":
            rows = [o for o in self.obs if frm <= o["startTime"] <= to]
        else:
            dtype = "BOOLEAN" if q["view"] == "scores-boolean" else "CATEGORICAL"
            rows = []
            by_trace = {o["traceId"]: o for o in self.obs if o.get("isRootObservation")}
            for tid, ss in self.scores.items():
                for s in ss:
                    if s["dataType"] == dtype and frm <= s["timestamp"] <= to:
                        rows.append({**s, "source": _plain(s["source"]), "tags": by_trace[tid]["tags"],
                                     "environment": "dev", "stringValue": s["value"],
                                     "value": float(s["value"]) if dtype == "BOOLEAN" else s["value"]})
        rows = [r for r in rows if all(_passes(r, f) for f in q["filters"])]
        dims = [d["field"] for d in q["dimensions"]]
        groups = {}
        for r in rows:
            groups.setdefault(tuple(_plain(r.get(d)) for d in dims), []).append(r)
        if not dims and not groups:
            groups[()] = []
        out = []
        for key, rs in groups.items():
            row = dict(zip(dims, key))
            for m in q["metrics"]:
                measure, agg = m["measure"], m["aggregation"]
                vals = [r.get(measure) for r in rs]
                if measure == "latency":
                    vals = [v * 1000 for v in vals]
                if agg == "count":
                    row[f"{agg}_{measure}"] = len(rs)
                elif agg == "sum":
                    row[f"{agg}_{measure}"] = sum(v or 0 for v in vals) if rs else None
                elif agg == "avg":
                    row[f"{agg}_{measure}"] = sum(vals) / len(vals) if vals else None
                else:
                    row[f"{agg}_{measure}"] = max(vals) if vals else None
            out.append(row)
        return NS(data=out)


def widget_row(spec, wid, prefix=""):
    return {"id": wid, "name": ds.widget_name(spec, prefix), "view": Enum(spec["view"]),
            "chartType": Enum(spec["chart_type"]), "dimensions": [{"field": d} for d in spec["dimensions"]],
            "metrics": [{"measure": m, "agg": Enum(a)} for m, a in spec["metrics"]],
            "filters": [dict(f) for f in spec["filters"]], "description": "", "chartConfig": {}}


class Fake:
    def __init__(self, obs, scores, *, widgets=None, dashboards=None, blob_status=403):
        self.obs, self.scores = obs, scores
        widgets = widgets if widgets is not None else [widget_row(s, f"w-{s['key']}") for s in ds.WIDGETS]
        if dashboards is None:
            dashboards = [{"id": "d1", "name": ds.DASHBOARD, "description": "", "filters": ds.dashboard_filters("dev"),
                           "definition": {"widgets": [{"type": "widget", "id": f"p{i}", "widgetId": w["id"], "x": 0,
                                                       "y": 6 * i, "width": 6, "height": 6}
                                                      for i, w in enumerate(widgets)]}}]
        self.metrics = FakeMetrics(obs, scores)
        blob_status_ = blob_status

        def blob_list():
            err = Exception("forbidden")
            err.status_code = blob_status_
            raise err

        page = lambda items: (lambda page=1, limit=100: NS(data=[Row(x) for x in items], meta=NS(total_pages=1)))
        self.api = NS(
            observations=NS(get_many=self._obs),
            scores=NS(get_many=lambda trace_id, fields=None, limit=100: NS(
                data=[Row(s) for s in self.scores.get(trace_id, [])], meta=NS(cursor=None))),
            metrics=self.metrics,
            unstable=NS(dashboards=NS(list=page(dashboards), get=lambda i: Row(next(d for d in dashboards
                                                                                   if d["id"] == i))),
                        dashboard_widgets=NS(list=page(widgets))),
            blob_storage_integrations=NS(get_blob_storage_integrations=blob_list),
        )

    def _obs(self, fields=None, filter=None, limit=None, cursor=None):
        half = len(self.obs) // 2   # two pages: the checks must follow the cursor
        if cursor is None:
            return NS(data=[Row(o) for o in self.obs[:half]], meta=NS(cursor="c2"))
        return NS(data=[Row(o) for o in self.obs[half:]], meta=NS(cursor=None))


def good_report(obs, scores, *, anomalies=None, human="human_routing_ok"):
    """What a correct weekly pull returns for the fixture (computed independently of verify)."""
    windows = {}
    for w, win in (("prev", PREV), ("curr", CURR)):
        roots = [o for o in obs if o["isRootObservation"] and win[0] <= o["startTime"] < win[1]]
        tids = {r["traceId"] for r in roots}
        mine = [o for o in obs if o["traceId"] in tids]
        cost = Counter()
        for o in mine:
            cost[o["traceId"]] += o["totalCost"] or 0
        lv = Counter(_plain(r["level"]) for r in roots)
        resolved = {r["traceId"] for r in roots if _plain(r["level"]) == "DEFAULT"
                    and next(s["value"] for s in scores[r["traceId"]] if s["name"] == "handoff_correct")}
        sc = [s for t in tids for s in scores[t]]
        hc = [float(s["value"]) for s in sc if s["name"] == "handoff_correct"]
        hu = [float(s["value"]) for s in sc if s["name"] == human]
        tool95 = {}
        for o in mine:
            if o["type"] == "TOOL":
                tool95[o["name"]] = max(tool95.get(o["name"], 0), o["latency"] * 1000)
        pairs = [(next(s["value"] for s in scores[t] if s["name"] == "routing_judge"),
                  next((s["value"] for s in scores[t] if s["name"] == human), None)) for t in tids]
        pairs = [p for p in pairs if p[1] is not None]
        by = {}
        for key, field in (("by_user", "userId"), ("by_session", "sessionId")):
            by[key] = {}
            for r in roots:
                row = by[key].setdefault(r[field], {"requests": 0, "cost_usd": 0.0, "resolved": 0})
                row["requests"] += 1
                row["cost_usd"] += cost[r["traceId"]]
                row["resolved"] += r["traceId"] in resolved
        user_cost = Counter()
        for r in roots:
            user_cost[r["userId"]] += cost[r["traceId"]]
        total = sum(cost.values())
        metrics = {"requests": len(roots), "error_rate": 1 - lv["DEFAULT"] / len(roots),
                   "level_share.WARNING": lv["WARNING"] / len(roots), "level_share.DEFAULT": lv["DEFAULT"] / len(roots),
                   "cost_usd.total": total, "score.handoff_correct.API": sum(hc) / len(hc),
                   f"score.{human}.ANNOTATION": sum(hu) / len(hu),
                   **{f"latency_ms.tool.{k}.p95": v for k, v in tool95.items()},
                   "resolved_rate": len(resolved) / len(roots), "cost_usd.per_request": total / len(roots),
                   "cost_usd.per_resolved": total / len(resolved),
                   "judge_human.pairs": len(pairs),
                   "judge_human.agreement": sum(bool(a) == bool(b) for a, b in pairs) / len(pairs)}
        windows[w] = {"from": win[0].isoformat(), "to": win[1].isoformat(), "metrics": metrics,
                      "top_users": [{"user_id": u, "cost_usd": c} for u, c in user_cost.most_common(5)],
                      "by_version": {}, **by}
    if anomalies is None:
        anomalies = [{"metric": "latency_ms.tool.lookup_order.p95", "direction": "up", "prev": 2.0, "curr": 3001.0,
                      "delta": 2999.0, "pct": 1499.5, "rule": "latency_ms.*.p95"}]
    return {"windows": windows, "comparison": {}, "anomalies": anomalies}


def export(tmp_path, obs, scores, *, drop_obs=0):
    (tmp_path / "observations.jsonl").write_text(
        "\n".join(json.dumps({"id": o["id"], "traceId": o["traceId"]}) for o in obs[drop_obs:]), encoding="utf-8")
    (tmp_path / "scores.jsonl").write_text(
        "\n".join(json.dumps({"id": s["id"]}) for rows in scores.values() for s in rows), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def no_minio(monkeypatch):
    monkeypatch.setattr(s3, "list_objects", lambda prefix, settings=None, **kw: [])
    for k in ("LANGFUSE_ORG_PUBLIC_KEY", "LANGFUSE_ORG_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


def run(fake, report, export_dir=None, **kw):
    checks = module_11_checks(TAG, fake, report=report, export_dir=export_dir, **kw)
    out = {}
    for c in checks:
        try:
            out[c.name] = c.fn()
        except FeatureUnavailable as exc:
            out[c.name] = ("unavailable", str(exc))
    return out


def by_prefix(results, prefix):
    return next(v for k, v in results.items() if k.startswith(prefix))


def test_all_pass(tmp_path):
    obs, scores = build()
    res = run(Fake(obs, scores), good_report(obs, scores), export(tmp_path, obs, scores))
    for name, (ok, detail) in res.items():
        if name.startswith(("blob storage", "UI batch")):
            assert ok == "unavailable", (name, detail)
        else:
            assert ok is True, (name, detail)
    assert "lookup_order p95 v1 2 ms -> v2 3001 ms" in by_prefix(res, "planted: the dashboard")[1]


def test_not_yet_until_all_roots(tmp_path):
    obs, scores = build()
    obs = [o for o in obs if o["traceId"] != "curr-injection"]
    checks = module_11_checks(TAG, Fake(obs, scores), report=good_report(*build()), export_dir=tmp_path)
    with pytest.raises(NotYet):
        checks[0].fn()


def test_dry_run_traffic_is_refused_unless_allowed(tmp_path):
    obs, scores = build(dry=True)
    rep = good_report(obs, scores)
    assert by_prefix(run(Fake(obs, scores), rep), "analytics traffic")[0] is False
    assert by_prefix(run(Fake(obs, scores), rep, allow_dry=True), "analytics traffic")[0] is True


def test_windows_must_contain_their_traffic():
    obs, scores = build()
    rep = good_report(obs, scores)
    rep["windows"]["prev"]["to"] = (T0 + timedelta(seconds=30)).isoformat()
    ok, detail = by_prefix(run(Fake(obs, scores), rep), "analytics traffic")
    assert ok is False and "in report window None" in detail


def test_wrong_metric_values_fail():
    obs, scores = build()
    rep = good_report(obs, scores)
    rep["windows"]["curr"]["metrics"]["requests"] = 6
    rep["windows"]["prev"]["metrics"]["latency_ms.tool.lookup_order.p95"] = 3001.0
    ok, detail = by_prefix(run(Fake(obs, scores), rep), "Metrics API numbers")
    assert ok is False and "curr: requests = 6" in detail and "prev: latency_ms.tool.lookup_order.p95" in detail


def test_missing_metric_key_and_wrong_top_user():
    obs, scores = build()
    rep = good_report(obs, scores)
    del rep["windows"]["prev"]["metrics"]["error_rate"]
    rep["windows"]["curr"]["top_users"] = [{"user_id": "cust_00", "cost_usd": 0.001}]
    ok, detail = by_prefix(run(Fake(obs, scores), rep), "Metrics API numbers")
    assert ok is False and "metrics['error_rate'] missing" in detail and "top user by cost" in detail


def test_rollups_wrong_cost_per_resolved_and_user():
    obs, scores = build()
    rep = good_report(obs, scores)
    rep["windows"]["prev"]["metrics"]["cost_usd.per_resolved"] = rep["windows"]["prev"]["metrics"][
        "cost_usd.per_request"]
    rep["windows"]["curr"]["by_user"]["cust_00"]["resolved"] = 0
    ok, detail = by_prefix(run(Fake(obs, scores), rep), "client-side rollups")
    assert ok is False and "prev: cost_usd.per_resolved" in detail and "by_user[cust_00]" in detail


def test_rollups_agreement_counts_only_pairs():
    obs, scores = build()
    rep = good_report(obs, scores)
    assert rep["windows"]["prev"]["metrics"]["judge_human.pairs"] == 2   # double_charge control + refund false alarm
    assert rep["windows"]["prev"]["metrics"]["judge_human.agreement"] == 0.5
    assert by_prefix(run(Fake(obs, scores), rep), "client-side rollups")[0] is True


def test_dashboard_duplicates_and_spec_mismatch():
    obs, scores = build()
    widgets = [widget_row(s, f"w-{s['key']}") for s in ds.WIDGETS]
    widgets.append(widget_row(ds.BY_KEY["levels"], "w-levels-2"))
    widgets[2]["dimensions"] = [{"field": "name"}]
    ok, detail = by_prefix(run(Fake(obs, scores, widgets=widgets), good_report(obs, scores)), "agent-health dashboard")
    assert ok is False and "duplicates" in detail and "tool_latency: dimensions" in detail


def test_dashboard_missing_environment_filter_and_placement():
    obs, scores = build()
    widgets = [widget_row(s, f"w-{s['key']}") for s in ds.WIDGETS]
    dash = [{"id": "d1", "name": ds.DASHBOARD, "filters": [], "definition": {"widgets": [
        {"type": "widget", "id": "p0", "widgetId": widgets[0]["id"], "x": 0, "y": 0, "width": 6, "height": 6}]}}]
    ok, detail = by_prefix(run(Fake(obs, scores, widgets=widgets, dashboards=dash), good_report(obs, scores)),
                           "agent-health dashboard")
    assert ok is False and "environment" in detail and "placed 0 time(s)" in detail


def test_dashboard_with_prefix():
    obs, scores = build()
    widgets = [widget_row(s, f"w-{s['key']}", prefix="zz_") for s in ds.WIDGETS]
    dash = [{"id": "d1", "name": "zz_" + ds.DASHBOARD, "filters": ds.dashboard_filters("dev"), "definition": {
        "widgets": [{"type": "widget", "id": f"p{i}", "widgetId": w["id"], "x": 0, "y": 0, "width": 6, "height": 6}
                    for i, w in enumerate(widgets)]}}]
    for rows in scores.values():
        for s in rows:
            if s["name"] == "human_routing_ok":
                s["name"] = "zz_human_routing_ok"
    rep = good_report(obs, scores, human="zz_human_routing_ok")
    res = run(Fake(obs, scores, widgets=widgets, dashboards=dash), rep, prefix="zz_")
    assert by_prefix(res, "agent-health dashboard")[0] is True
    assert by_prefix(res, "Metrics API numbers")[0] is True
    assert by_prefix(res, "client-side rollups")[0] is True


def test_planted_dashboard_needs_the_regression():
    obs, scores = build(slow_curr=False)
    ok, detail = by_prefix(run(Fake(obs, scores), good_report(obs, scores)), "planted: the dashboard")
    assert ok is False and "does not show the slow_tool regression" in detail


def test_planted_dashboard_widget_without_data():
    obs, scores = build()
    widgets = [widget_row(s, f"w-{s['key']}") for s in ds.WIDGETS]
    widgets[6]["filters"] = [{"column": "name", "operator": "=", "type": "string", "value": "nope"}]
    ok, detail = by_prefix(run(Fake(obs, scores, widgets=widgets), good_report(obs, scores)),
                           "planted: the dashboard")
    assert ok is False and "feedback" in detail


def test_export_incomplete_or_missing(tmp_path):
    obs, scores = build()
    rep = good_report(obs, scores)
    ok, detail = by_prefix(run(Fake(obs, scores), rep, export(tmp_path, obs, scores, drop_obs=3)), "portable export")
    assert ok is False and "3/" in detail
    ok, detail = by_prefix(run(Fake(obs, scores), rep, tmp_path / "nowhere"), "portable export")
    assert ok is False and "does not exist" in detail


def test_blob_unavailable_with_project_key():
    obs, scores = build()
    status, detail = by_prefix(run(Fake(obs, scores), good_report(obs, scores)), "blob storage")
    assert status == "unavailable" and "ORGANIZATION API key" in detail


def test_batch_export_found(monkeypatch):
    obs, scores = build()
    monkeypatch.setattr(s3, "list_objects", lambda prefix, settings=None, **kw: [
        {"key": "exports/old.json", "size": 1, "last_modified": T0 - timedelta(days=1)},
        {"key": "exports/new.json", "size": 1, "last_modified": T0 + timedelta(minutes=5)}])
    ok, detail = by_prefix(run(Fake(obs, scores), good_report(obs, scores)), "UI batch export")
    assert ok is True and "exports/new.json" in detail and "1 UI batch export" in detail


def test_planted_report_must_flag_latency():
    obs, scores = build()
    rep = good_report(obs, scores, anomalies=[{"metric": "error_rate", "direction": "up"}])
    ok, detail = by_prefix(run(Fake(obs, scores), rep), "planted: the weekly report")
    assert ok is False and "did not flag" in detail
    rep = good_report(obs, scores)
    rep["anomalies"].append({"metric": "feedback_share.thumbs_down", "direction": "up"})
    ok, detail = by_prefix(run(Fake(obs, scores), rep), "planted: the weekly report")
    assert ok is True and "also flagged: ['feedback_share.thumbs_down']" in detail


def test_planted_report_needs_slow_spans_in_curr_only():
    obs, scores = build(slow_curr=False)
    ok, detail = by_prefix(run(Fake(obs, scores), good_report(obs, scores)), "planted: the weekly report")
    assert ok is False and "does not show the planted slow_tool" in detail


def test_no_report():
    obs, scores = build()
    with pytest.raises(AssertionError, match="no weekly report"):
        module_11_checks(TAG, Fake(obs, scores), report=None)[0].fn()
