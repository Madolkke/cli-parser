# Minimal synthetic example

This example is synthetic and is not part of the smoke suite.

Input 1:

```text
Bay  State  Label
1    ready  edge-a
2    down   edge-b
```

Input 2:

```text
----- Bay inventory -----
Bay  State  Label  Temperature
7    ready  core-a 31C
8    ready  core-b
```

The primary entity is a bay row. `temperature` is excluded because it is absent for bay 8 and does not exist in input 1. Headings and the separator are excluded. `bay` remains a string because it is an identifier.

Target:

```json
{
  "records": [
    {
      "bays": [
        {"bay": "1", "state": "ready", "label": "edge-a"},
        {"bay": "2", "state": "down", "label": "edge-b"}
      ]
    },
    {
      "bays": [
        {"bay": "7", "state": "ready", "label": "core-a"},
        {"bay": "8", "state": "ready", "label": "core-b"}
      ]
    }
  ],
  "schema_contract": [
    {"path": "/", "type": "object", "required": false},
    {"path": "/bays", "type": "array", "required": true},
    {"path": "/bays/*", "type": "object", "required": false},
    {"path": "/bays/*/bay", "type": "string", "required": true},
    {"path": "/bays/*/state", "type": "string", "required": true},
    {"path": "/bays/*/label", "type": "string", "required": true}
  ]
}
```

If a human instead considered each physical bay to own a varying list of readings, the primary structure would be ambiguous and the Agent should ask before writing files.
