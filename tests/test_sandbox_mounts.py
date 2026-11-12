"""MountTable: how host paths map onto the directories mounted into a sandbox."""

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from raven.sandbox.mounts import WORKSPACE_MOUNT, MountTable


def _table(workspace: Path, extra: list[list[str]] | None = None) -> MountTable:
    return MountTable(workspace, extra or [])


class TestTranslateCwd:
    def test_none_returns_workspace_mount(self, tmp_path):
        assert _table(tmp_path).translate_cwd(None) == WORKSPACE_MOUNT == "/workspace"

    def test_workspace_root_returns_workspace_mount(self, tmp_path):
        assert _table(tmp_path).translate_cwd(str(tmp_path)) == "/workspace"

    def test_subdir_translates(self, tmp_path):
        assert _table(tmp_path).translate_cwd(str(tmp_path / "sub" / "dir")) == "/workspace/sub/dir"

    def test_outside_path_falls_back_to_workspace(self, tmp_path):
        assert _table(tmp_path).translate_cwd("/completely/outside") == "/workspace"

    def test_extra_volume_root_translates_to_its_own_guest_path(self, tmp_path):
        workspace, home = tmp_path / "workspace", tmp_path / "home"
        workspace.mkdir()
        home.mkdir()
        assert _table(workspace, [[str(home), "/agent-home", "rw"]]).translate_cwd(str(home)) == "/agent-home"

    def test_extra_volume_subdir_translates(self, tmp_path):
        workspace, home = tmp_path / "workspace", tmp_path / "home"
        workspace.mkdir()
        (home / "skills").mkdir(parents=True)
        table = _table(workspace, [[str(home), "/agent-home", "rw"]])
        assert table.translate_cwd(str(home / "skills")) == "/agent-home/skills"

    def test_nested_mount_wins_over_its_parent(self, tmp_path):
        inner = tmp_path / "inner"
        inner.mkdir()
        table = _table(tmp_path, [[str(inner), "/inner", "ro"]])
        assert table.translate_cwd(str(inner / "x")) == "/inner/x"


class TestTranslateProcessCwd:
    def test_none_returns_workspace_mount(self, tmp_path):
        assert _table(tmp_path).translate_process_cwd(None) == "/workspace"

    def test_a_subdirectory_translates(self, tmp_path):
        assert _table(tmp_path).translate_process_cwd(str(tmp_path / "sub")) == "/workspace/sub"

    def test_a_path_outside_every_volume_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="not inside any volume mounted into the sandbox"):
            _table(tmp_path).translate_process_cwd("/completely/outside")

    def test_guest_mount_path_uses_posix_separators(self):
        relative = PureWindowsPath("packages/demo/bin").relative_to(PureWindowsPath("."))
        assert MountTable.guest_mount_path("/workspace", relative) == "/workspace/packages/demo/bin"


class TestTranslateProcessPath:
    def test_windows_path_suffix_uses_posix_separators(self, tmp_path, monkeypatch):
        table = _table(tmp_path)
        monkeypatch.setattr(table, "roots", lambda: [(PureWindowsPath("C:/workspace"), "/workspace")])
        translated = table.translate_process_path(r"C:\workspace\packages\demo\bin;C:\outside\bin")
        assert translated == r"/workspace/packages/demo/bin;C:\outside\bin"

    def test_every_occurrence_of_one_root_is_translated(self, tmp_path):
        root = str(tmp_path.resolve())
        translated = _table(tmp_path).translate_process_path(f"cd {root}/pkgs/p && node {root}/pkgs/p/server.js")
        assert translated == "cd /workspace/pkgs/p && node /workspace/pkgs/p/server.js"

    def test_a_posix_tail_keeps_its_backslashes(self, tmp_path, monkeypatch):
        table = _table(tmp_path)
        monkeypatch.setattr(table, "roots", lambda: [(PurePosixPath("/srv/work"), "/workspace")])
        backslash = "\\"
        translated = table.translate_process_path(f"--data=/srv/work/d --sep={backslash}t")
        assert translated == f"--data=/workspace/d --sep={backslash}t"

    def test_empty_text_is_returned_unchanged(self, tmp_path):
        assert _table(tmp_path).translate_process_path("") == ""


class TestMountList:
    def test_mounts_list_the_workspace_first_then_extras_with_modes(self, tmp_path):
        home = tmp_path / "home"
        table = _table(tmp_path, [[str(home), "/agent-home", "ro"]])
        assert table.mounts() == [
            (str(tmp_path), "/workspace", False),
            (str(home), "/agent-home", True),
        ]
