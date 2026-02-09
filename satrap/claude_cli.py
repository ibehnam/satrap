"""Utilities for invoking the Claude Code CLI ("claude") in structured JSON mode.

This module runs `claude` with `--output-format json` and `--json-schema <schema>`, where
`<schema>` is produced by compacting a JSON Schema file via `jq -c . <schema_file>`. The
schema is always loaded from disk (never inlined in code) to match the CLI's documented
`--json-schema "$(jq -c . schema.json)"` usage and to avoid quoting/whitespace issues.

Invocation
- `run_claude_json_from_files()` reads the prompt from `prompt_file` (UTF-8) and launches:
  `claude --model <model> -p <prompt> --json-schema <compact_schema> --output-format json`
  in the provided `cwd`. The argument order intentionally keeps `--output-format json` last,
  as the CLI can be sensitive to flag ordering.

Output collection and envelope parsing
- stdout and stderr are read concurrently using `selectors`. stderr is streamed through to
  the parent process's stderr in real time, while stdout is buffered to completion because
  `--output-format json` can emit a large event envelope.
- The buffered stdout is interpreted as a "Claude Code JSON envelope". The code first tries
  to parse stdout as a single JSON value (typically a JSON array of events). If that fails,
  it falls back to parsing JSON objects line-by-line (JSONL), ignoring non-JSON lines.

Result extraction
- The extractor walks the parsed envelope from the end and selects the last event with
  `type == "result"`. From that event it prefers:
  - `structured_output` (when `--json-schema` is honored). This is treated as the primary,
    schema-validated payload and is returned as `data`. For observability it is also printed
    as pretty JSON (sorted keys) when serializable.
  - `result` (a printed string). If present, the code returns the string as `stdout` and
    attempts a best-effort JSON parse of the string for `data`. If parsing fails, `data`
    is `None`.

Return value and side effects
- `run_claude_json_from_files()` returns `ClaudeJSONResult(exit_code, stdout, stderr, data)`,
  where `stdout` is the normalized text that this module prints to `sys.stdout` (not the raw
  CLI envelope), `stderr` is the captured stderr content, and `data` is the structured payload
  when available.
- This module does not raise on a non-zero Claude exit status; callers are expected to inspect
  `exit_code` and/or `data` to decide whether a run was successful.

Error-handling assumptions
- `jq` must be installed and on `PATH`; otherwise `_jq_compact_json()` raises `RuntimeError`.
- Parsing is intentionally best-effort: if the CLI output format changes or the expected
  `type=="result"` event is missing, `data` will be `None` and `stdout` will fall back to raw
  stdout text (trimmed).
"""

from __future__ import annotations

import io
import json
import selectors
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import uuid

from .tmux import PaneContext, send_command, shell_argv, wait_for


@dataclass(frozen=True)
class ClaudeJSONResult:
    exit_code: int
    stdout: str
    stderr: str
    data: Any | None


def _run_claude_json_via_tmux(
    *,
    executable: str,
    model: str,
    prompt: str,
    schema_str: str,
    cwd: Path,
    run_cwd: Path,
    pane: PaneContext,
) -> ClaudeJSONResult:
    runs_dir = (cwd / ".satrap" / "runs").resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    wait_key = f"satrap-json-{run_id}"
    code_file = runs_dir / f"json-{run_id}.code"
    out_file = runs_dir / f"json-{run_id}.stdout"
    err_file = runs_dir / f"json-{run_id}.stderr"

    cmd = [
        executable,
        "--model",
        model,
        "-p",
        prompt,
        "--json-schema",
        schema_str,
        "--output-format",
        "json",
    ]
    cmd_str = " ".join(shlex.quote(a) for a in cmd)
    script = "\n".join(
        [
            f"cd {shlex.quote(str(run_cwd))}",
            f"out_file={shlex.quote(str(out_file))}",
            f"err_file={shlex.quote(str(err_file))}",
            f"code_file={shlex.quote(str(code_file))}",
            f"wait_key={shlex.quote(wait_key)}",
            "set +e",
            f"{cmd_str} >\"$out_file\" 2>\"$err_file\"",
            "code=$?",
            "printf '%s\n' \"$code\" > \"$code_file\"",
            "tmux wait-for -S \"$wait_key\"",
        ]
    )
    send_command(pane_id=pane.pane_id, argv=shell_argv(script=script))
    wait_for(key=wait_key)

    stdout = out_file.read_text(encoding="utf-8").strip() if out_file.exists() else ""
    stderr = err_file.read_text(encoding="utf-8").strip() if err_file.exists() else ""
    try:
        code = int(code_file.read_text(encoding="utf-8").strip()) if code_file.exists() else 1
    except Exception:
        code = 1

    if stderr:
        sys.stderr.write(stderr + "\n")
        sys.stderr.flush()

    extracted = _extract_structured_or_printed_result(stdout)
    normalized = extracted.print_text.strip()
    if normalized:
        sys.stdout.write(normalized + "\n")
        sys.stdout.flush()

    return ClaudeJSONResult(exit_code=code, stdout=normalized, stderr=stderr, data=extracted.data)


def run_claude_json_from_files(
    *,
    executable: str = "claude",
    model: str,
    prompt_file: Path,
    schema_file: Path,
    cwd: Path,
    pane: PaneContext | None = None,
    run_cwd: Path | None = None,
) -> ClaudeJSONResult:
    """Run Claude with `--json-schema "$(jq -c . schema_file)"` and parse JSON from stdout.

    Important: we intentionally avoid passing schemas inline. The schema is always loaded from a file
    and compacted with `jq -c .` (matching the documented CLI usage).

    Note: Claude Code's structured output is most reliable when using `--output-format json`.
    We run with that and extract the final `result` payload before parsing JSON.
    """
    prompt = prompt_file.read_text(encoding="utf-8")
    schema_str = _jq_compact_json(schema_file, cwd=cwd)

    if pane is not None:
        return _run_claude_json_via_tmux(
            executable=executable,
            model=model,
            prompt=prompt,
            schema_str=schema_str,
            cwd=cwd,
            run_cwd=(run_cwd or cwd),
            pane=pane,
        )

    # Ordering matters with Claude Code CLI: keep `--output-format json` at the end
    # (matching the documented `-p "<prompt>" ...` usage).
    cmd = [executable, "--model", model, "-p", prompt, "--json-schema", schema_str, "--output-format", "json"]
    p = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    assert p.stdout is not None
    assert p.stderr is not None

    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    sel.register(p.stderr, selectors.EVENT_READ)

    out_chunks: list[str] = []
    err_chunks: list[str] = []

    # Avoid streaming stdout: `--output-format json` can emit a huge JSON event envelope.
    # We print a normalized payload after extracting the final result.
    while sel.get_map():
        for key, _ in sel.select(timeout=0.1):
            stream = key.fileobj
            if not hasattr(stream, "readline"):
                sel.unregister(stream)
                continue
            line = stream.readline()
            if line == "":
                sel.unregister(stream)
                continue

            if stream is p.stdout:
                out_chunks.append(line)
            else:
                err_chunks.append(line)
                sys.stderr.write(line)
                sys.stderr.flush()

        if p.poll() is not None and not sel.get_map():
            break

    code = p.wait()
    raw_stdout = "".join(out_chunks).strip()
    stderr = "".join(err_chunks).strip()

    extracted = _extract_structured_or_printed_result(raw_stdout)
    stdout = extracted.print_text.strip()
    if stdout:
        sys.stdout.write(stdout + "\n")
        sys.stdout.flush()
    data: Any | None = extracted.data

    return ClaudeJSONResult(exit_code=code, stdout=stdout, stderr=stderr, data=data)


def _jq_compact_json(schema_file: Path, *, cwd: Path) -> str:
    try:
        out = subprocess.check_output(["jq", "-c", ".", str(schema_file)], cwd=str(cwd), text=True)
    except FileNotFoundError as e:
        raise RuntimeError("jq is required to compact JSON schemas for claude --json-schema.") from e
    return out.strip()


def _best_effort_parse_json(text: str) -> Any | None:
    """Parse the first JSON value in `text`."""
    # Fast path: strict parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Recovery: find the first JSON object/array and parse it.
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None

    decoder = json.JSONDecoder()
    try:
        val, _end = decoder.raw_decode(text[start:])
        return val
    except json.JSONDecodeError:
        return None


@dataclass(frozen=True)
class _ClaudeExtracted:
    # Schema-validated payload (present when `--json-schema` is used).
    data: Any | None
    # Human/debug output to print.
    print_text: str


def _parse_envelope(raw_stdout: str) -> Any | None:
    """Parse `claude --output-format json` stdout.

    Claude Code typically emits a single JSON array. If that changes to JSONL,
    we fall back to parsing line-by-line.
    """
    if not raw_stdout.strip():
        return None

    env = _best_effort_parse_json(raw_stdout)
    if env is not None:
        return env

    items: list[Any] = []
    for ln in raw_stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            items.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return items if items else None


def _extract_structured_or_printed_result(raw_stdout: str) -> _ClaudeExtracted:
    """Extract structured output (preferred) or printed result text.

    With `--json-schema`, Claude Code may return a `structured_output` payload on the final
    `type=="result"` event. The printed `result` string is often non-JSON and should not be
    used as the structured output source of truth.
    """
    env = _parse_envelope(raw_stdout)
    if env is None:
        return _ClaudeExtracted(data=None, print_text=raw_stdout.strip())

    result_ev: dict | None = None
    if isinstance(env, dict):
        result_ev = env
    elif isinstance(env, list):
        for it in reversed(env):
            if isinstance(it, dict) and it.get("type") == "result":
                result_ev = it
                break

    if result_ev is None:
        return _ClaudeExtracted(data=None, print_text=raw_stdout.strip())

    structured = result_ev.get("structured_output")
    if structured is not None:
        # Prefer printing normalized JSON for observability.
        try:
            return _ClaudeExtracted(data=structured, print_text=json.dumps(structured, indent=2, sort_keys=True))
        except TypeError:
            return _ClaudeExtracted(data=structured, print_text=str(structured))

    res = result_ev.get("result")
    if isinstance(res, str) and res.strip():
        return _ClaudeExtracted(data=_best_effort_parse_json(res), print_text=res.strip())

    return _ClaudeExtracted(data=None, print_text=raw_stdout.strip())
