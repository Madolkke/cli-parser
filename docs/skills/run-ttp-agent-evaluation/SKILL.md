---
name: run-ttp-agent-evaluation
description: Run a configured black-box TTP Agent evaluation in this repository and analyze the resulting Laminar traces. Use when asked to run or rerun baseline/smoke cases, compare model or reasoning configurations, inspect convergence or latency, or report correctness, candidate, token, cost, and trace metrics.
---

# Run TTP Agent Evaluation

Run a controlled real-model evaluation through the existing repository runner, then interpret its deterministic score and Laminar telemetry without changing product behavior or leaking sensitive data.

Read `AGENTS.md`, `docs/agent-evaluation.md`, and [references/run-and-analyze.md](references/run-and-analyze.md) completely before a live run or trace analysis.

## Workflow

1. Confirm the evaluation question, selected suite or case, trials, model settings, reasoning configuration, and budgets. For a comparison, change one variable only and use a new evaluation name per arm.
2. Run `list` and `preflight` before the live request. These operations are offline and do not initialize Laminar.
3. Confirm all required model and Laminar environment variables are already supplied privately. Never print, persist, or place keys in commands, prompts, logs, Trace metadata, or reports.
4. Apply the requested configuration only to the current shell process. Do not edit `.env`, source defaults, prompts, product API, or test assets. Use `--env-file .env` only when the user has explicitly configured that private file.
5. Run `scripts/run_agent_evaluation.py run` with exactly one of `--suite` or `--case`. Use `--concurrency 1` for latency, convergence, candidate, or configuration analysis. Do not automatically retry failed trials.
6. Read the resulting safe local `summary.json` and inspect Laminar through its existing read-only evaluation/Trace views or SQL projections. If raw model output, template, capture, or Thinking is necessary for an explicitly requested diagnosis, inspect it only in Laminar and report an abstracted finding, never a verbatim transcript.
7. Report deterministic correctness separately from telemetry completeness. Include configuration, coverage, result quality, funnel/convergence, resource breakdown, and a bounded next action.

## Operating Rules

- Official evaluation assets are single-input: the complete `all` suite is 31 cases and `smoke` is 5 cases. The runner's public API boundary remains unchanged.
- A live run requires a model configuration plus `LMNR_PROJECT_API_KEY`, `LMNR_BASE_URL`, `LMNR_HTTP_PORT`, `LMNR_GRPC_PORT`, and `LMNR_FRONTEND_PORT`.
- Treat a one-trial result as diagnostic evidence, not a reliability claim. Use repeated trials for rates or configuration comparisons, and state the sample count.
- Keep high-budget diagnostics isolated from default acceptance runs. The documented diagnostic profile is `7200s / 32 rounds / 24 submissions / 120s model timeout`.
- `strict_pass` derives only from deterministic `candidate_pass`. A missing Trace, delayed telemetry, or Trace-ID mismatch must be reported as telemetry incompleteness, never folded into strict correctness.
- Do not use the golden-authoring Skill, alter golden data, invoke replay/debugger, or feed Trace findings back into the running Agent. Human review labels are optional and only written via the runner's explicit `review` command after the run.
- Local artifacts must stay sanitized. Do not copy records, captures, raw inputs, templates, model text, Thinking, credentials, or exception bodies out of Laminar.

## Analysis Priorities

Analyze the run in this order:

1. Deterministic outcome: generation success, independent acceptance, records exactness, Schema exactness, field F1, and issue taxonomy.
2. Convergence funnel: Schema frozen, TTP entered, first accepted candidate, `finish_generation`, and final acceptance. Distinguish no valid candidate from a valid candidate that was not finished.
3. Reliability: termination reason, fault domain, zero-tool retries, model/tool errors, submission and round consumption, cleanup, and telemetry completeness.
4. Efficiency: root, phase, `context.fit`, `agent.round`, LLM, TOOL, cleanup, and final-acceptance durations; input/output/reasoning tokens, cost, context growth, and explained-duration ratio.

Use the reference for exact commands, environment variables, interpretation rules, and report format.
