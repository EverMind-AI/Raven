"""A preference read while the process runs (raven/config/live.py).

The point of the module, and the thing worth pinning, is the difference between
"read once" and "read now". A switch on the page writes a file; if the answer was
settled at startup, the switch changed a file and nothing else, and the only way
to be believed was to quit.

So these are about the read: that it notices a change, that it does not re-parse
when nothing changed, and that the two failure modes a file has -- absent, and
briefly unparseable while something rewrites it -- answer differently. The last
one matters most: a torn read must keep the previous answer, because withholding
every tool for one turn because a file was mid-write is worse than being one
second stale.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from raven.config import live as live_module
from raven.config.live import LiveConfig, disabled_tool_names


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestReadingNowRatherThanOnce:
    def test_a_change_on_disk_is_the_next_answer(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        assert disabled_tool_names(live) == frozenset({"exec"})

        _write(path, {"tools": {"disabledTools": ["exec", "web_fetch"]}})

        assert disabled_tool_names(live) == frozenset({"exec", "web_fetch"})

    def test_turning_one_back_on_is_the_same_read(self, tmp_path: Path) -> None:
        """The direction that used to be impossible. Withholding is reversible;
        unregistering was not, because nothing remembered what to put back."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        assert disabled_tool_names(live) == frozenset({"exec"})

        _write(path, {"tools": {"disabledTools": []}})

        assert disabled_tool_names(live) == frozenset()

    def test_unchanged_bytes_are_not_parsed_again(self, tmp_path: Path) -> None:
        """Asked once per assembled tool array, so the repeat cost has to be the
        read alone. Counted through the parser rather than asserted about timing.

        The read itself is not what is being saved -- see the class docstring on
        why a ``stat`` fingerprint cannot stand in for the content."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        live.raw()

        parses = 0
        real = live_module.json.loads

        def counting(*a: object, **k: object) -> object:
            nonlocal parses
            parses += 1
            return real(*a, **k)  # type: ignore[arg-type]

        live_module.json.loads = counting  # type: ignore[assignment]
        try:
            for _ in range(5):
                live.raw()
        finally:
            live_module.json.loads = real  # type: ignore[assignment]

        assert parses == 0

    def test_a_same_length_rewrite_is_still_seen(self, tmp_path: Path) -> None:
        """The case a ``stat`` fingerprint got wrong: equal-length writes inside
        one clock tick share an ``(mtime_ns, size)`` pair, and the second one was
        then invisible for the life of the process.

        The collision is forced with ``os.utime`` rather than left to the clock.
        It is the filesystem's granularity that decides whether two real writes
        collide -- ns on APFS, coarse on the CI runner that caught this -- so a
        test that just writes twice quickly asserts nothing where the author runs
        it and everything where CI does."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        stamp = path.stat()
        live = LiveConfig(path)
        assert disabled_tool_names(live) == frozenset({"exec"})

        _write(path, {"tools": {"disabledTools": ["grep"]}})
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert path.stat().st_size == stamp.st_size
        assert disabled_tool_names(live) == frozenset({"grep"})


class TestTheTwoWaysAFileFails:
    def test_no_file_means_no_preferences(self, tmp_path: Path) -> None:
        live = LiveConfig(tmp_path / "absent.json")
        assert disabled_tool_names(live) == frozenset()

    def test_a_file_being_rewritten_keeps_the_last_good_answer(self, tmp_path: Path) -> None:
        """Every writer makes the file briefly unparseable, and that instant must
        not change any answer: emptying the set would offer a switched-off tool,
        and emptying the config would withhold every tool at once."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        assert disabled_tool_names(live) == frozenset({"exec"})

        path.write_text("{ this is half a write", encoding="utf-8")

        assert disabled_tool_names(live) == frozenset({"exec"})

    def test_and_recovers_when_the_write_lands(self, tmp_path: Path) -> None:
        """The pairing case: keeping the old answer must not mean never looking
        again, which a stamp updated on failure would have caused."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        live = LiveConfig(path)
        live.raw()
        path.write_text("{ half", encoding="utf-8")
        assert disabled_tool_names(live) == frozenset({"exec"})

        _write(path, {"tools": {"disabledTools": ["grep"]}})

        assert disabled_tool_names(live) == frozenset({"grep"})

    def test_a_json_document_that_is_not_an_object_reads_as_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert disabled_tool_names(LiveConfig(path)) == frozenset()


class TestBothSpellingsCount:
    def test_the_wire_name_the_page_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"]}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset({"exec"})

    def test_the_snake_case_name_the_schema_reads(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabled_tools": ["exec"]}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset({"exec"})

    def test_a_file_carrying_both_counts_both(self, tmp_path: Path) -> None:
        """A switch that only counts under one spelling is a switch that works
        from one surface: `settings.set` writes the camelCase name and the
        loader's schema reads the snake_case one."""
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec"], "disabled_tools": ["grep"]}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset({"exec", "grep"})

    def test_a_list_holding_junk_keeps_only_the_names(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": ["exec", 7, None, "grep"]}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset({"exec", "grep"})

    def test_a_value_that_is_not_a_list_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        _write(path, {"tools": {"disabledTools": "exec"}})
        assert disabled_tool_names(LiveConfig(path)) == frozenset()
