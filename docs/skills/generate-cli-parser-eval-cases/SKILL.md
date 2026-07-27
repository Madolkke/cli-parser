---
name: generate-cli-parser-eval-cases
description: Create, extend, or correct versioned golden cases for this repository's CLI parser black-box evaluation system from 1-5 raw outputs of one command. Use when authoring expected records, schema structure assertions, or manifest entries under evals/ttp_generation, or when validating those definitions offline.
---

# Generate CLI Parser Eval Cases

Create human-auditable golden data from raw CLI text without consulting the system under test. Treat the raw captures as the only source of truth.

## Required references

Read these files completely before editing a case:

- `references/test-case-format.md`
- `references/annotation-policy.md`
- `references/minimal-example.md`

Also read the repository `AGENTS.md`. Do not read `scripts/run_agent_evaluation.py`: it is a local-secret boundary whose live configuration is injected from the environment.

## Safety boundary

Never inspect or use any of the following while deriving a golden:

- Laminar traces, evaluations, spans, or UI pages;
- `.artifacts/` or any historical/current Agent result;
- generated TTP templates, generated Schemas, captures, or model text;
- upstream parser templates, reference YAML, JSON command results, or another parser's output;
- an LLM or the tested Agent to propose, fill, or verify expected values.

Never run `scripts/run_agent_evaluation.py run`, a live model request, replay, debugger, or judge. Running `list` and `preflight` is allowed. Do not read, modify, or broadly diff `scripts/run_agent_evaluation.py`.

## Workflow

1. Confirm that there are 1-5 non-empty raw text files for the same command and that their order is intentional. Read every file in full.
2. Identify the repeated primary business entity in each capture: for example, one interface, route, neighbor, or inventory item. Ignore headings, separators, prompts, echoes, pager text, prose notices, and other control material.
3. If two or more primary output structures are equally reasonable and would produce materially different roots or arrays, stop before editing and ask the human to choose. Explain the alternatives using field names only; do not invent a preference.
4. Apply the maximum stable semantic projection in `references/annotation-policy.md`. Preserve all primary entities and their source order, but retain only fine-grained fields that are semantically stable across every same-kind entity in every supplied capture.
5. Write one expected root object per input, in input order. Keep scalar types strict and default source-derived values to strings. Never add `null`, empty placeholders, empty objects, or empty arrays.
6. Derive the closed schema contract from those exact records. Include every object, array, item, and scalar path exactly once. Use `required: true` only for object properties; root and array-item paths use `false`.
7. Add or update the manifest entry, calculate SHA-256 over the exact file bytes, and update the target hash after the target is final. Keep paths relative to the repository root and POSIX-formatted.
8. Run `uv run python scripts/run_agent_evaluation.py preflight`. Fix every local validation error. This command must not initialize Laminar or make a network request.
9. Review only the files in scope. If a diff is needed, limit it explicitly to `evals/ttp_generation` and this skill directory. Never run an unrestricted repository diff because the evaluation entry script may contain local hard-coded keys.

## Completion report

Report the case ID, ordered input count, selected primary entity, retained field paths, excluded unstable fields, input/target hashes, and preflight result. State explicitly that no live evaluation, trace, artifact, upstream answer, or model-generated golden was used.
