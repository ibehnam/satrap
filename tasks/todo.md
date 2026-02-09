# satrap scaffolding

## Plan
- [x] Define on-disk contract: `todo.json` (single source of truth), `todo-schema.json`, `satrap/phrases.txt`, `satrap/lessons.md`, and generated render files under `.satrap/`.
- [x] Update `todo-schema.json` to include `details` (required for path-aware rendering).
- [x] Implement Python package + CLI entrypoint (`python -m satrap ...`).
- [x] Implement core todo operations: load/save, find-by-number, merge children, dependency scheduling, path-aware rendering.
- [x] Implement phrase generator backed by macOS dictionary with uniqueness via `satrap/phrases.txt`.
- [x] Implement git worktree helpers (create/switch by cwd, commit-if-needed, merge-to-parent, undo-to-base).
- [x] Implement orchestration flow:
  - break down via planner agent (placeholder backend)
  - if atomic: worker loop (tiers) + commit + verify + retry/undo + merge + status update
  - else: recurse into children in dependency order
- [x] Provide clear placeholders/contracts for the external planner/worker/verifier CLIs.
- [x] Quick verification: `python -m satrap --help`, minimal `--dry-run` smoke test (uses no-op git client).
- [x] Fix orchestrator correctness: restore accidentally-indented todo persistence methods and handle non-zero worker exit codes.
- [x] Make Claude structured output reliable: run planner/verifier with `--output-format json`, extract the final `result`, then parse JSON.

## Notes
- Keep orchestration “one source of truth”: all status/children updates go through `todo.json`.
- Rendering shows all `text` in-scope, but only `details` and `done_when` along the active path (and always shows `done_when` for the current step).

## Results
- Added a scaffolded `satrap` Python package with a working CLI (`python -m satrap --help`).
- Implemented todo tree load/save, path-aware rendering, dependency scheduling, phrase generation, and git worktree operations (with a dry-run no-op git client).
- Left explicit placeholders for wiring the external planner/worker/verifier CLIs and for aggregate verification/root merge logic.
- Wired planner + verifier backends to Claude CLI with schemas loaded via `jq -c` from `todo-schema.json` and `verifier-schema.json` (run with `--output-format json`, extract final `result`, then parse).
- Updated tmux integration so running `satrap` in tmux spawns a new pane in a dedicated window (`$SATRAP_TMUX_WINDOW`, default `satrap`) and auto-closes it on completion.
- Wired worker backend to Claude Code using `--dangerously-skip-permissions` and tiered models: `ccss-haiku` → `ccss-sonnet` → `ccss-opus` → `ccss-default` (configurable via `--worker-tiers`, `--worker-cmd`).
