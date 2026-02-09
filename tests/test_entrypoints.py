from __future__ import annotations

import runpy

import pytest


def test_init_exports_version() -> None:
    import satrap

    assert satrap.__all__ == ["__version__"]
    assert isinstance(satrap.__version__, str)
    assert satrap.__version__


def test_main_module_import_exposes_cli_main() -> None:
    import satrap.__main__ as main_mod
    import satrap.cli as cli

    assert main_mod.main is cli.main


def test_main_module_exec_uses_cli_return_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import satrap.cli as cli

    monkeypatch.setattr(cli, "main", lambda: 17)

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("satrap.__main__", run_name="__main__")

    assert exc.value.code == 17
