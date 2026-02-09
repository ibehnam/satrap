# Lessons

## Codex
- Tooling: Use the dedicated `functions.apply_patch` tool for patch edits; never embed `apply_patch` inside `functions.exec_command`.

## Satrap
- Claude CLI: Never pass JSON schemas inline to `claude --json-schema`. Always load schemas from a file (e.g. `--json-schema "$(jq -c . schema.json)"`).
- Claude structured output: Prefer `--output-format json` and extract the final `type=="result"` object's `result` string (then parse JSON from that). Empirically, placing `--output-format json` at the end of the argv is more reliable.
