"""Unit tests for the filesystem tools (read/write/edit feedback and matching)."""

from __future__ import annotations

import pytest

from raven.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool, _find_matches


@pytest.fixture
def tool(tmp_path):
    return EditFileTool(workspace=tmp_path)


def write(tmp_path, name, content):
    fp = tmp_path / name
    fp.write_text(content, encoding="utf-8")
    return fp


class TestFindMatches:
    def test_exact_matches_with_offsets(self):
        matches = _find_matches("a foo b foo c", "foo")
        assert [off for off, _ in matches] == [2, 8]

    def test_exact_matches_non_overlapping(self):
        matches = _find_matches("aaaa", "aa")
        assert [off for off, _ in matches] == [0, 2]

    def test_trimmed_fallback_reports_window_offsets(self):
        content = "def f():\n    x = 1\n    y = 2\n"
        matches = _find_matches(content, "x = 1\ny = 2")
        assert len(matches) == 1
        off, frag = matches[0]
        assert content[off:].startswith("    x = 1")
        assert frag == "    x = 1\n    y = 2"

    def test_trimmed_fallback_skips_overlapping_windows(self):
        content = "a\na\na\n"
        matches = _find_matches(content, " a\n a")
        assert len(matches) == 1

    def test_no_match(self):
        assert _find_matches("abc", "xyz") == []


class TestEditFileTool:
    async def test_single_match_reports_line(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "one\ntwo\nthree\n")
        result = await tool.execute(path=str(fp), old_text="two", new_text="TWO")
        assert "line 2" in result
        assert fp.read_text() == "one\nTWO\nthree\n"

    async def test_multi_match_error_lists_line_numbers(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "fast car\nslow boat\nfast train\n")
        result = await tool.execute(path=str(fp), old_text="fast", new_text="quick")
        assert result.startswith("Error")
        assert "2 locations" in result
        assert "line 1" in result and "line 3" in result
        assert "occurrence" in result and "replace_all" in result
        assert fp.read_text() == "fast car\nslow boat\nfast train\n"

    async def test_occurrence_targets_nth_match(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "fast car\nslow boat\nfast train\n")
        result = await tool.execute(path=str(fp), old_text="fast", new_text="quick", occurrence=2)
        assert "line 3" in result
        assert fp.read_text() == "fast car\nslow boat\nquick train\n"

    async def test_occurrence_out_of_range_lists_matches(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "fast car\nfast train\n")
        result = await tool.execute(path=str(fp), old_text="fast", new_text="quick", occurrence=5)
        assert result.startswith("Error")
        assert "matches only 2" in result
        assert "line 1" in result

    async def test_occurrence_with_replace_all_rejected(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "fast\n")
        result = await tool.execute(path=str(fp), old_text="fast", new_text="quick", occurrence=1, replace_all=True)
        assert result.startswith("Error")
        assert "mutually exclusive" in result

    async def test_replace_all_reports_count(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "fast car\nfast train\n")
        result = await tool.execute(path=str(fp), old_text="fast", new_text="quick", replace_all=True)
        assert "2 occurrences" in result
        assert fp.read_text() == "quick car\nquick train\n"

    async def test_empty_old_text_points_to_write_file(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "content\n")
        result = await tool.execute(path=str(fp), old_text="", new_text="x")
        assert result.startswith("Error")
        assert "write_file" in result

    async def test_trimmed_fallback_edit(self, tool, tmp_path):
        fp = write(tmp_path, "f.py", "def f():\n    x = 1\n    return x\n")
        result = await tool.execute(path=str(fp), old_text="x = 1\nreturn x", new_text="    return 2")
        assert "Successfully edited" in result
        assert fp.read_text() == "def f():\n    return 2\n"

    async def test_crlf_preserved(self, tool, tmp_path):
        fp = tmp_path / "f.txt"
        fp.write_bytes(b"one\r\ntwo\r\n")
        result = await tool.execute(path=str(fp), old_text="two", new_text="TWO")
        assert "Successfully edited" in result
        assert fp.read_bytes() == b"one\r\nTWO\r\n"

    async def test_crlf_snippet_echoes_edited_region(self, tool, tmp_path):
        # Offsets are computed in LF space; with enough preceding lines the
        # CRLF re-expansion used to shift the echoed window off the edit,
        # showing untouched code as if the edit had missed.
        fp = tmp_path / "f.txt"
        lines = [f"line{i:03d}" for i in range(60)] + ["needle-old"]
        fp.write_bytes(("\r\n".join(lines) + "\r\n").encode())
        result = await tool.execute(path=str(fp), old_text="needle-old", new_text="needle-new")
        assert "Successfully edited" in result
        assert "needle-new" in result
        assert "needle-old" not in result.split("New content:")[1]

    async def test_multi_match_listing_capped(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "hit\n" * 20)
        result = await tool.execute(path=str(fp), old_text="hit", new_text="miss")
        assert "20 locations" in result
        assert "and 12 more" in result

    async def test_success_echoes_edited_region(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "a\nb\nc\nd\ne\n")
        result = await tool.execute(path=str(fp), old_text="c", new_text="C-NEW")
        assert "3| C-NEW" in result
        assert "1| a" in result and "5| e" in result


class TestReadFileTool:
    async def test_long_line_truncated(self, tmp_path):
        fp = write(tmp_path, "min.js", "x" * 10_000 + "\nshort\n")
        result = await ReadFileTool(workspace=tmp_path).execute(path=str(fp))
        assert "line truncated to 2000 chars" in result
        assert "2| short" in result
        assert len(result) < 5_000

    async def test_char_budget_reports_continuation(self, tmp_path):
        fp = write(tmp_path, "big.txt", "\n".join(f"line {i} " + "y" * 100 for i in range(1000)))
        result = await ReadFileTool(workspace=tmp_path).execute(path=str(fp))
        assert "Use offset=" in result
        assert len(result) <= 49_000

    async def test_single_oversized_line_still_returns_content(self, tmp_path):
        fp = write(tmp_path, "one.txt", "z" * 200_000)
        result = await ReadFileTool(workspace=tmp_path).execute(path=str(fp))
        assert result.startswith("1| zzz")


class TestReadBeforeEdit:
    @pytest.fixture
    def tracked(self, tmp_path):
        from raven.agent.tools.filesystem import FileReadTracker

        tracker = FileReadTracker()
        return (
            ReadFileTool(workspace=tmp_path, tracker=tracker),
            WriteFileTool(workspace=tmp_path, tracker=tracker),
            EditFileTool(workspace=tmp_path, tracker=tracker),
        )

    async def test_edit_unread_file_rejected(self, tracked, tmp_path):
        _, _, edit = tracked
        fp = write(tmp_path, "f.txt", "alpha\n")
        result = await edit.execute(path=str(fp), old_text="alpha", new_text="beta")
        assert result.startswith("Error")
        assert "read_file" in result
        assert fp.read_text() == "alpha\n"

    async def test_edit_after_read_succeeds(self, tracked, tmp_path):
        read, _, edit = tracked
        fp = write(tmp_path, "f.txt", "alpha\n")
        await read.execute(path=str(fp))
        result = await edit.execute(path=str(fp), old_text="alpha", new_text="beta")
        assert "Successfully edited" in result

    async def test_edit_after_own_write_succeeds(self, tracked, tmp_path):
        _, write_tool, edit = tracked
        fp = tmp_path / "new.txt"
        await write_tool.execute(path=str(fp), content="alpha\n")
        result = await edit.execute(path=str(fp), old_text="alpha", new_text="beta")
        assert "Successfully edited" in result

    async def test_consecutive_edits_succeed(self, tracked, tmp_path):
        read, _, edit = tracked
        fp = write(tmp_path, "f.txt", "one two\n")
        await read.execute(path=str(fp))
        assert "Successfully" in await edit.execute(path=str(fp), old_text="one", new_text="1")
        assert "Successfully" in await edit.execute(path=str(fp), old_text="two", new_text="2")

    async def test_external_modification_requires_reread(self, tracked, tmp_path):
        import os

        read, _, edit = tracked
        fp = write(tmp_path, "f.txt", "alpha\n")
        await read.execute(path=str(fp))
        fp.write_text("alpha changed\n")
        os.utime(fp, ns=(1, 1))
        result = await edit.execute(path=str(fp), old_text="alpha", new_text="beta")
        assert result.startswith("Error")
        assert "changed since" in result
        await read.execute(path=str(fp))
        assert "Successfully" in await edit.execute(path=str(fp), old_text="alpha", new_text="beta")

    async def test_no_tracker_means_no_enforcement(self, tool, tmp_path):
        fp = write(tmp_path, "f.txt", "alpha\n")
        result = await tool.execute(path=str(fp), old_text="alpha", new_text="beta")
        assert "Successfully edited" in result


class TestWriteFileTool:
    async def test_create_reports_created(self, tmp_path):
        fp = tmp_path / "new.txt"
        result = await WriteFileTool(workspace=tmp_path).execute(path=str(fp), content="hello")
        assert "Created new file" in result
        assert fp.read_text() == "hello"

    async def test_overwrite_reports_previous_size(self, tmp_path):
        fp = write(tmp_path, "old.txt", "previous content here")
        result = await WriteFileTool(workspace=tmp_path).execute(path=str(fp), content="new")
        assert "Overwrote existing file" in result
        assert "was 21 bytes" in result
        assert fp.read_text() == "new"
