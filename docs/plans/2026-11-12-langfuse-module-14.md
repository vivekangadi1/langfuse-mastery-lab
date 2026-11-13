# Langfuse Mastery: Module 14 (Capstone: the production loop) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. The workflow is the same as the module 06–13 plans.

**Goal:** modules 01–13 built every piece separately. The capstone runs them as **one loop on one planted
regression**, the way a production team lives through an incident, and leaves an **evidence trail** a leader can read:

```
traffic ─► judge ─► alert ─► review queue ─► human labels ─► dataset items ─► prompt fix ─► experiment vs baseline
   ▲                (07/08)  (11/12)  (08)                       (09)          (04)          (09/10)
   │                                                                                            │
   └── recovered traffic ◄── promote (move labels, audited) ◄── CI gate (10) ◄──────────────────┘
                                                                   └─► a quick fix that regresses billing: BLOCKED
```

Scenario (design row 14): "traffic → judge → alert → review → dataset → prompt fix → experiment → CI → promote, on a
planted regression". The capstone is also the **leadership demo**: its last product is an incident report (Markdown +
HTML) with the timeline, the headline numbers (time to detect / fix / recover, customers affected, the bad fix that
was stopped) and a Langfuse link for every claim.

**The planted regression:** someone deploys module 04's `staging` candidate (login → billing) **straight to `live`**,
the label the agent serves, without the gate. `production` stays on the approved version. Nothing errors, cost and
latency don't move: only the **judge** sees it (live traffic has no ground truth; the judge is the quality signal).

**The loop must not be fooled (planted):** the first fix is a **quick fix** (a find-and-replace `billing_specialist →
account_security` over the routing rules). It fixes login, so it passes the incident's own new tests, but it also sends
double charges to account security. The CI gate (module 10, on the dataset that now holds the incident's items) must
**block** it, and the loop must not promote it. The second fix (production's rules with an explicit "never billing for
login" line) must ship, be promoted (`production` + `live`), and traffic after it must be healthy and the alert resolve.

**The learner's TODOs are the orchestration and the decisions between stages** (everything else REUSES the learner's
earlier modules): what goes into the review queue, how human labels become dataset items, the release policy (gate
thresholds), when to promote, and the evidence trail.

**Prerequisites (the first cell checks them and names what to finish first):** module 04 `prompting.py`, 06
`scoring.py`, 07 `judging.py`, 08 `review.py`, 09 `experiments.py`, 10 `ci_gate.py` + `ci/release_gate.py` +
`ci/test_release_gate.py`, 11 `analytics.py` + `weekly_pull.health_metrics`, 12 `alerting.py` + `alert_job.run_once`.
Earlier modules' **objects** need not exist: `ci/harness.py bootstrap()` (module 10) get-or-creates the prompt + labels,
the `handoff_correct` config and the regression dataset; a given cell get-or-creates the review configs and the queue.

**Conventions:** as in modules 07–13. Project-global names are `PREFIX + base`; the learner uses `PREFIX = ""`,
validation `zz_ref_m14_`.

| object | name (`PREFIX=""`) |
|---|---|
| managed prompt | `support-agent-system`: `production` = approved, `live` = served (module 12), `staging` = bad (module 04), `hotfix` = the fix under review (new) |
| regression dataset | `support-regression` (module 09/10; incident items are added to it) |
| score configs | `handoff_correct` (06), `human_routing_ok` + `review_error_type` (08), **`expected_handoff`** (CATEGORICAL, new) |
| annotation queue | **`incident-review`** (the 3 configs; queues can't be edited or deleted) |
| judge | `routing_judge`: live EVAL if the learner's module 07 rule `support-live-eval` is enabled (traffic tagged `live-eval`), else module 08's stand-in |
| stable tag / environment | `prod-loop` / `production` |
| gate | module 10's `release-gate` experiments, `CANDIDATE_LABEL=hotfix`, policy from `loop.LOOP_POLICY` |
| alert | the module 12 DIY job with `loop.LOOP_RULES` (judge pass rate), receiver `127.0.0.1:80/m14/alerts`, secret `M14_ALERT_SECRET` |
| outputs (git-ignored) | `langfuse_mastery/loop_out/<run id>/`: receiver log, ticks, gate artifacts, `evidence.json`, `incident-report.md/.html` |

**Verified live (probe, 2026-11-12, server 4.77 / SDK 5.2; probe objects `zz_probe_m14_*`):**
- **Prompt version reads** (`api.prompts.get(name, version=)`) carry, in `model_extra` (and in `.dict()`), `id` (the
  version's own id), `createdAt`, `updatedAt`, `createdBy` (`API`), `projectId`, `isActive`, plus `commitMessage`.
- **A label move bumps `updatedAt` on BOTH versions** (the one that gains the label and the one that loses it), so
  `updatedAt` cannot date a deploy. The prompt itself keeps no label history (audit logs are Enterprise, module 13).
- **Prompt comments are the audit trail on OSS:** `api.comments.create(project_id=, object_type="PROMPT",
  object_id=<prompt VERSION id>, content=)` works; `comments.get(object_type="PROMPT", object_id=)` returns them with a
  server `createdAt`. No update/delete (module 08). The capstone writes one comment per label move
  (`[<run tag>] <action>: …`) and dates the deploy, the candidate and the promotion by them.
- `update_prompt(name, version, new_labels=["production", "live"])` moves **both** labels in one call (the old holder
  loses both); labels already on the version stay (`hotfix` stayed). A custom label (`hotfix`) needs no setup.
- **Clocks:** comment `createdAt` was +0.12 s vs this host (module 09 saw the server −3 s): every cross-source ordering
  (host-stamped traces and deliveries vs server-stamped comments, queue items, dataset items) uses a 10 s tolerance.
- Re-used facts: annotation queue items have `createdAt` / `completedAt` (08); dataset items `createdAt` / `version`
  (server time) / `sourceTraceId` (09); ANNOTATION scores need `config_id` and should carry `environment` + `timestamp`
  (08, 11); score views of the Metrics API filter on the **score's** timestamp (11): every traffic window closes only
  after its judge scores exist (EVAL scores arrive ~30 s after ingestion); window bounds are floored to the ms (12).

---

### Task 1: lab (non-Langfuse)
- `prompts.QUICK_FIX_TEMPLATE` (`WRONG_HANDOFF_TEMPLATE.replace("billing_specialist", "account_security")`),
  `prompts.LOOP_FIX_TEMPLATE` (the clean candidate + "never send login, password or account access issues to
  billing_specialist"), `prompts.HOTFIX_LABEL = "hotfix"`.
- `oracle_llm.billing_team(system_prompt)`: the dry oracle follows the prompt's double-charge rule too (default
  billing), so a dry quick fix misroutes double charges like the real model. Modules 11/12 are unchanged.
- `ci/harness.py` (module 10's given harness): the dry-run oracle reads the served prompt's login/billing rules
  (`_oracle(label, expected, text)`); module 10's dry behaviour is unchanged (staging = login → billing).
- `traffic.loop_cases(run_id)`: 3 requests × 3 windows (`baseline` good, `incident` bad, `recovered` fix): login cust_02,
  locked out cust_03 (both NEW to the dataset: they become incident items), double charge 8129.
- `langfuse_lab/loop_spec.py`: names, stages + the evidence schema, `audit_text` / `parse_audit`, `evidence_problems`,
  `render_markdown`, `render_html`, `write_report`.
- Tests. Commit: "lab: module 14 loop cases, quick fix + fix prompts, prompt-aware dry oracles, loop spec + report".

### Task 2: verify: module 14 checks
`module_14_checks(run_tag, client, *, prefix, environment, deliveries, alert_secret, ticks, queued, items, releases,
evidence, allow_dry=False)`; one snapshot: the run's observations, scores per trace (with `annotation`), prompt versions
+ their audit comments, the queue + its items, the dataset + its items, the run's gate experiments (+ items, re-runs,
`ci_gate` scores, baselines).
1. traffic: 3 × 3 roots; baseline on the good template version, incident on the bad template version, recovered on the
   shipped fix; `live` + `production` now on the fix.
2. live traffic judged (mode reported): every root has a judge verdict; the incident's login requests judged False.
3. the alert fired once, signed, with the stored judge pass rates and the incident's version.
4. the review queue = the alert window's judge-flagged traces + ≥1 control, no duplicates, nothing outside the window.
5. human review complete (UI or API): COMPLETED items, ANNOTATION labels with config + queue, expected team on failures.
6. confirmed failures became dataset items (expected = the human's label, source trace, incident metadata), none for
   controls or false alarms, created after the review; TODO 3's pin covers them.
7. **planted: the quick fix was blocked** (gate block recomputed from stored scores, ≥1 confirmed regression outside the
   incident items) and never promoted (no `production`/`live`, no traffic, no promotion audit).
8. the fix shipped through a gate that ran the incident items on the pinned version, all passing, `max_regressions` 0,
   baseline = production.
9. promotion: `production` + `live` on the fix, an audit comment, ordered after the ship decision and before recovered.
10. recovered: healthy judge pass rate, exactly one resolved delivery after the firing one.
11. the evidence trail matches the stored data (every stage, causal order, numbers, links, durations, report files).
12. native prompt webhook (module 12) on the deploy and the promotion (⚠️ while the stack can't call this machine).
Commit: "lab: module 14 checks (...)".

### Task 3: learner `langfuse_mastery/loop.py` + tests
Given `LOOP_RULES`; TODO 1: `LOOP_POLICY` (the release policy as data), `review_selection`, `incident_items`, `gate_env`,
`promotion_decision`, `timeline`. Tests `tests/test_loop.py`. Commit: "mastery: module 14 loop stubs + tests (learner
implements)".

### Task 4: notebook `src/14_capstone.py`, validation
Prerequisites; TODO 1; given stack (bootstrap, prompt versions + start state, configs + queue, judge mode, receiver,
instrumentation, audit, deploy, windows); baseline; the planted deploy; incident; alert tick; TODO 2 `fill_review_queue`;
human review (UI; scripted fallback `SCRIPTED_REVIEW`); TODO 3 `labels_to_dataset`; given fix templates + `run_gate`;
TODO 4 `release` (quick fix → blocked, fix → shipped + promoted); recovered; resolve tick; TODO 5 `collect_evidence` +
the report; verify; the leadership demo walk-through; UI tour; notes.
Budget: 9 traffic turns + 3 gate runs on a 9-item dataset (8 LLM items) ≈ 95 Azure calls; dry rehearsal first
(`LOOP_DRY_RUN=1`, oracle LLM + dry gate, 0 Azure), then one live run.

---

### As implemented (2026-11-13)

- **Files:** lab `prompts.QUICK_FIX_TEMPLATE` / `LOOP_FIX_TEMPLATE` / `HOTFIX_LABEL`, `oracle_llm.billing_team`, the
  prompt-aware dry oracle in `ci/harness.py`, `traffic.loop_cases`, `loop_spec.py` (+ `tests/test_loop_lab.py`, 8 tests);
  `verify.module_14_checks` / `check_module_14` (+ `tests/test_verify_m14.py`, 15 tests on a fake world); learner
  `loop.py` (TODO 1: `LOOP_POLICY`, `review_selection`, `incident_items`, `gate_env`, `promotion_decision`, `timeline`;
  16 tests); notebook TODOs 2 `fill_review_queue`, 3 `labels_to_dataset`, 4 `release`, 5 `collect_evidence`.
- **Rehearsals (0 Azure):** dry run 1 stopped at the alert: the stand-in judge's `create_score` ran outside the trace
  context, so the scores got the CLIENT's environment (`default`) and the alert's Metrics query (`environment =
  production`) never saw them. Fix: `create_score(environment=ENV)`; verify now fails with that explanation
  (`judged_check`). Dry run 2 (`run:m14-18f14c`): MODULE PASSED, 11 ✅ 1 ⚠️, first verify attempt.
- **Live run 1 (`run:m14-8559cf`, `PREFIX="zz_ref_m14_"`, references swapped in, stand-in judge, scripted review):
  107 Azure calls** (25 traffic + 82 gate). Live behaviour: baseline 3/3 routed right; after the planted deploy both
  login requests went to `billing_specialist` (judge 1 → 0.33), the alert fired once (`judge-routing-drop`, prompt-v2),
  the queue held the 2 flagged requests + the double-charge control, the scripted reviewer confirmed 2 failures, they
  became 2 new items; the **quick fix was blocked** (the double charge confirmed regressed 1.0 → 0.0, invoice and the
  vague login flaky, exit 1) and not promoted. **Then the real fix was blocked too**: `invoice_request` still carried
  module 10's seed label `none` (module 09's label fix had never been applied to this dataset); production answered it
  without a handoff on both attempts, the fix routed it to billing (correct per the policy) on both. A wrong label made
  the gate block a correct fix. Re-verified on the stored data: judged, queue, review, dataset and **planted** ✅; the
  alert's signature could not be re-checked (the per-run secret was not kept); fix/promotion/recovered/evidence ❌ (no
  promotion happened). Counterfactual from the stored outputs: with the corrected label the fix gate is 0 regressions +
  1 improvement = ship.
- **Design fix:** the given setup applies module 09's `LABEL_FIX` when it is still pending (`ensure_label_fix`, a new
  item version; a no-op after module 09), with the lesson in the notebook ("a blocked good fix is often a bad test: fix
  the label, keep the policy"). Dry run 3 (`run:m14-e4770d`, 0 Azure) re-rehearsed the whole loop with it: MODULE
  PASSED, 11 ✅ 1 ⚠️ (label fixed `none` → `billing_specialist`; quick fix blocked on 2 confirmed regressions, the
  double charge and the invoice; the fix shipped, was promoted, the alert resolved, the evidence matched).
- **Not re-run live:** a second live run costs ≈105 more Azure calls (the cap was ~100); it needs the user's approval
  (`<scratchpad>/m14/live2.sh`, fresh `PREFIX="zz_ref_m14b_"`, pinned alert secret).
- **Cleanup:** archived configs `zz_ref_m14_{handoff_correct,human_routing_ok,review_error_type,expected_handoff}`;
  archived the 9 items of `zz_ref_m14_support-regression` (no dataset delete); deleted the prompts
  `zz_ref_m14_support-agent-system` and `zz_probe_m14_prompt`; deleted the dry rehearsals' traffic traces (33, tagged
  `dry-run` + `zz_ref_m14_prod-loop`) and the 89 traces of the 15 dry gate experiments. Left: the queue
  `zz_ref_m14_incident-review` (no delete API), the live run's traces and gate experiments (`run:m14-8559cf`, evidence),
  comments on the deleted prompt versions and on traces (no comment delete API).
