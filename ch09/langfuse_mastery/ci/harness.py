"""Module 10: the GIVEN part of the release gate (a module 09 recap plus CI plumbing). Not a TODO.

- `Harness.from_env(env)`: what the gate runs against, READ-ONLY: the Langfuse client, the project-global names
  (`GATE_PREFIX` + base), the managed prompt's labels, module 06's score config, the regression dataset pinned to
  one version (`GATE_DATASET_VERSION`, else the newest item version on the server), the gate policy (`GATE_*`
  env vars), the task per prompt label and the evaluators. It raises a clear error when something is missing:
  CI must never create prompts, labels or datasets.
- `bootstrap(langfuse, prefix=...)`: the notebook's get-or-create (prompt versions + labels, config, a seeded
  dataset when none exists). It WRITES, so only the notebook calls it, never CI.
- Dry run (`GATE_DRY_RUN=1`): an oracle task instead of the agent (no Azure calls): it answers the expected team,
  except where the served prompt's routing rules say otherwise (`staging` sends login cases to billing, like the
  planted bad prompt; module 14's quick fix sends double charges to account security). Its experiments carry
  `dry_run=true` and a different baseline key, so they never stand in for a real run.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from langfuse import Evaluation, propagate_attributes
from langfuse.api import NotFoundError

from langfuse_lab import oracle_llm, regression, verify
from langfuse_lab.prompts import (CANDIDATE_LABEL, CLEAN_CANDIDATE_TEMPLATE, PROMPT_CONFIG, PROMPT_VARIABLES,
                                  SYSTEM_PROMPT_TEMPLATE, WRONG_HANDOFF_TEMPLATE)

EXPERIMENT_NAME = verify.GATE_EXPERIMENT_NAME  # "release-gate"
GATE_SCORE = verify.GATE_SCORE                 # "ci_gate": the decision, a run-level score on the candidate run
BASELINE_LABEL = verify.BASELINE_LABEL         # "production"
LABEL_TEMPLATES = {"production": SYSTEM_PROMPT_TEMPLATE, "staging": WRONG_HANDOFF_TEMPLATE,
                   CANDIDATE_LABEL: CLEAN_CANDIDATE_TEMPLATE}
BASELINE_MAX_AGE = timedelta(hours=24)  # an older baseline is not reused (models and data drift)
LOOKBACK = BASELINE_MAX_AGE + timedelta(hours=1)
DEFAULT_POLICY = {
    "reruns": 1,            # extra attempts per side for each suspect (0 = trust the first run)
    "max_rerun_items": 3,   # more suspects than this: don't spend on re-runs, the candidate is plainly worse
    "max_regressions": 0,   # confirmed regressions tolerated
    "max_drop": 0.2,        # tolerated drop of the mean pass rate
    "max_items": 20,        # budget: refuse to gate a dataset bigger than this (cost/time per PR)
    "max_concurrency": 1,   # a sync task runs items one by one anyway (module 09)
}
POLICY_ENV = {"reruns": ("GATE_RERUNS", int), "max_rerun_items": ("GATE_MAX_RERUN_ITEMS", int),
              "max_regressions": ("GATE_MAX_REGRESSIONS", int), "max_drop": ("GATE_MAX_DROP", float),
              "max_items": ("GATE_MAX_ITEMS", int)}


def policy_from_env(env) -> dict:
    policy = dict(DEFAULT_POLICY)
    for key, (var, cast) in POLICY_ENV.items():
        if env.get(var):
            policy[key] = cast(env[var])
    return policy


def names_for(prefix: str) -> dict:
    return {role: prefix + base for role, base in verify.REGRESSION_NAMES.items()}


def _active_items(langfuse, dataset_name: str) -> list:
    out, page = [], 1
    while True:
        resp = langfuse.api.dataset_items.list(dataset_name=dataset_name, page=page, limit=100)
        out += [i for i in resp.data if verify._plain(i.status) == "ACTIVE"]
        if page >= (resp.meta.total_pages or 1):
            return out
        page += 1


def _oracle(label: str, expected: str, text: str | None = None) -> str:
    """The dry run's answer: the expected team, unless the served prompt's rules route that intent elsewhere."""
    if text is None:
        return "billing_specialist" if label == "staging" and expected == verify.LOGIN_TEAM else expected
    if expected == verify.LOGIN_TEAM:
        return oracle_llm.login_team(text)
    if expected == oracle_llm.BILLING_TEAM:
        return oracle_llm.billing_team(text)
    return expected


@dataclass
class Harness:
    langfuse: object
    prefix: str
    names: dict
    dataset_id: str
    dataset_version: datetime
    items: list
    config_id: str
    labels: dict
    policy: dict
    dry_run: bool = False
    agent_model: str = "oracle"
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc) - LOOKBACK)
    _agent: object = None

    @classmethod
    def from_env(cls, env=None, *, langfuse=None) -> "Harness":
        env = os.environ if env is None else env
        langfuse = langfuse or verify.get_client()  # loads the repo-root .env when present (CI: env vars/secrets)
        prefix = env.get("GATE_PREFIX", "")
        names = names_for(prefix)
        dry = env.get("GATE_DRY_RUN", "") in ("1", "true", "yes")
        labels = {}
        for label in (BASELINE_LABEL, env.get("CANDIDATE_LABEL") or "staging"):
            try:
                labels[label] = langfuse.api.prompts.get(names["prompt"], label=label).version
            except NotFoundError:
                raise RuntimeError(f"prompt {names['prompt']!r} has no label {label!r}: promote a version to it "
                                   "(module 04), or run module 10's bootstrap cell") from None
        try:
            ds = langfuse.api.datasets.get(names["dataset"])
        except NotFoundError:
            raise RuntimeError(f"no dataset {names['dataset']!r}: build it in module 09 or run module 10's "
                               "bootstrap cell") from None
        configs = [c for c in langfuse.api.score_configs.get(page=1, limit=100).data
                   if c.name == names["config"] and not c.is_archived]
        if not configs:
            raise RuntimeError(f"no active score config {names['config']!r} (module 06 / the bootstrap cell)")
        if env.get("GATE_DATASET_VERSION"):
            version = datetime.fromisoformat(env["GATE_DATASET_VERSION"].replace("Z", "+00:00"))
        else:
            active = _active_items(langfuse, names["dataset"])
            if not active:
                raise RuntimeError(f"dataset {names['dataset']!r} has no active items")
            version = max(i.version for i in active)  # a SERVER timestamp (module 09: never pin with local time)
        items = langfuse.get_dataset(names["dataset"], version=version).items
        policy = policy_from_env(env)
        if not items:
            raise RuntimeError(f"dataset {names['dataset']!r} has no items at version {version.isoformat()}")
        if len(items) > policy["max_items"]:
            raise RuntimeError(f"budget: {len(items)} items > max_items={policy['max_items']} per gate run")
        model = "oracle" if dry else (env.get("AZURE_OPENAI_DEPLOYMENT") or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
                                      or "gpt-4.1-mini")
        return cls(langfuse=langfuse, prefix=prefix, names=names, dataset_id=ds.id, dataset_version=version,
                   items=items, config_id=configs[0].id, labels=labels, policy=policy, dry_run=dry, agent_model=model)

    # ------------------------------------------------------------ what the experiments run on
    @property
    def experiment_name(self) -> str:
        return EXPERIMENT_NAME

    def pinned(self):
        """A fresh DatasetClient with the pinned items (slice `.items` to run a subset)."""
        return self.langfuse.get_dataset(self.names["dataset"], version=self.dataset_version)

    @property
    def info(self) -> dict:
        """item id -> {"case_id", "message", "expected"} (for ci_gate.report_rows)."""
        return {i.id: {"case_id": (i.metadata or {}).get("case_id", i.id), "message": (i.input or {}).get("message", ""),
                       "expected": (i.expected_output or {}).get("handoff")} for i in self.items}

    @property
    def baseline_key(self) -> str:
        """Everything that changes a baseline's result. Reuse a stored baseline only when this matches exactly."""
        return (f"{self.dataset_id}@{self.dataset_version.isoformat()}|{self.names['prompt']}@v"
                f"{self.labels[BASELINE_LABEL]}|{self.agent_model}|{self.names['config']}")

    def metadata(self) -> dict:
        """Harness metadata every gate experiment carries (merge it into ci_gate.experiment_metadata)."""
        return {"dry_run": self.dry_run, "agent_model": self.agent_model, "baseline_key": self.baseline_key}

    def task(self, label: str):
        """(prompt, task): the prompt is served ONCE per run (every item sees the same version); the task links each
        generation to it (module 04/09) and returns {"handoff", "answer"}."""
        prompt = self.langfuse.get_prompt(self.names["prompt"], label=label, cache_ttl_seconds=0)
        text = prompt.compile(**PROMPT_VARIABLES)
        params = {k: prompt.config[k] for k in ("temperature", "max_tokens") if k in (prompt.config or {})}
        if self.dry_run:
            def task(*, item, **kwargs):
                return {"handoff": _oracle(label, item.expected_output["handoff"], text), "answer": "Done (dry run)."}
            return prompt, task

        from langfuse.langchain import CallbackHandler

        from langfuse_lab.agent import SupportAgent

        if self._agent is None:
            self._agent = SupportAgent()

        def task(*, item, **kwargs):
            with propagate_attributes(prompt=prompt):
                run = self._agent.invoke(item.input["message"], config={"callbacks": [CallbackHandler()]},
                                         system_prompt=text, llm_params=params)
            return {"handoff": run.summary["handoff_target"], "answer": run.summary["final_answer"]}

        return prompt, task

    @property
    def evaluators(self) -> list:
        config_name, config_id = self.names["config"], self.config_id

        def handoff_correct(*, output, expected_output, **kwargs):
            ok = output["handoff"] == expected_output["handoff"]
            return Evaluation(name=config_name, value=ok, data_type="BOOLEAN", config_id=config_id,
                              comment=None if ok else f"expected {expected_output['handoff']}, got {output['handoff']}")

        return [handoff_correct]

    @property
    def run_evaluators(self) -> list:
        config_name = self.names["config"]

        def handoff_accuracy(*, item_results, **kwargs):
            vals = [bool(e.value) for r in item_results for e in r.evaluations if e.name == config_name]
            return Evaluation(name="handoff_accuracy", value=sum(vals) / len(vals) if vals else 0.0,
                              data_type="NUMERIC", comment=f"{sum(vals)}/{len(vals)} items routed correctly")

        return [handoff_accuracy]

    # ------------------------------------------------------------ reading results back
    def wait_for_items(self, experiment_id: str, n: int, timeout: float = 180) -> None:
        """Poll until `n` items of the experiment are ingested, each with its handoff score."""
        deadline = time.time() + timeout
        while True:
            rows = self.langfuse.api.experiments.list_items(from_start_time=self.since, experiment_id=experiment_id,
                                                            fields="core,scores", limit=100).data
            if len(rows) >= n and all(any(s.name == self.names["config"] for s in (r.scores or [])) for r in rows):
                return
            if time.time() > deadline:
                raise TimeoutError(f"experiment {experiment_id}: {len(rows)}/{n} items ingested+scored in {timeout}s")
            time.sleep(3)

    def experiment_url(self, experiment_id: str) -> str:
        base = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")
        project = self.langfuse.api.projects.get().data[0].id
        return f"{base}/project/{project}/experiments/results?baseline={experiment_id}"


# ---------------------------------------------------------------- notebook-only setup (writes)
def _prompt_version(langfuse, name: str, template: str) -> int:
    metas = [m for m in langfuse.api.prompts.list(name=name, limit=100).data if m.name == name]
    for v in sorted({v for m in metas for v in (m.versions or [])}):
        row = langfuse.api.prompts.get(name, version=v)
        if row.prompt == template and all((row.config or {}).get(k) == val for k, val in PROMPT_CONFIG.items()):
            return v
    return langfuse.create_prompt(name=name, prompt=template, type="text", config=PROMPT_CONFIG,
                                  commit_message="module 10: get-or-create by content").version


def bootstrap(langfuse, *, prefix: str = "") -> dict:
    """Get-or-create what the gate reads: prompt versions + labels (a label is only set when missing), the score
    config, and the regression dataset (seeded only when it doesn't exist). Returns a summary dict."""
    names = names_for(prefix)
    versions = {label: _prompt_version(langfuse, names["prompt"], tpl) for label, tpl in LABEL_TEMPLATES.items()}
    for label, version in versions.items():
        try:
            langfuse.api.prompts.get(names["prompt"], label=label)
        except NotFoundError:
            langfuse.update_prompt(name=names["prompt"], version=version, new_labels=[label])
    labels = {label: langfuse.api.prompts.get(names["prompt"], label=label).version for label in LABEL_TEMPLATES}
    warnings = [f"{label!r} is on v{labels[label]}, but its template is v{versions[label]}"
                for label in LABEL_TEMPLATES if labels[label] != versions[label]]
    configs = [c for c in langfuse.api.score_configs.get(page=1, limit=100).data
               if c.name == names["config"] and not c.is_archived]
    config = configs[0] if configs else langfuse.api.score_configs.create(
        name=names["config"], data_type="BOOLEAN", description="agent handed off to the expected team (code score)")
    try:
        langfuse.api.datasets.get(names["dataset"])
        seeded = 0
    except NotFoundError:
        langfuse.create_dataset(name=names["dataset"], description="regression set (seeded by module 10; module 09 "
                                "builds it from labelled traces)", metadata={"seeded_by": "module 10"})
        items = regression.seed_items(names["dataset"])
        for it in items:
            langfuse.create_dataset_item(dataset_name=names["dataset"], **it)
        seeded = len(items)
    langfuse.flush()
    active = _active_items(langfuse, names["dataset"])
    return {"names": names, "labels": labels, "warnings": warnings, "config_id": config.id, "seeded": seeded,
            "items": len(active), "dataset_version": max(i.version for i in active)}
