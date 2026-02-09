from __future__ import annotations

from pathlib import Path

import pytest

import satrap.cli as cli


def test_build_parser_flags_and_defaults() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["task text", "--no-worktree-panes", "--keep-pane", "--max-parallel", "3"])

    assert args.task == "task text"
    assert args.no_worktree_panes is True
    assert args.keep_pane is True
    assert args.kill_pane is False
    assert args.no_tmux is False
    assert args.max_parallel == 3
    assert args.worker_tiers == "ccss-haiku,ccss-sonnet,ccss-opus,ccss-default"


def test_build_parser_rejects_keep_and_kill_pane_together() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "--keep-pane", "--kill-pane"])


def test_read_task_input_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_read_text(self: Path, encoding: str = "utf-8") -> str:
        assert encoding == "utf-8"
        assert str(self) == "/dev/stdin"
        return "stdin task"

    monkeypatch.setattr(cli.Path, "read_text", fake_read_text, raising=False)

    assert cli._read_task_input("-") == "stdin task"


def test_read_task_input_from_existing_file(tmp_path: Path) -> None:
    task_file = tmp_path / "task.txt"
    task_file.write_text("from file", encoding="utf-8")

    assert cli._read_task_input(str(task_file)) == "from file"


def test_read_task_input_treats_non_file_as_literal(tmp_path: Path) -> None:
    literal = str(tmp_path / "not-a-file")
    assert cli._read_task_input(literal) == literal


def test_main_dry_run_wires_stub_backends_and_clamps_parallel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SATRAP_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "in_tmux", lambda: False)

    captured: dict[str, object] = {}

    class FakeOrchestrator:
        def __init__(self, cfg: object) -> None:
            captured["cfg"] = cfg

        def run(self, *, task_text: str, start_step: str | None, reset_todo: bool) -> None:
            captured["run"] = {
                "task_text": task_text,
                "start_step": start_step,
                "reset_todo": reset_todo,
            }

    monkeypatch.setattr(cli, "SatrapOrchestrator", FakeOrchestrator)

    rc = cli.main(
        [
            "literal task",
            "--dry-run",
            "--step",
            "2.1",
            "--reset-todo",
            "--max-parallel",
            "0",
            "--worker-tiers",
            " low , , high ",
            "--todo-json",
            "custom/todo.json",
            "--schema-json",
            "schemas/todo.json",
        ]
    )

    assert rc == 0
    cfg = captured["cfg"]
    assert isinstance(cfg.git, cli.DryRunGitClient)
    assert isinstance(cfg.planner_backend, cli.StubPlannerBackend)
    assert isinstance(cfg.worker_backend, cli.StubWorkerBackend)
    assert isinstance(cfg.verifier_backend, cli.StubVerifierBackend)
    assert cfg.max_parallel == 1
    assert cfg.model_tiers == [["low"], ["high"]]
    assert cfg.control_root == tmp_path.resolve()
    assert cfg.todo_json_path == (tmp_path / "custom/todo.json").resolve()
    assert cfg.todo_schema_path == (tmp_path / "schemas/todo.json").resolve()

    assert captured["run"] == {
        "task_text": "literal task",
        "start_step": "2.1",
        "reset_todo": True,
    }


def test_main_non_dry_run_wires_external_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SATRAP_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("SATRAP_TMUX_WINDOW", "work-window")
    monkeypatch.setattr(cli, "in_tmux", lambda: False)

    captured: dict[str, object] = {}

    class FakePlanner:
        def __init__(self, *, cmd: str | None) -> None:
            self.cmd = cmd

    class FakeWorker:
        def __init__(
            self,
            *,
            cmd: str | None,
            control_root: Path,
            use_tmux_panes: bool,
            tmux_window_name: str,
        ) -> None:
            self.cmd = cmd
            self.control_root = control_root
            self.use_tmux_panes = use_tmux_panes
            self.tmux_window_name = tmux_window_name

    class FakeVerifier:
        def __init__(self, *, cmd: str | None, schema_file: Path) -> None:
            self.cmd = cmd
            self.schema_file = schema_file

    class FakeGit:
        def __init__(self, *, control_root: Path) -> None:
            self.control_root = control_root

    class FakeOrchestrator:
        def __init__(self, cfg: object) -> None:
            captured["cfg"] = cfg

        def run(self, *, task_text: str, start_step: str | None, reset_todo: bool) -> None:
            captured["run"] = (task_text, start_step, reset_todo)

    monkeypatch.setattr(cli, "ExternalPlannerBackend", FakePlanner)
    monkeypatch.setattr(cli, "ExternalWorkerBackend", FakeWorker)
    monkeypatch.setattr(cli, "ExternalVerifierBackend", FakeVerifier)
    monkeypatch.setattr(cli, "GitClient", FakeGit)
    monkeypatch.setattr(cli, "SatrapOrchestrator", FakeOrchestrator)

    rc = cli.main(
        [
            "task from cli",
            "--planner-cmd",
            "plan-cmd",
            "--worker-cmd",
            "work-cmd",
            "--verifier-cmd",
            "verify-cmd",
            "--verifier-schema-json",
            "schemas/verifier.json",
            "--no-worktree-panes",
            "--max-parallel",
            "5",
            "--worker-tiers",
            "tier-a,tier-b",
        ]
    )

    assert rc == 0
    cfg = captured["cfg"]
    assert isinstance(cfg.git, FakeGit)
    assert isinstance(cfg.planner_backend, FakePlanner)
    assert isinstance(cfg.worker_backend, FakeWorker)
    assert isinstance(cfg.verifier_backend, FakeVerifier)

    assert cfg.planner_backend.cmd == "plan-cmd"
    assert cfg.worker_backend.cmd == "work-cmd"
    assert cfg.worker_backend.control_root == tmp_path.resolve()
    assert cfg.worker_backend.use_tmux_panes is False
    assert cfg.worker_backend.tmux_window_name == "work-window"
    assert cfg.verifier_backend.cmd == "verify-cmd"
    assert cfg.verifier_backend.schema_file == (tmp_path / "schemas/verifier.json").resolve()

    assert cfg.max_parallel == 5
    assert cfg.model_tiers == [["tier-a"], ["tier-b"]]
    assert captured["run"] == ("task from cli", None, False)


def test_main_tmux_autospawn_short_circuits_orchestrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SATRAP_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("SATRAP_TMUX_WINDOW", "satrap-window")
    monkeypatch.setattr(cli, "in_tmux", lambda: True)

    calls: dict[str, object] = {}

    def fake_ensure_window(*, window_name: str, cwd: Path) -> str:
        calls["ensure_window"] = (window_name, cwd)
        return "mysession:satrap-window"

    def fake_spawn_pane(**kwargs: object) -> str:
        calls["spawn_pane"] = kwargs
        return "%1"

    class GuardOrchestrator:
        def __init__(self, cfg: object) -> None:  # pragma: no cover - defensive
            raise AssertionError("orchestrator should not be constructed when tmux autospawn short-circuits")

    monkeypatch.setattr(cli, "ensure_window", fake_ensure_window)
    monkeypatch.setattr(cli, "spawn_pane", fake_spawn_pane)
    monkeypatch.setattr(cli, "SatrapOrchestrator", GuardOrchestrator)

    rc = cli.main(["task", "--kill-pane"])

    assert rc == 0
    assert calls["ensure_window"] == ("satrap-window", tmp_path.resolve())
    spawn_kwargs = calls["spawn_pane"]
    assert spawn_kwargs["window_target"] == "mysession:satrap-window"
    assert spawn_kwargs["cwd"] == tmp_path.resolve()
    assert spawn_kwargs["title"] == "satrap"
    assert spawn_kwargs["keep_pane"] is False
    assert spawn_kwargs["select"] is True
    assert spawn_kwargs["env"] == {"SATRAP_CONTROL_ROOT": str(tmp_path.resolve())}
    assert spawn_kwargs["argv"][-1] == "--no-tmux"


def test_spawn_pane_autoclose_script_waits_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import satrap.tmux as tmux

    captured: dict[str, object] = {}

    def fake_check_output(argv: list[str], text: bool = True) -> str:
        captured["argv"] = argv
        return "%99\n"

    monkeypatch.setattr(tmux.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(tmux.subprocess, "run", lambda *args, **kwargs: None)

    tmux.spawn_pane(
        window_target="sess:satrap",
        argv=["python3", "-m", "satrap", "--no-tmux"],
        cwd=tmp_path,
        title="satrap",
        keep_pane=False,
        select=False,
    )

    split_argv = captured["argv"]
    assert isinstance(split_argv, list)
    script = split_argv[-1]
    assert "sleep 5; tmux kill-pane -t $TMUX_PANE" in script


def test_main_tmux_autospawn_uses_same_window_even_when_already_in_target_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SATRAP_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("SATRAP_TMUX_WINDOW", "satrap-window")
    monkeypatch.setattr(cli, "in_tmux", lambda: True)
    monkeypatch.setattr(cli, "current_window_name", lambda: "satrap-window")

    calls: dict[str, object] = {}

    def fake_ensure_window(*, window_name: str, cwd: Path) -> str:
        calls["ensure_window"] = (window_name, cwd)
        return "mysession:satrap-window"

    def fake_spawn_pane(**kwargs: object) -> str:
        calls["spawn_pane"] = kwargs
        return "%1"

    class GuardOrchestrator:
        def __init__(self, cfg: object) -> None:  # pragma: no cover - defensive
            raise AssertionError("orchestrator should not be constructed when tmux autospawn short-circuits")

    monkeypatch.setattr(cli, "ensure_window", fake_ensure_window)
    monkeypatch.setattr(cli, "spawn_pane", fake_spawn_pane)
    monkeypatch.setattr(cli, "SatrapOrchestrator", GuardOrchestrator)

    rc = cli.main(["task", "--kill-pane"])

    assert rc == 0
    assert calls["ensure_window"] == ("satrap-window", tmp_path.resolve())


def test_main_tmux_autospawn_defaults_to_keep_when_no_kill_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SATRAP_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("SATRAP_TMUX_WINDOW", "satrap-window")
    monkeypatch.setattr(cli, "in_tmux", lambda: True)
    monkeypatch.setattr(cli, "current_window_name", lambda: "other-window")

    calls: dict[str, object] = {}

    def fake_ensure_window(*, window_name: str, cwd: Path) -> str:
        calls["ensure_window"] = (window_name, cwd)
        return "mysession:satrap-window"

    def fake_spawn_pane(**kwargs: object) -> str:
        calls["spawn_pane"] = kwargs
        return "%1"

    class GuardOrchestrator:
        def __init__(self, cfg: object) -> None:  # pragma: no cover - defensive
            raise AssertionError("orchestrator should not be constructed when tmux autospawn short-circuits")

    monkeypatch.setattr(cli, "ensure_window", fake_ensure_window)
    monkeypatch.setattr(cli, "spawn_pane", fake_spawn_pane)
    monkeypatch.setattr(cli, "SatrapOrchestrator", GuardOrchestrator)

    rc = cli.main(["task"])

    assert rc == 0
    assert calls["spawn_pane"]["keep_pane"] is True
    assert calls["spawn_pane"]["select"] is True


def test_main_tmux_autospawn_keep_pane_flag_preserves_pane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SATRAP_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("SATRAP_TMUX_WINDOW", "satrap-window")
    monkeypatch.setattr(cli, "in_tmux", lambda: True)
    monkeypatch.setattr(cli, "current_window_name", lambda: "other-window")

    calls: dict[str, object] = {}

    def fake_ensure_window(*, window_name: str, cwd: Path) -> str:
        calls["ensure_window"] = (window_name, cwd)
        return "mysession:satrap-window"

    def fake_spawn_pane(**kwargs: object) -> str:
        calls["spawn_pane"] = kwargs
        return "%1"

    class GuardOrchestrator:
        def __init__(self, cfg: object) -> None:  # pragma: no cover - defensive
            raise AssertionError("orchestrator should not be constructed when tmux autospawn short-circuits")

    monkeypatch.setattr(cli, "ensure_window", fake_ensure_window)
    monkeypatch.setattr(cli, "spawn_pane", fake_spawn_pane)
    monkeypatch.setattr(cli, "SatrapOrchestrator", GuardOrchestrator)

    rc = cli.main(["task", "--keep-pane"])

    assert rc == 0
    assert calls["spawn_pane"]["keep_pane"] is True
    assert calls["spawn_pane"]["select"] is True


def test_main_in_tmux_with_no_tmux_flag_runs_inline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SATRAP_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "in_tmux", lambda: True)

    called: dict[str, bool] = {"run": False}

    def fail_spawn_pane(**kwargs: object) -> str:  # pragma: no cover - defensive
        raise AssertionError("spawn_pane should not run with --no-tmux")

    class FakeOrchestrator:
        def __init__(self, cfg: object) -> None:
            self.cfg = cfg

        def run(self, *, task_text: str, start_step: str | None, reset_todo: bool) -> None:
            called["run"] = True

    monkeypatch.setattr(cli, "spawn_pane", fail_spawn_pane)
    monkeypatch.setattr(cli, "SatrapOrchestrator", FakeOrchestrator)

    rc = cli.main(["task", "--dry-run", "--no-tmux"])

    assert rc == 0
    assert called["run"] is True


def test_read_task_input_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert cli._read_task_input(str(empty)) == ""


def test_read_task_input_nonexistent_file_literal() -> None:
    path = "/tmp/nonexistent_satrap_test_file_xyz"
    assert cli._read_task_input(path) == path


def test_build_parser_defaults() -> None:
    args = cli.build_parser().parse_args(["task"])
    assert args.task == "task"
    assert args.step is None
    assert args.todo_json == ".satrap/todo.json"
    assert args.reset_todo is False
    assert args.schema_json == "todo-schema.json"
    assert args.dry_run is False
    assert args.planner_cmd is None
    assert args.verifier_cmd is None
    assert args.verifier_schema_json == "verifier-schema.json"
    assert args.worker_tiers == "ccss-haiku,ccss-sonnet,ccss-opus,ccss-default"
    assert args.worker_cmd == "claude"
    assert args.no_worktree_panes is False
    assert args.max_parallel == 1
    assert args.no_tmux is False
    assert args.keep_pane is False
    assert args.kill_pane is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("single", [["single"]]),
        ("a,,b", [["a"], ["b"]]),
        (",,,", []),
    ],
)
def test_worker_tiers_parsing_edge_cases(raw: str, expected: list[list[str]]) -> None:
    tiers = [s.strip() for s in raw.split(",") if s.strip()]
    tier_cmds = [[t] for t in tiers]
    assert tier_cmds == expected


def test_control_root_from_cwd_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SATRAP_CONTROL_ROOT", raising=False)
    monkeypatch.setattr(cli, "in_tmux", lambda: False)

    captured: dict[str, object] = {}

    class FakeOrchestrator:
        def __init__(self, cfg: object) -> None:
            captured["cfg"] = cfg

        def run(self, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(cli, "SatrapOrchestrator", FakeOrchestrator)
    cli.main(["task", "--dry-run"])
    assert captured["cfg"].control_root == Path.cwd().resolve()
