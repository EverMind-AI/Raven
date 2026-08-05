"""Unit tests for the file tools' description channel.

Wave6 measured that this model family follows a rule written into a tool
description far more reliably than the same rule written into the identity
prompt, so the operative file-handling discipline lives here. These tests pin
each clause so a description edit cannot silently drop one.
"""

from __future__ import annotations

from raven.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool


class TestReadBeforeModify:
    def test_write_file_requires_reading_an_existing_target(self) -> None:
        desc = WriteFileTool().description
        assert "read it with read_file before overwriting" in desc

    def test_write_file_prefers_editing_over_creating(self) -> None:
        desc = WriteFileTool().description
        assert "ALWAYS prefer editing existing files" in desc
        assert "NEVER write new files unless the task requires it" in desc

    def test_edit_file_requires_reading_first(self) -> None:
        assert "Read the file with read_file before editing" in EditFileTool().description


class TestDoNotAssumePathsExist:
    def test_read_file_warns_against_guessed_paths(self) -> None:
        desc = ReadFileTool().description
        assert "Do not assume a path exists" in desc
        assert "find or list_dir" in desc

    def test_list_dir_warns_against_assumed_directories(self) -> None:
        assert "Do not assume a directory exists" in ListDirTool().description


class TestRepositoryMapping:
    def test_list_dir_recommends_recursive_for_a_first_look(self) -> None:
        assert "recursive=true" in ListDirTool().description
