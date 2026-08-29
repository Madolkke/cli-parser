---
name: generate-cli-parser-eval-cases
description: Create or review the canonical four-part CLI parser test sets offline.
---

# Generate Standard Test Sets

Use this skill only for offline authoring and review of `evals/test_sets/`.
Read the repository `AGENTS.md` first. Never run a live model evaluation, use a
Trace as a golden source, or copy an Agent artifact into a test set.

Each independent test set contains exactly:

```text
<case-id>/
  inputs/001.txt ... 005.txt
  schema.json
  template.ttp
  expected.json
```

The inputs are same-command UTF-8 command echoes in source order. The Schema,
standard TTP template, and expected records must be reviewed together for all
inputs. `expected.json` is a list of one object per input. The root manifest is
`evals/test_sets/manifest.json` and contains only index metadata and SHA-256
values.

Before changing assets, preserve the raw source and third-party attribution in
the review context. Write the standard template independently, run the local
deterministic baseline, and calculate hashes over the final bytes. Do not add
`target`, `schema_contract`, per-case external schema paths, or model output
files.

Run the offline checks:

```powershell
uv run python scripts/run_test_sets.py list --manifest evals/test_sets/manifest.json
uv run python scripts/run_test_sets.py preflight --manifest evals/test_sets/manifest.json
uv run python scripts/run_test_sets.py run --manifest evals/test_sets/manifest.json --mode baseline --suite smoke
```

The baseline must parse every input and reproduce `expected.json` exactly.
Object key order is ignored by the evaluator; array order, scalar type,
missing fields, `null`, and empty strings remain significant. Test sets are
not a route to execute shell commands, access the network, or run the Agent.
