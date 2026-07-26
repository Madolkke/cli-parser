# Evaluation case format

All paths are relative to the repository root. The versioned manifest is `evals/ttp_generation/manifest.json`; targets belong in `evals/ttp_generation/targets/`.

## Manifest

The root has exactly `version` and `cases`; version is currently `1`. Each case has exactly:

```json
{
  "id": "source.platform.command_name",
  "command": "show something",
  "suites": ["smoke"],
  "tags": ["vendor", "shape"],
  "inputs": [
    {
      "path": "testdata/path/capture.txt",
      "sha256": "64 lowercase hexadecimal characters"
    }
  ],
  "target": {
    "path": "evals/ttp_generation/targets/source.platform.command_name.json",
    "sha256": "64 lowercase hexadecimal characters"
  }
}
```

Use 1-5 unique ordered inputs. IDs and tags are lowercase stable identifiers. Do not add comments or extra keys. SHA-256 covers the exact bytes on disk, including line endings and final newline.

## Target

The target root has exactly `records` and `schema_contract`:

```json
{
  "records": [
    {"items": [{"name": "alpha", "state": "up"}]}
  ],
  "schema_contract": [
    {"path": "/", "type": "object", "required": false},
    {"path": "/items", "type": "array", "required": true},
    {"path": "/items/*", "type": "object", "required": false},
    {"path": "/items/*/name", "type": "string", "required": true},
    {"path": "/items/*/state", "type": "string", "required": true}
  ]
}
```

There must be exactly one record per input, in the same order. Records must be non-empty root objects. Objects are closed by the contract: every observed path is declared and every declared path is observed.

Supported node types are `object`, `array`, `string`, `integer`, `number`, and `boolean`. Field names are ASCII `snake_case`. `/` is the root. `*` is permitted only as an array item segment. Array item nodes are never required; object properties are required in this benchmark's stable projection.

Every string value in record `n` must occur literally in input `n`. This is a provenance check, not permission to copy headings or control text.

## Offline validation

Run only:

```text
uv run python scripts/run_agent_evaluation.py preflight
```

The command verifies strict JSON, duplicate keys, traversal-free paths, UTF-8 and size limits, hashes, terminal noise and credential patterns, record/input cardinality, value provenance, and closed schema assertions. It does not prove that an annotation is semantically correct, so perform the human review first.
