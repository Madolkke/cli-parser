# Evaluation Run And Laminar Analysis

## Preconditions

Run from the repository root. Read `AGENTS.md` and `docs/agent-evaluation.md` before setting a configuration.

Required for a live evaluation:

- `OPENAI_API_KEY`, `OPENAI_MODEL`, and optional `OPENAI_BASE_URL`
- `LMNR_PROJECT_API_KEY`, `LMNR_BASE_URL`, `LMNR_HTTP_PORT`, `LMNR_GRPC_PORT`, and `LMNR_FRONTEND_PORT`

Do not display values of keys. Let the private environment or explicitly requested `--env-file` supply them. The runner rejects missing configuration before network access.

## Select A Run Shape

Use a single case for a canary, latency diagnosis, or controlled configuration comparison. Use `smoke` before the complete `all` suite when checking a new configuration. Set concurrency to one whenever timing, traces, candidates, or reasoning behavior is under analysis.

| Purpose | Selection | Trials | Configuration rule |
| --- | --- | --- | --- |
| Offline asset check | `list`, then `preflight` | n/a | No model or Laminar configuration needed. |
| Canary | one `--case` | 1 | Keep defaults unless diagnosing one variable. |
| Smoke baseline | `--suite smoke` | 1 | Start with documented defaults or one controlled profile. |
| Configuration comparison | the same one `--case` | 3+ if practical | Change exactly one variable per arm; retain model, prompt version, input, budgets, and concurrency otherwise. |
| Convergence diagnosis | one `--case` | 1 | Use the high-budget profile and concurrency 1. |

Do not use a single run to claim an improvement is general. Do not automatically rerun a failure: a repeat is a separately named trial with its own Trace.

## Commands

First validate the versioned fixtures without networking:

```powershell
uv run python scripts/run_agent_evaluation.py list
uv run python scripts/run_agent_evaluation.py preflight
```

For a normal single-case run using a private `.env`:

```powershell
uv run --env-file .env python scripts/run_agent_evaluation.py run `
  --case ntc.cisco_ios.show_interfaces_status.sample_01 `
  --trials 1 --concurrency 1 --name baseline-canary
```

Apply diagnostics to the current PowerShell process only:

```powershell
$env:CLI_PARSER_GENERATION_TIMEOUT_SECONDS = "7200"
$env:CLI_PARSER_MAX_AGENT_ITERS = "32"
$env:CLI_PARSER_MAX_TEMPLATE_SUBMISSIONS = "24"
$env:CLI_PARSER_MODEL_TIMEOUT_SECONDS = "120"
uv run --env-file .env python scripts/run_agent_evaluation.py run `
  --case ntc.cisco_ios.show_interfaces_status.sample_01 `
  --trials 1 --concurrency 1 --name schema-convergence-diagnostic
```

Configure reasoning with the standard OpenAI-compatible variables only:

```powershell
$env:CLI_PARSER_MODEL_THINKING_ENABLE = "true"
$env:CLI_PARSER_MODEL_REASONING_EFFORT = "high"
```

Valid repository values are `none`, `minimal`, `low`, `medium`, `high`, and `xhigh`. Whether a provider accepts each value is a provider capability; preserve a structured provider error rather than silently changing the setting. Omit both reasoning variables to preserve the existing wire behavior. Set `CLI_PARSER_MODEL_THINKING_ENABLE=false` to explicitly send `reasoning_effort=none`.

The runner writes a sanitized result to `.artifacts/agent-evals/<run-id>/summary.json` and prints its path, Evaluation ID, and Evaluation URL. Its nonzero exit status can indicate a deterministic evaluation failure or incomplete telemetry; inspect the summary before drawing conclusions.

## Variables To Fingerprint

Keep these constant except for the deliberately tested variable:

- model name and base URL;
- thinking enablement and reasoning effort;
- temperature, max tokens, context size, model retries, and model timeout;
- total generation timeout, maximum agent rounds, template submissions, zero-tool retry limits, parser timeout, and input character budget;
- case, trial count, concurrency, prompt version, manifest version, and Laminar endpoint configuration.

The existing runner records a redacted configuration fingerprint. Never add keys to that fingerprint.

## Read The Results

Start with `summary.json`; it intentionally contains only safe values, IDs, enums, issue codes, and numeric aggregates.

| Question | Deterministic source | Laminar evidence | Interpretation |
| --- | --- | --- | --- |
| Was the result correct? | `strict_pass`, `candidate_pass`, independent acceptance, exactness and F1 | Optional corroboration only | Correctness does not depend on telemetry. |
| Did the Agent converge? | termination, issue taxonomy | `schema.phase`, `ttp.phase`, `agent.round`, candidate trajectory, `finish_generation` | Separate a rejected/no candidate from an accepted candidate that was never finished. |
| What consumed time? | run-level duration and summary metrics | root/phase/LLM/TOOL/context-fit/cleanup/final-acceptance spans | Attribute time to measured segments; state any unexplained remainder. |
| Did a model/tool failure occur? | failure category, fault domain, issue codes | LLM and TOOL status/error class | Report safe types/codes, not exception or model text. |
| Is the trace usable? | `telemetry_complete`, Trace-ID consistency | expected span tree and ingestion result | Telemetry incompleteness is separate from a failed candidate. |

Expected process tree when a full run reaches TTP:

```text
evaluation -> executor -> ttp.generate
                         |- context.fit
                         |- schema.phase -> agent.round -> LLM / TOOL
                         |- ttp.phase    -> agent.round -> LLM / TOOL
                         |                 \-> generation.deadline_cleanup (only when triggered)
                         \- final.acceptance (only after finish)
```

Use Laminar's existing evaluation and Trace pages or the runner's existing read-only SQL projections. Query only the evaluation or trace under investigation. Do not write a new query tool, alter trace data, use replay/debugger, or export raw model/command content to local files.

## Report Template

Give the user a concise analysis with these fields:

1. Scope: case or suite, trial count, run/Evaluation/Trace IDs, and whether all fixtures are single-input.
2. Configuration: model, reasoning setting, selected budgets, concurrency, and the one variable changed relative to the control.
3. Correctness: strict pass count/rate, independent acceptance, records/Schema exactness, and field/schema F1. Keep telemetry separate.
4. Funnel and reliability: Schema frozen, TTP entered, first accepted candidate, finish, final acceptance, termination/fault-domain distribution, and key issue codes.
5. Efficiency: end-to-end time, major span durations, token/cost metrics when present, context growth, and explained-duration ratio.
6. Conclusion: evidence-qualified finding, limitations such as small trial count or incomplete telemetry, and one next experiment or implementation action.

For an explicit template-quality review, inspect the authorized Laminar Trace and use only the runner's bounded `review` command. State the phase, submission index, structured label, dimensions, and safe issue codes; do not persist or quote template, capture, records, input, model text, or Thinking.
