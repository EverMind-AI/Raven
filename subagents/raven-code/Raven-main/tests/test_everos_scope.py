"""EverOS storage scope derivation.

The scope is what keeps one workspace's memory out of another's while a
single EverOS server serves them all, so its ids have to satisfy EverOS's
own validation and stay stable across processes.
"""

from __future__ import annotations

import re
from pathlib import Path

from raven.plugin.memory.everos.scope import (
    DEFAULT_APP_ID,
    Scope,
    project_id_for_workspace,
    resolve_scope,
)

# everos validates both ids with this charset, 1-128 chars, rejecting "." / "..".
_EVEROS_SCOPE_ID = re.compile(r"[a-zA-Z0-9_.-]{1,128}")


def _legal(value: str) -> bool:
    return bool(_EVEROS_SCOPE_ID.fullmatch(value)) and value not in {".", ".."}


class TestProjectIdDerivation:
    def test_is_legal_for_awkward_paths(self, tmp_path: Path) -> None:
        ws = tmp_path / "some repo (v2) 中文"
        ws.mkdir()
        assert _legal(project_id_for_workspace(ws))

    def test_is_stable_across_calls(self, tmp_path: Path) -> None:
        assert project_id_for_workspace(tmp_path) == project_id_for_workspace(tmp_path)

    def test_distinct_paths_do_not_collide(self, tmp_path: Path) -> None:
        """The escape used for raven's own state dirs maps both of these to the
        same name; here that would merge two repositories' memory."""
        a = tmp_path / "b-c"
        b = tmp_path / "b_c"
        a.mkdir()
        b.mkdir()
        assert project_id_for_workspace(a) != project_id_for_workspace(b)

    def test_bounded_for_a_very_long_directory_name(self, tmp_path: Path) -> None:
        ws = tmp_path / ("x" * 200)
        ws.mkdir()
        assert _legal(project_id_for_workspace(ws))

    def test_a_path_of_only_illegal_characters_still_yields_an_id(self) -> None:
        assert _legal(project_id_for_workspace("/中文/工作区"))


class TestResolveScope:
    def test_defaults_to_raven_app_and_workspace_project(self, tmp_path: Path) -> None:
        scope = resolve_scope(tmp_path)
        assert scope.app_id == DEFAULT_APP_ID
        assert scope.project_id == project_id_for_workspace(tmp_path)

    def test_explicit_config_wins(self, tmp_path: Path) -> None:
        scope = resolve_scope(tmp_path, {"app_id": "ae", "project_id": "task-42"})
        assert scope == Scope(app_id="ae", project_id="task-42")

    def test_without_a_workspace_falls_back_to_the_default_bucket(self) -> None:
        assert resolve_scope(None).project_id == "default"
