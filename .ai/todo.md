# satrap scaffolding

## Plan
- [x] Define on-disk contract: `.satrap/todo.json` (single source of truth), `todo-schema.json`, `satrap/phrases.txt`, `satrap/lessons.md`, and generated render files under `.satrap/`.
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
- Keep orchestration “one source of truth”: all status/children updates go through `.satrap/todo.json`.
- Rendering shows all `text` in-scope, but only `details` and `done_when` along the active path (and always shows `done_when` for the current step).

## Results
- Added a scaffolded `satrap` Python package with a working CLI (`python -m satrap --help`).
- Implemented todo tree load/save, path-aware rendering, dependency scheduling, phrase generation, and git worktree operations (with a dry-run no-op git client).
- Left explicit placeholders for wiring the external planner/worker/verifier CLIs and for aggregate verification/root merge logic.
- Wired planner + verifier backends to Claude CLI with schemas loaded via `jq -c` from `todo-schema.json` and `verifier-schema.json` (run with `--output-format json`, extract final `result`, then parse).
- Updated tmux integration so running `satrap` in tmux spawns a new pane in a dedicated window (`$SATRAP_TMUX_WINDOW`, default `satrap`) and auto-closes it on completion.
- Wired worker backend to Claude Code using `--dangerously-skip-permissions` and tiered models: `ccss-haiku` → `ccss-sonnet` → `ccss-opus` → `ccss-default` (configurable via `--worker-tiers`, `--worker-cmd`).

## 2026-02-09: Tmux Per-Worktree Panes + Reliable Re-runs

### Plan
- [x] Make tmux behavior match spec: worker attempts run in a new tmux pane per step worktree and panes remain visible after exit.
- [x] Prevent "satrap did nothing" on a new task: reset/replace `.satrap/todo.json` when task input changes, with archival history under `.satrap/todo-history/`.
- [x] Improve observability: always print which `todo.json` is loaded, whether it's complete, and when satrap is exiting because there's nothing to do.
- [x] Verify locally with `--no-tmux --dry-run` that reset and logging work; then verify in tmux manually that panes show worker output.

### Results
- [x] Worker attempts now run in per-step worktree tmux panes (remain visible after exit) instead of only running in the top-level satrap pane.
- [x] `.satrap/todo.json` now resets when task input changes and the previous plan is complete (or when forced via `--reset-todo`), with archival copies under `.satrap/todo-history/`.
- [x] Satrap now prints explicit diagnostics about which `todo.json` is loaded, its context, and whether it is exiting because all steps are already done.

## 2026-02-09: Comprehensive Test Suite (excluding tmux)

### Plan
- [x] Enumerate all `satrap/*.py` modules except `satrap/tmux.py` and map test cases per public behavior and edge-path.
- [x] Use parallel subagents to author pytest files in `tests/`, with one focused ownership slice per module group.
- [x] Integrate agent outputs, resolve overlaps, and ensure imports/fixtures/mocks are consistent across the suite.
- [x] Run the full test suite, fix failures, and rerun until green.
- [x] Update `AGENTS.md` with explicit test command guidance for this repository.
- [ ] Stage all changes, commit, merge to `main`, and push `main` to `origin`.

### Results
- [x] Added comprehensive pytest coverage for all `satrap/*.py` modules except `satrap/tmux.py`.
- [x] Added test files: `test_agents.py`, `test_claude_cli.py`, `test_cli.py`, `test_dag.py`, `test_entrypoints.py`, `test_git_ops.py`, `test_orchestrator.py`, `test_phrases.py`, `test_render.py`, `test_todo.py`.
- [x] Verified full suite with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q` -> `95 passed`.
- [ ] Pending git stage/commit/merge/push.


## 2026-02-09: Targeted Tests for render.py + claude_cli.py

### Plan
- [x] Review `satrap/render.py` and `satrap/claude_cli.py` behaviors and map each requested edge case to explicit pytest cases.
- [x] Add `tests/test_render.py` with coverage for ancestor/path rendering, status glyph fallback, planner/worker/verifier instruction blocks, lessons extraction/section behavior, and verifier prompt formatting (empty commits + diff trimming).
- [x] Add `tests/test_claude_cli.py` with coverage for JSON envelope parsing paths, structured_output preference, result-string fallback parse, jq error path, and `run_claude_json_from_files` stream behavior via monkeypatched `subprocess`/`selectors`.
- [x] Run targeted pytest selection for the new files, fix failures, and rerun until green.
- [x] Document outcomes and changed files in this section.

### Results
- [x] Added `tests/test_render.py` with 7 tests covering path-aware rendering, glyph fallback, role instructions, lessons extraction/loading behavior, and verifier prompt formatting edge cases.
- [x] Added `tests/test_claude_cli.py` with 5 tests covering envelope parsing branches, structured-output precedence, result-string best-effort JSON parse, jq missing-path error handling, and streamed subprocess/selectors behavior.
- [x] Verification command: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_render.py tests/test_claude_cli.py`.
- [x] Outcome: `12 passed in 0.03s`.

## 2026-02-09: `satrap/orchestrator.py` Edge Test Coverage

### Plan
- [x] Build test harness fakes for planner/worker/verifier/git and prompt writers so tests never call external tools.
- [x] Add `run()` coverage for root flow and start-step flow parent-worktree selection.
- [x] Add `_ensure_planned()` coverage for root no-op/root plan, specific-step no-op, atomic refinement, and child upsert behavior.
- [x] Add `_implement_atomic()` coverage for tier retry on worker non-zero, verifier reject reset/retry, and final blocked transition with lesson writes.
- [x] Add merge/state coverage for `_merge_step_into_parent()` setting `done` and saving.
- [x] Add `_load_or_init_todo()` coverage for initial create, reset archive path, mismatch rejection, and complete-plan replacement.
- [x] Add `_append_under_section()` coverage for empty text bootstrap, existing placeholder replacement, and missing-header insertion.
- [x] Run targeted pytest for the new orchestrator tests and capture outcomes.

### Results
- [x] Added `tests/test_orchestrator.py` with 17 isolated tests covering `run()`, `_ensure_planned()`, `_implement_atomic()`, blocked/lesson behavior, `_merge_step_into_parent()`, `_load_or_init_todo()`, and `_append_under_section()`.
- [x] Used fake planner/worker/verifier/git backends and monkeypatched prompt writers to keep tests hermetic and avoid external CLI/tool invocations.
- [x] Verification command: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_orchestrator.py -q`.
- [x] Outcome: `17 passed in 0.06s`.

## 2026-02-09: Per-Worktree Panes + Background-Safe UX

### Plan
- [x] Add orchestrator-managed worktree pane lifecycle map and deterministic per-step color tags.
- [x] Add/extend tmux helpers for documented background split/send/wait/kill/capture behavior with no focus stealing.
- [x] Route planner/worker/verifier execution through the step pane context with fallback behavior preserved.
- [x] Trim noisy lifecycle logging and keep concise role/step/pane summaries in control pane output.
- [x] Add/update tests for pane lifecycle, pane routing, no-focus behavior, and status/log expectations.
- [x] Run full pytest suite plus weather dry-run smoke verification.

### Results
- [x] Implemented per-step pane lifecycle in `satrap/orchestrator.py`: create once, reuse per step, close on step completion and orchestrator exit (best-effort).
- [x] Updated `satrap/tmux.py` with module docstring, detached pane creation by default (`-d`), pane context struct, send/wait/kill helpers, and color helpers.
- [x] Routed planner/verifier structured runs and worker attempts through shared pane context when available while preserving non-pane fallback behavior.
- [x] Updated CLI auto-spawn and worker fallback pane spawning to non-focus behavior (`select=False`).
- [x] Replaced textual `[step|color]` tokens with ANSI-colored lifecycle labels in control output and retained concise lifecycle lines.
- [x] Added/updated tests in `tests/test_orchestrator.py`, `tests/test_claude_cli.py`, and `tests/test_cli.py` for pane routing, pane opt-out, tmux JSON path, and no-focus spawn settings.
- [x] Verification:
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q` -> `210 passed, 1 warning`
  - `python3 -m satrap --dry-run --no-tmux --reset-todo "get the weather in dallas and toronto"` -> passes with colored lifecycle logs.
