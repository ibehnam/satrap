# satrap

Recursive orchestration for agentic CLI tools (planner, worker, verifier) with a single JSON todo tree as the source of truth.

This repository currently contains scaffolding code. Planner + verifier are wired to Claude Code (`claude`) using JSON schemas loaded from files via `jq -c . schema.json`. The worker backend is still a placeholder.

## Quick usage

```bash
python3 -m satrap "your task here"
```

If you are in tmux, `satrap` runs in a new pane inside a single window (`$SATRAP_TMUX_WINDOW`, default `satrap`) and the pane auto-closes on completion. Use `--no-tmux` to run in the current pane.
