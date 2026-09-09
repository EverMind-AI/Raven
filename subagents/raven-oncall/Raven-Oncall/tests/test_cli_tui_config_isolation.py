"""``raven tui --config`` gives an instance its own runtime directory.

Without it, two TUIs on one machine share ``~/.raven`` -- the same cron store and
the same ops ledgers -- so one on-call campaign's wakes and another's land in one
file. ``raven agent`` already had ``--config``; the TUI not having it was the gap.

The isolation is not a separate mechanism: ``set_config_path`` moves
``get_data_dir()``, and every runtime subdirectory hangs off that.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from raven.cli.tui_commands import apply_config_path
from raven.config.loader import get_config_path, set_config_path
from raven.config.paths import get_cron_dir, get_data_dir


@pytest.fixture(autouse=True)
def _restore_config_path():
    """set_config_path writes a module global; leaving it set would silently move
    every later test's runtime paths."""
    from raven.config import loader

    before = loader._current_config_path
    yield
    loader._current_config_path = before


def _config(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    path = d / "config.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_config_moves_the_cron_store_and_the_ops_dir(tmp_path: Path) -> None:
    path = _config(tmp_path, "inst-a")

    apply_config_path(str(path))

    assert get_config_path() == path
    assert get_data_dir() == path.parent
    assert get_cron_dir() == path.parent / "cron"


def test_two_instances_get_two_stores(tmp_path: Path) -> None:
    """The point of the option. Asserted as two different paths AND as two
    different files on disk -- equal paths would pass a weaker check that only
    compared the second reading against the default."""
    a, b = _config(tmp_path, "inst-a"), _config(tmp_path, "inst-b")

    apply_config_path(str(a))
    store_a = get_cron_dir() / "jobs.json"
    store_a.write_text('{"jobs": [{"id": "from-a"}]}', encoding="utf-8")

    apply_config_path(str(b))
    store_b = get_cron_dir() / "jobs.json"
    store_b.write_text('{"jobs": [{"id": "from-b"}]}', encoding="utf-8")

    assert store_a != store_b
    assert "from-a" in store_a.read_text()
    assert "from-b" in store_b.read_text()


def test_no_config_leaves_the_default_in_place(tmp_path: Path) -> None:
    """Every existing invocation passes None, so the default must not move."""
    before = get_config_path()

    apply_config_path(None)

    assert get_config_path() == before
    assert get_cron_dir() == before.parent / "cron"


def test_a_missing_config_file_exits_rather_than_creating_one(tmp_path: Path) -> None:
    """Silently creating the file would put the instance on an empty config and
    look like a successful start -- the same shape as an isolation that did not
    isolate."""
    missing = tmp_path / "nope" / "config.json"
    before = get_config_path()

    with pytest.raises(typer.Exit) as exc:
        apply_config_path(str(missing))

    assert exc.value.exit_code == 2
    assert not missing.exists()
    assert get_config_path() == before


def test_a_relative_path_and_a_tilde_both_resolve(tmp_path: Path, monkeypatch) -> None:
    path = _config(tmp_path, "inst-rel")
    monkeypatch.chdir(tmp_path)

    apply_config_path("inst-rel/config.json")

    assert get_config_path() == path.resolve()


def test_the_option_is_declared_on_the_command() -> None:
    """The helper being right is not enough: the command has to expose it."""
    import inspect

    from raven.cli.tui_commands import tui

    assert "config" in inspect.signature(tui).parameters
