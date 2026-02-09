from __future__ import annotations

import selectors
from pathlib import Path
from types import SimpleNamespace

import pytest

import satrap.agents as agents
from satrap.todo import TodoItem


def _result(*, exit_code: int = 0, stderr: str = "", data: object = None) -> SimpleNamespace:
    return SimpleNamespace(exit_code=exit_code, stderr=stderr, data=data)


def test_parse_todo_item_spec_trims_and_filters_blank_entries() -> None:
    out = agents._parse_todo_item_spec(
        {
            "number": " 1.2 ",
            "text": "  Ship feature  ",
            "details": "  with docs ",
            "depends_on": ["1", " ", "2"],
            "done_when": ["works", "   "],
        }
    )

    assert out.number == "1.2"
    assert out.text == "Ship feature"
    assert out.details == "with docs"
    assert out.depends_on == ["1", "2"]
    assert out.done_when == ["works"]


def test_parse_todo_item_spec_normalizes_blank_optional_fields_to_none() -> None:
    out = agents._parse_todo_item_spec(
        {
            "number": "1",
            "text": "task",
            "details": "   ",
        }
    )

    assert out.details is None
    assert out.depends_on is None
    assert out.done_when is None


@pytest.mark.parametrize(
    "payload,error_fragment",
    [
        ({"text": "x"}, "missing required string: number"),
        ({"number": "1", "text": "   "}, "missing required string: text"),
        ({"number": "1", "text": "ok", "depends_on": ["1", 2]}, "invalid depends_on"),
        ({"number": "1", "text": "ok", "done_when": "bad"}, "invalid done_when"),
    ],
)
def test_parse_todo_item_spec_validation_errors(payload: dict[str, object], error_fragment: str) -> None:
    with pytest.raises(ValueError, match=error_fragment):
        agents._parse_todo_item_spec(payload)


def test_external_planner_raises_on_command_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        agents,
        "run_claude_json_from_files",
        lambda **kwargs: _result(exit_code=9, stderr="planner boom", data=None),
    )

    backend = agents.ExternalPlannerBackend(cmd="planner")
    with pytest.raises(RuntimeError, match="exit code 9"):
        backend.plan(prompt_file=tmp_path / "prompt.md", schema_file=tmp_path / "schema.json", step_number=None)


def test_external_planner_validates_json_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend = agents.ExternalPlannerBackend(cmd="planner")

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data=[]))
    with pytest.raises(ValueError, match="non-object JSON"):
        backend.plan(prompt_file=tmp_path / "prompt.md", schema_file=tmp_path / "schema.json", step_number=None)

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data={"title": "x"}))
    with pytest.raises(ValueError, match="missing required field: items"):
        backend.plan(prompt_file=tmp_path / "prompt.md", schema_file=tmp_path / "schema.json", step_number=None)

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data={"items": []}))
    with pytest.raises(ValueError, match="must contain at least 1 item"):
        backend.plan(prompt_file=tmp_path / "prompt.md", schema_file=tmp_path / "schema.json", step_number=None)

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data={"items": ["not-an-object"]}))
    with pytest.raises(ValueError, match="item 0 is not an object"):
        backend.plan(prompt_file=tmp_path / "prompt.md", schema_file=tmp_path / "schema.json", step_number=None)


def test_external_planner_success_trims_title_and_items(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(**kwargs: object) -> SimpleNamespace:
        assert kwargs["executable"] == "planner-cmd"
        assert kwargs["model"] == "ccss-sonnet"
        return _result(
            data={
                "title": "  Plan title  ",
                "items": [
                    {
                        "number": " 1 ",
                        "text": "  Build  ",
                        "depends_on": [" ", "0"],
                        "done_when": ["done", "  "],
                    }
                ],
            }
        )

    monkeypatch.setattr(agents, "run_claude_json_from_files", fake_run)

    backend = agents.ExternalPlannerBackend(cmd="planner-cmd")
    out = backend.plan(prompt_file=tmp_path / "prompt.md", schema_file=tmp_path / "schema.json", step_number="1")

    assert out.title == "Plan title"
    assert len(out.items) == 1
    assert out.items[0].number == "1"
    assert out.items[0].text == "Build"
    assert out.items[0].depends_on == ["0"]
    assert out.items[0].done_when == ["done"]


def _todo_step() -> TodoItem:
    return TodoItem(number="1", text="step")


def test_external_verifier_raises_on_command_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        agents,
        "run_claude_json_from_files",
        lambda **kwargs: _result(exit_code=7, stderr="verify boom", data=None),
    )

    backend = agents.ExternalVerifierBackend(cmd="verify", schema_file=tmp_path / "v.json")
    with pytest.raises(RuntimeError, match="exit code 7"):
        backend.verify(prompt_file=tmp_path / "prompt.md", diff="d", commits=["c"], step=_todo_step())


def test_external_verifier_validation_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend = agents.ExternalVerifierBackend(cmd="verify", schema_file=tmp_path / "v.json")

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data=[]))
    with pytest.raises(ValueError, match="non-object JSON"):
        backend.verify(prompt_file=tmp_path / "prompt.md", diff="", commits=[], step=_todo_step())

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data={"passed": "yes"}))
    with pytest.raises(ValueError, match="required boolean field: passed"):
        backend.verify(prompt_file=tmp_path / "prompt.md", diff="", commits=[], step=_todo_step())

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data={"passed": True, "note": 3}))
    with pytest.raises(ValueError, match="field 'note' must be a string"):
        backend.verify(prompt_file=tmp_path / "prompt.md", diff="", commits=[], step=_todo_step())


def test_external_verifier_normalizes_notes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend = agents.ExternalVerifierBackend(cmd="verify", schema_file=tmp_path / "v.json")

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data={"passed": False, "note": "   "}))
    out = backend.verify(prompt_file=tmp_path / "prompt.md", diff="", commits=[], step=_todo_step())
    assert out.passed is False
    assert out.note == "Rejected with no note."

    monkeypatch.setattr(agents, "run_claude_json_from_files", lambda **kwargs: _result(data={"passed": True, "note": "  all good  "}))
    out2 = backend.verify(prompt_file=tmp_path / "prompt.md", diff="", commits=[], step=_todo_step())
    assert out2.passed is True
    assert out2.note == "all good"


def test_external_worker_spawn_proc_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prompt_file = tmp_path / "1-worker.md"
    prompt_file.write_text("do work", encoding="utf-8")

    captured: dict[str, object] = {}

    class FakeProc:
        stdout = object()
        stderr = object()

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProc:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(agents, "in_tmux", lambda: False)
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    backend = agents.ExternalWorkerBackend(cmd="worker-cmd", control_root=tmp_path)
    run = backend.spawn(tier=["tier-model"], prompt_file=prompt_file, cwd=tmp_path)

    assert run.opaque["kind"] == "proc"
    assert captured["argv"] == [
        "worker-cmd",
        "--model",
        "tier-model",
        "-p",
        "do work",
        "--dangerously-skip-permissions",
    ]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["text"] is True


def test_external_worker_spawn_uses_default_model_when_tier_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "1-worker.md"
    prompt_file.write_text("do work", encoding="utf-8")

    captured: dict[str, object] = {}

    class FakeProc:
        stdout = object()
        stderr = object()

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProc:
        captured["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(agents, "in_tmux", lambda: False)
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    backend = agents.ExternalWorkerBackend(cmd="worker-cmd", control_root=tmp_path)
    backend.spawn(tier=[], prompt_file=prompt_file, cwd=tmp_path)

    assert captured["argv"][2] == "ccss-sonnet"


def test_external_worker_spawn_tmux_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prompt_file = tmp_path / "1-2-worker.md"
    prompt_file.write_text("run step", encoding="utf-8")

    calls: dict[str, object] = {}

    def fake_shell_argv(*, script: str) -> list[str]:
        calls["script"] = script
        return ["shell", "-lc", script]

    monkeypatch.setattr(agents, "in_tmux", lambda: True)
    monkeypatch.setattr(agents, "ensure_window", lambda **kwargs: "session:window")
    monkeypatch.setattr(agents, "shell_argv", fake_shell_argv)
    def fake_spawn_pane_remain_on_exit(**kwargs: object) -> str:
        calls["spawn"] = kwargs
        return "%42"

    monkeypatch.setattr(agents, "spawn_pane_remain_on_exit", fake_spawn_pane_remain_on_exit)
    monkeypatch.setattr("uuid.uuid4", lambda: SimpleNamespace(hex="abc123"))

    backend = agents.ExternalWorkerBackend(cmd="worker", control_root=tmp_path, use_tmux_panes=True, tmux_window_name="w")
    run = backend.spawn(tier=["model-x"], prompt_file=prompt_file, cwd=tmp_path)

    assert run.opaque["kind"] == "tmux"
    assert run.opaque["pane_id"] == "%42"
    assert run.opaque["wait_key"] == "satrap-worker-abc123"
    assert run.opaque["exit_file"].endswith("worker-abc123.exit")
    assert "trap 'code=$?; echo $code > \"$exit_file\"; tmux wait-for -S \"$wait_key\"' EXIT" in calls["script"]
    assert "worker --model model-x -p 'run step' --dangerously-skip-permissions" in calls["script"]

    spawn_kwargs = calls["spawn"]
    assert spawn_kwargs["window_target"] == "session:window"
    assert spawn_kwargs["cwd"] == tmp_path
    assert spawn_kwargs["title"] == "1.2 model-x"


def test_external_worker_spawn_ignores_tmux_when_panes_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "1-worker.md"
    prompt_file.write_text("x", encoding="utf-8")

    class FakeProc:
        stdout = object()
        stderr = object()

    monkeypatch.setattr(agents, "in_tmux", lambda: True)
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: FakeProc())

    backend = agents.ExternalWorkerBackend(cmd="worker", control_root=tmp_path, use_tmux_panes=False)
    run = backend.spawn(tier=["m"], prompt_file=prompt_file, cwd=tmp_path)

    assert run.opaque["kind"] == "proc"


def test_external_worker_watch_tmux_success_reads_exit_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exit_file = tmp_path / "worker.exit"
    exit_file.write_text("13\n", encoding="utf-8")

    seen: dict[str, str] = {}
    monkeypatch.setattr(agents, "wait_for", lambda *, key: seen.setdefault("key", key))

    backend = agents.ExternalWorkerBackend(control_root=tmp_path)
    run = agents.WorkerRun(
        tier=["m"],
        prompt_file=tmp_path / "p.md",
        cwd=tmp_path,
        opaque={"kind": "tmux", "wait_key": "wk", "exit_file": str(exit_file)},
    )

    out = backend.watch(run)

    assert seen["key"] == "wk"
    assert out.exit_code == 13


def test_external_worker_watch_tmux_missing_or_bad_exit_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agents, "wait_for", lambda *, key: None)

    backend = agents.ExternalWorkerBackend(control_root=tmp_path)

    missing_wait_key = agents.WorkerRun(
        tier=["m"],
        prompt_file=tmp_path / "p.md",
        cwd=tmp_path,
        opaque={"kind": "tmux", "exit_file": str(tmp_path / "x.exit")},
    )
    with pytest.raises(RuntimeError, match="wait_key"):
        backend.watch(missing_wait_key)

    missing_exit_file = agents.WorkerRun(
        tier=["m"],
        prompt_file=tmp_path / "p.md",
        cwd=tmp_path,
        opaque={"kind": "tmux", "wait_key": "wk"},
    )
    with pytest.raises(RuntimeError, match="exit_file"):
        backend.watch(missing_exit_file)

    unreadable_exit = agents.WorkerRun(
        tier=["m"],
        prompt_file=tmp_path / "p.md",
        cwd=tmp_path,
        opaque={"kind": "tmux", "wait_key": "wk", "exit_file": str(tmp_path / "does-not-exist.exit")},
    )
    out = backend.watch(unreadable_exit)
    assert out.exit_code == 1


def test_external_worker_watch_rejects_invalid_opaque(tmp_path: Path) -> None:
    backend = agents.ExternalWorkerBackend(control_root=tmp_path)

    with pytest.raises(RuntimeError, match="missing/invalid"):
        backend.watch(agents.WorkerRun(tier=["m"], prompt_file=tmp_path / "p", cwd=tmp_path, opaque=None))

    with pytest.raises(RuntimeError, match="Unknown worker run kind"):
        backend.watch(agents.WorkerRun(tier=["m"], prompt_file=tmp_path / "p", cwd=tmp_path, opaque={"kind": "what"}))

    with pytest.raises(RuntimeError, match=r"missing \(expected subprocess handle\)"):
        backend.watch(agents.WorkerRun(tier=["m"], prompt_file=tmp_path / "p", cwd=tmp_path, opaque={"kind": "proc"}))


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        return ""


class _FakeSelector:
    def __init__(self) -> None:
        self._map: dict[object, object] = {}

    def register(self, fileobj: object, event: object) -> None:
        self._map[fileobj] = event

    def unregister(self, fileobj: object) -> None:
        self._map.pop(fileobj, None)

    def select(self, timeout: float = 0.1) -> list[tuple[SimpleNamespace, object]]:
        return [(SimpleNamespace(fileobj=f), None) for f in list(self._map.keys())]

    def get_map(self) -> dict[object, object]:
        return self._map


def test_external_worker_watch_proc_streams_output_and_returns_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeProc:
        def __init__(self) -> None:
            self.stdout = _FakeStream(["out line\n", ""])
            self.stderr = _FakeStream(["err line\n", ""])

        def poll(self) -> None:
            return None

        def wait(self) -> int:
            return 4

    monkeypatch.setattr(selectors, "DefaultSelector", _FakeSelector)

    backend = agents.ExternalWorkerBackend(control_root=tmp_path)
    run = agents.WorkerRun(
        tier=["m"],
        prompt_file=tmp_path / "p.md",
        cwd=tmp_path,
        opaque={"kind": "proc", "p": FakeProc()},
    )

    out = backend.watch(run)
    captured = capsys.readouterr()

    assert out.exit_code == 4
    assert "out line" in captured.out
    assert "err line" in captured.err


def test_external_worker_watch_proc_requires_pipes(tmp_path: Path) -> None:
    class FakeProcNoPipes:
        stdout = None
        stderr = None

        def poll(self) -> None:
            return None

        def wait(self) -> int:
            return 0

    backend = agents.ExternalWorkerBackend(control_root=tmp_path)
    run = agents.WorkerRun(
        tier=["m"],
        prompt_file=tmp_path / "p.md",
        cwd=tmp_path,
        opaque={"kind": "proc", "p": FakeProcNoPipes()},
    )

    with pytest.raises(RuntimeError, match="missing stdout/stderr"):
        backend.watch(run)


def test_stub_backends_basic_behavior(tmp_path: Path) -> None:
    planner = agents.StubPlannerBackend()
    root = planner.plan(prompt_file=tmp_path / "p.md", schema_file=tmp_path / "s.json", step_number=None)
    step = planner.plan(prompt_file=tmp_path / "p.md", schema_file=tmp_path / "s.json", step_number="2.3")

    assert root.title == "(stub) Plan"
    assert len(root.items) == 2
    assert step.title is None
    assert step.items[0].number == "2.3"

    worker = agents.StubWorkerBackend()
    run = worker.spawn(tier=["m"], prompt_file=tmp_path / "p.md", cwd=tmp_path)
    outcome = worker.watch(run)
    assert outcome.exit_code == 0

    verifier = agents.StubVerifierBackend()
    verdict = verifier.verify(prompt_file=tmp_path / "v.md", diff="", commits=[], step=_todo_step())
    assert verdict.passed is True
    assert verdict.note is None
