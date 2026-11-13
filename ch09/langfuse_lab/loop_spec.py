"""Module 14 (the capstone): the production loop's names, stages, audit format and the incident report. No Langfuse code.

The loop: traffic -> judge -> alert -> review queue -> human labels -> dataset items -> prompt fix -> experiment vs the
baseline -> CI gate -> promote -> recovered traffic -> the alert resolves. The notebook records one EVENT per stage
(from what Langfuse stored), `langfuse_mastery.loop.timeline` orders them and computes the durations, and
`write_report` turns the result into `evidence.json`, `incident-report.md` and `incident-report.html`.

THE EVIDENCE (evidence.json):

    {"incident": "INC-m14-abc123", "run_tag", "prompt", "environment", "judge_mode": "live" | "stand-in",
     "events": [{"stage", "at": ISO, "title", "numbers": {...}, "refs": {...}, "links": {name: url}}, ...],
     "durations": {"time_to_detect_s", "time_to_review_s", "time_to_fix_s", "time_to_recover_s"},
     "problems": [str, ...]}

EVENT_SCHEMA lists, per stage, the numbers / refs / links an event must carry (verify compares them with the stored data).
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from . import prompts

ROOT_NAME = "support-request"
ENVIRONMENT = "production"
ALERT_PATH = "/m14/alerts"            # the receiver path the DIY alert job posts to
SECRET_ENV = "M14_ALERT_SECRET"       # the alert job's signing secret (the notebook sets it)
JUDGE_RULE = "judge-routing-drop"     # loop.LOOP_RULES' judge rule (what pages on live traffic)
LIVE_LABEL, PRODUCTION_LABEL, HOTFIX_LABEL = "live", "production", prompts.HOTFIX_LABEL
EXPECTED_TEAMS = ("account_security", "billing_specialist", "shipping_specialist", "none")
ERROR_TYPES = ("wrong_team", "missed_handoff", "unnecessary_handoff", "other")  # module 08's taxonomy
SKEW_S = 10.0  # tolerance for orderings across clocks (traces/deliveries: this host; comments/items: the server)

BASE_NAMES = {  # role -> base name; every project-global name is PREFIX + base
    "prompt": prompts.PROMPT_NAME,    # module 04's managed prompt
    "dataset": "support-regression",  # module 09/10's regression dataset (the incident adds items)
    "config": "handoff_correct",      # module 06's code-score config (the gate's item evaluator)
    "queue": "incident-review",       # new annotation queue
    "human": "human_routing_ok",      # module 08's BOOLEAN review config
    "error_type": "review_error_type",  # module 08's CATEGORICAL taxonomy config
    "expected": "expected_handoff",   # new CATEGORICAL config: the team the request should have gone to
    "judge": "routing_judge",         # module 07's evaluator (EVAL) or module 08's stand-in (API)
    "rule": "support-live-eval",      # module 07's evaluation rule (live judge mode when it is enabled)
    "live_tag": "live-eval",          # module 07's opt-in tag
    "tag": "prod-loop",               # stable tag on every capstone trace
}

AUDIT_ACTIONS = ("start", "deploy", "candidate", "promote")

# (stage, title, what it proves). The order is the loop's causal order.
STAGES = (
    ("baseline", "Healthy traffic", "the approved version serves live traffic and the judge passes it"),
    ("deploy", "Planted deploy", "the staging candidate is moved to `live` without the gate"),
    ("judge", "The judge sees it", "live traffic has no ground truth: the LLM judge is the quality signal"),
    ("alert", "Alert fires", "the alert job compares the judge pass rate with the baseline and pages once"),
    ("review", "Human review", "people confirm what the judge flagged (and check a control the judge passed)"),
    ("dataset", "Failures become tests", "each confirmed failure is a regression-dataset item, labelled by a human"),
    ("fix", "Prompt fix", "a fix is written as a new prompt version and put up for review (label `hotfix`)"),
    ("experiment", "Experiment vs baseline", "the fix runs on the pinned dataset next to the approved version"),
    ("gate", "CI gate", "the gate decides ship / block from the item-level comparison"),
    ("promote", "Promotion", "only a shipped fix that passes the incident's tests is moved to production + live"),
    ("recovered", "Healthy again", "traffic after the promotion is served by the fix and the judge passes it"),
    ("resolved", "Alert resolved", "the alert job sends one resolved message"),
)
STAGE_NAMES = tuple(s for s, _, _ in STAGES)
STAGE_TITLES = {s: t for s, t, _ in STAGES}
# Causal pairs (a, b): the first event of b cannot come before the first event of a (within SKEW_S).
CAUSAL = (("baseline", "deploy"), ("deploy", "judge"), ("judge", "alert"), ("alert", "review"), ("review", "dataset"),
          ("dataset", "experiment"), ("fix", "experiment"), ("experiment", "gate"), ("gate", "promote"),
          ("promote", "recovered"), ("recovered", "resolved"))
DURATIONS = (  # key, from stage (first event), to stage (first or last event), what it measures
    ("time_to_detect_s", "deploy", "first", "alert", "first", "deploy -> the alert fires"),
    ("time_to_review_s", "alert", "first", "review", "last", "alert -> the human review is complete"),
    ("time_to_fix_s", "alert", "first", "promote", "first", "alert -> a fix is promoted"),
    ("time_to_recover_s", "deploy", "first", "resolved", "first", "deploy -> the alert resolves (MTTR)"),
)
EVENT_SCHEMA = {  # stage -> (numbers, refs, links) every event of that stage must carry
    "baseline": (("requests", "judge_pass_rate", "version"), (), ("prompt",)),
    "deploy": (("from_version", "to_version"), (), ("prompt",)),
    "judge": (("requests", "judge_pass_rate", "flagged"), ("trace_ids",), ()),
    "alert": (("rule", "metric", "baseline", "current", "version"), ("id",), ()),
    "review": (("queued", "flagged", "controls", "confirmed_failures", "false_alarms"), ("queue_id", "trace_ids"),
               ("queue",)),
    "dataset": (("items", "pinned_version"), ("dataset_id", "item_ids"), ("dataset",)),
    "fix": (("kind", "version"), (), ("prompt",)),
    "experiment": (("kind", "candidate_version", "baseline_version", "items"),
                   ("candidate_experiment_id", "baseline_experiment_id"), ("candidate", "baseline")),
    "gate": (("kind", "version", "decision", "exit_code", "regressed"), ("experiment_id",), ()),
    "promote": (("version", "labels"), (), ("prompt",)),
    "recovered": (("requests", "judge_pass_rate", "version"), (), ("prompt",)),
    "resolved": (("rule", "current"), ("id",), ()),
}
TOP_KEYS = ("incident", "run_tag", "prompt", "environment", "judge_mode", "events", "durations", "problems")
FILES = {"evidence": "evidence.json", "markdown": "incident-report.md", "html": "incident-report.html"}


def names(prefix: str = "") -> dict:
    """Project-global names for one PREFIX (the learner's is "")."""
    return {role: prefix + base for role, base in BASE_NAMES.items()}


def incident_id(run_id: str) -> str:
    return f"INC-{run_id}"


def audit_text(run_tag: str, action: str, text: str) -> str:
    """A label move's audit line, written as a comment on the prompt version (OSS has no audit log)."""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action {action!r} (one of {AUDIT_ACTIONS})")
    return f"[{run_tag}] {action}: {text}"


def parse_audit(content: str | None) -> tuple[str, str, str] | None:
    """audit_text's inverse: (run_tag, action, text), or None for any other comment."""
    text = (content or "").strip()
    if not text.startswith("[") or "] " not in text:
        return None
    tag, rest = text[1:].split("] ", 1)
    action, sep, body = rest.partition(": ")
    return (tag, action, body) if sep and action in AUDIT_ACTIONS else None


def _as_dt(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v if v is None or v.tzinfo else v.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def evidence_problems(evidence) -> list[str]:
    """What is missing or malformed in an evidence dict (the schema above); [] when it is complete."""
    if not isinstance(evidence, dict):
        return ["evidence is not a dict"]
    out = [f"missing key: {k}" for k in TOP_KEYS if k not in evidence]
    events = evidence.get("events") or []
    if not isinstance(events, list):
        return out + ["events is not a list"]
    seen = set()
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            out.append(f"event {i} is not a dict")
            continue
        stage = e.get("stage")
        if stage not in EVENT_SCHEMA:
            out.append(f"event {i}: unknown stage {stage!r}")
            continue
        seen.add(stage)
        try:
            if _as_dt(e.get("at")) is None:
                out.append(f"{stage}: no 'at'")
        except (TypeError, ValueError):
            out.append(f"{stage}: 'at' is not an ISO timestamp: {e.get('at')!r}")
        numbers, refs, links = EVENT_SCHEMA[stage]
        for kind, keys in (("numbers", numbers), ("refs", refs), ("links", links)):
            have = e.get(kind) or {}
            if not isinstance(have, dict):
                out.append(f"{stage}: {kind} is not a dict")
                continue
            out += [f"{stage}: {kind} lacks {k!r}" for k in keys if k not in have]
    out += [f"no event for stage {s!r}" for s in STAGE_NAMES if s not in seen]
    return out


# ---------------------------------------------------------------- rendering
def _dur(seconds) -> str:
    if seconds is None:
        return "–"
    s = int(round(float(seconds)))
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def _val(v) -> str:
    if isinstance(v, float):
        return f"{v:.3g}"
    if isinstance(v, (list, tuple)):
        return ", ".join(_val(x) for x in v) or "–"
    return "–" if v is None else str(v)


def _hhmmss(at) -> str:
    d = _as_dt(at)
    return d.strftime("%H:%M:%S") if d else "?"


def headline(evidence: dict) -> list[tuple[str, str]]:
    """(label, value) pairs for the report's top: the numbers a leader asks for first."""
    ev = evidence.get("events") or []
    d = evidence.get("durations") or {}
    judge = next((e for e in ev if e.get("stage") == "judge"), {})
    blocked = [e for e in ev if e.get("stage") == "gate" and (e.get("numbers") or {}).get("decision") == "block"]
    promoted = next((e for e in ev if e.get("stage") == "promote"), {})
    jn = judge.get("numbers") or {}
    return [
        ("Detected", f"{_dur(d.get('time_to_detect_s'))} after the deploy"),
        ("Customers affected", f"{_val(jn.get('flagged'))} of {_val(jn.get('requests'))} requests misrouted while the "
                               "bad version was live"),
        ("Bad fix stopped", ", ".join(f"v{(e.get('numbers') or {}).get('version') or '?'}" for e in blocked)
         + f" blocked by the gate ({_val([x for e in blocked for x in (e.get('numbers') or {}).get('regressed') or []])})"
         if blocked else "none"),
        ("Fixed", f"v{(promoted.get('numbers') or {}).get('version', '?')} promoted {_dur(d.get('time_to_fix_s'))} "
                  "after the alert"),
        ("Recovered (MTTR)", f"{_dur(d.get('time_to_recover_s'))} from the deploy to the resolved alert"),
    ]


def render_markdown(evidence: dict) -> str:
    """The incident report as Markdown (a PR comment, a wiki page, a Slack post)."""
    lines = [f"# Incident {evidence.get('incident')}: a prompt deploy misrouted login requests", "",
             f"Prompt `{evidence.get('prompt')}` · environment `{evidence.get('environment')}` · judge "
             f"`{evidence.get('judge_mode')}` · run `{evidence.get('run_tag')}`", "", "## Headline", ""]
    lines += [f"- **{k}:** {v}" for k, v in headline(evidence)]
    lines += ["", "## Timeline (UTC)", "", "| time | stage | what happened | numbers | links |", "|---|---|---|---|---|"]
    for e in evidence.get("events") or []:
        nums = "; ".join(f"{k} {_val(v)}" for k, v in (e.get("numbers") or {}).items())
        links = " ".join(f"[{k}]({u})" for k, u in (e.get("links") or {}).items())
        title = str(e.get("title") or STAGE_TITLES.get(e.get("stage"), "")).replace("|", "/")
        lines.append(f"| {_hhmmss(e.get('at'))} | {STAGE_TITLES.get(e.get('stage'), e.get('stage'))} | {title} | "
                     f"{nums.replace('|', '/')} | {links} |")
    d = evidence.get("durations") or {}
    lines += ["", "## Durations", ""] + [f"- {why}: **{_dur(d.get(key))}**" for key, *_, why in DURATIONS]
    if evidence.get("problems"):
        lines += ["", "## Problems in this trail", ""] + [f"- {p}" for p in evidence["problems"]]
    lines += ["", "_Every number above was read back from Langfuse (traces, scores, the annotation queue, the dataset, "
                  "experiments, prompt versions and their audit comments) or from the alert receiver's log._", ""]
    return "\n".join(lines)


_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--accent:#2f6fde;--ok:#1f8a4c;--bad:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#a3a29b;--line:#34332f;
--accent:#7aa7ff;--ok:#4cc38a;--bad:#ff8a5b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:1080px;margin:0 auto;padding:32px 16px}h1{font-size:26px;margin:0 0 4px}
.sub{color:var(--muted);margin-bottom:24px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:12px;margin-bottom:28px}.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpi b{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:4px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;
overflow:hidden}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;
font-size:14px}th{font-size:12px;text-transform:uppercase;color:var(--muted)}td.t{white-space:nowrap;
font-variant-numeric:tabular-nums}a{color:var(--accent)}.stage{font-weight:600}.block{color:var(--bad);font-weight:600}
.ship{color:var(--ok);font-weight:600}.wrap{overflow-x:auto}footer{color:var(--muted);font-size:13px;margin-top:20px}
"""


def render_html(evidence: dict) -> str:
    """The incident report as one self-contained HTML page (no external assets)."""
    esc = html.escape
    kpis = "".join(f'<div class="kpi"><b>{esc(k)}</b>{esc(v)}</div>' for k, v in headline(evidence))
    rows = []
    for e in evidence.get("events") or []:
        nums = e.get("numbers") or {}
        decision = nums.get("decision")
        cls = f' class="{decision}"' if decision in ("ship", "block") else ""
        num_html = "<br>".join(f"{esc(str(k))}: <span{cls if k == 'decision' else ''}>{esc(_val(v))}</span>"
                               for k, v in nums.items())
        links = "<br>".join(f'<a href="{esc(str(u))}" target="_blank" rel="noopener">{esc(str(k))}</a>'
                            for k, u in (e.get("links") or {}).items())
        rows.append(f'<tr><td class="t">{esc(_hhmmss(e.get("at")))}</td><td class="stage">'
                    f'{esc(STAGE_TITLES.get(e.get("stage"), str(e.get("stage"))))}</td><td>{esc(str(e.get("title") or ""))}'
                    f"</td><td>{num_html}</td><td>{links}</td></tr>")
    d = evidence.get("durations") or {}
    durs = "".join(f"<li>{esc(why)}: <b>{esc(_dur(d.get(key)))}</b></li>" for key, *_, why in DURATIONS)
    probs = "".join(f"<li>{esc(p)}</li>" for p in evidence.get("problems") or [])
    title = f"Incident {evidence.get('incident')}"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>{_CSS}</style></head><body>'
            f"<main><h1>{esc(title)}: a prompt deploy misrouted login requests</h1>"
            f'<div class="sub">Prompt <code>{esc(str(evidence.get("prompt")))}</code> · environment '
            f'<code>{esc(str(evidence.get("environment")))}</code> · judge {esc(str(evidence.get("judge_mode")))} · '
            f'run <code>{esc(str(evidence.get("run_tag")))}</code></div><div class="kpis">{kpis}</div>'
            f'<div class="wrap"><table><thead><tr><th>Time (UTC)</th><th>Stage</th><th>What happened</th><th>Numbers</th>'
            f'<th>Evidence</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f"<h2>Durations</h2><ul>{durs}</ul>" + (f"<h2>Problems in this trail</h2><ul>{probs}</ul>" if probs else "")
            + "<footer>Every number was read back from Langfuse (traces, scores, the annotation queue, the dataset, "
              "experiments, prompt versions and their audit comments) or from the alert receiver's log.</footer>"
              "</main></body></html>")


def write_report(evidence: dict, out_dir) -> dict:
    """Write evidence.json, incident-report.md and incident-report.html into `out_dir`; returns {kind: path}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {k: out / name for k, name in FILES.items()}
    paths["evidence"].write_text(json.dumps(evidence, indent=1, default=str), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(evidence), encoding="utf-8")
    paths["html"].write_text(render_html(evidence), encoding="utf-8")
    return paths
