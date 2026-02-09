# Lessons

## Codex
- Tooling: Use the dedicated `functions.apply_patch` tool for patch edits; never embed `apply_patch` inside `functions.exec_command`.

## Satrap
- Claude CLI: Never pass JSON schemas inline to `claude --json-schema`. Always load schemas from a file (e.g. `--json-schema "$(jq -c . schema.json)"`).
- Claude structured output: Prefer `--output-format json` and extract the final `type=="result"` object's `result` string (then parse JSON from that). Empirically, placing `--output-format json` at the end of the argv is more reliable.
- Tmux debugging: If satrap appears to "do nothing", it may be reusing an already-complete `todo.json` and immediately exiting. Make new task runs reset/replace `todo.json` (ideally with an archive) and print explicit "nothing to do" diagnostics.
- Pane shells: Don't assume `$SHELL` matches the user's interactive shell; prefer a deterministic pane shell (default `/bin/zsh`) with an override (e.g. `SATRAP_PANE_SHELL`) to avoid surprises from login profiles.
- Don't add implicit "legacy migration" behaviors unless explicitly requested; keep default file locations and state transitions simple and predictable.
