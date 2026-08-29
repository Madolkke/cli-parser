# Minimal Test Set

```text
demo.values/
  inputs/001.txt
  schema.json
  template.ttp
  expected.json
```

`schema.json` describes one record, while `expected.json` contains one record
for every input. The template must be run through the deterministic baseline
before hashes are updated in the root manifest.
