# Real Command Output Corpus

This corpus contains public command-output fixtures selected for live testing of
the TTP generator. Each case groups one to five outputs of the same command.
Only command output is retained; prompts and echoed commands are removed.

The corpus is intentionally separate from pytest. Use
`scripts/run_live_corpus.py` to inspect, preflight, or run it against a real
OpenAI-compatible model.

## Sources

- `ntc_templates/`: Network to Code `ntc-templates` tag `v9.2.0`, commit
  `891746e659e3a25d5065ee9dac29e7de5760bdf7`, licensed under Apache-2.0.
- `ttp_templates/`: `dmulyalin/ttp_templates` tag `0.5.9`, commit
  `307f16812503f3470897020c2267101bcf7af5d5`, licensed under MIT.

The complete upstream license texts are in `licenses/`. Expected YAML results,
TextFSM/TTP templates, mock data, and JSON command outputs are not included.

These are public raw CLI test fixtures and may have been sanitized or curated by
their upstream projects. They should not be described as unmodified production
captures. Future private command output must be redacted before it is added or
sent to a model provider.
