"""HermesScanner -- home resolution, memory-file discovery, and session listing."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.importer.scanners.hermes import (
    DryRunRunner,
    HermesExportError,
    HermesScanner,
    hermes_session_to_messages,
    list_exportable_sessions,
    parse_dry_run_listing,
    resolve_hermes_home,
    strip_images,
)
from raven.importer.types import Platform, SourceKind


def _make_home(
    root: Path,
    *,
    user_md: str | None = "a\n§\nb",
    memory_md: str | None = "m",
) -> Path:
    home = root / ".hermes"
    mem = home / "memories"
    mem.mkdir(parents=True, exist_ok=True)
    if user_md is not None:
        (mem / "USER.md").write_text(user_md, encoding="utf-8")
        (mem / "USER.md.lock").write_text("", encoding="utf-8")
    if memory_md is not None:
        (mem / "MEMORY.md").write_text(memory_md, encoding="utf-8")
    return home


def test_env_var_wins_over_platform_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert resolve_hermes_home() == tmp_path / "custom"


def test_blank_env_var_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", "   ")
    assert resolve_hermes_home() != tmp_path / "   "


async def test_missing_home_yields_nothing(tmp_path: Path) -> None:
    results = await HermesScanner(hermes_home=tmp_path / "nope").scan()
    assert results == []


async def test_both_memory_files_discovered(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    results = await HermesScanner(hermes_home=home).scan()
    keys = {r.source_key for r in results}
    assert keys == {"user-md", "memory-md"}
    assert all(r.platform is Platform.HERMES for r in results)
    assert all(r.kind is SourceKind.MEMORY_FILE for r in results)


async def test_lock_sibling_is_not_a_source(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    results = await HermesScanner(hermes_home=home).scan()
    assert not any(".lock" in str(p) for r in results for p in r.file_paths)


async def test_missing_one_file_yields_only_the_other(tmp_path: Path) -> None:
    home = _make_home(tmp_path, memory_md=None)
    results = await HermesScanner(hermes_home=home).scan()
    assert [r.source_key for r in results] == ["user-md"]


async def test_scan_result_carries_size_and_mtime(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="x" * 40)
    (result,) = [r for r in await HermesScanner(hermes_home=home).scan() if r.source_key == "user-md"]
    assert result.estimated_size == 40
    assert result.mtime > 0


async def test_user_md_entries_are_all_user_role(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="fact one\n§\nfact two")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert session.session_id == "import-hermes-user-md"
    assert [m.role for m in session.messages] == ["user", "user", "user"]
    assert [m.content for m in session.messages[1:]] == ["fact one", "fact two"]


async def test_memory_md_entries_are_assistant_after_a_user_preamble(tmp_path: Path) -> None:
    home = _make_home(tmp_path, memory_md="agent noted X\n§\nagent noted Y")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "memory-md"]
    session = await scanner.read(result)
    assert [m.role for m in session.messages] == ["user", "assistant", "assistant"]


async def test_bare_section_sign_inside_an_entry_is_not_split(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="see § 4.2 for details\n§\nsecond")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    bodies = [m.content for m in session.messages[1:]]
    assert bodies == ["see § 4.2 for details", "second"]


async def test_blank_entries_dropped_and_empty_file_yields_preamble_only(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="a\n§\n\n§\n   \n§\nb")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert [m.content for m in session.messages[1:]] == ["a", "b"]


async def test_empty_file_yields_zero_messages(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert session.messages == ()


async def test_timestamps_are_monotonic_from_mtime(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="a\n§\nb")
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    stamps = [m.timestamp for m in session.messages]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    assert stamps[0] == int(result.mtime * 1000)


async def test_preamble_language_follows_content(tmp_path: Path) -> None:
    cjk = await _preamble(tmp_path / "cjk", "中文事实一\n§\n中文事实二")
    latin = await _preamble(tmp_path / "latin", "an english fact")
    assert "Hermes" in cjk and any("一" <= c <= "鿿" for c in cjk)
    assert "Hermes" in latin and not any("一" <= c <= "鿿" for c in latin)


async def test_preamble_language_looks_past_the_first_entry(tmp_path: Path) -> None:
    # A latin first entry long enough to fill is_cjk's 200-char default sample,
    # followed by CJK entries. Sampling only the head would pick the wrong
    # language for a file that is mostly Chinese.
    body = ("an english fact that runs on for a while " * 6) + "\n§\n" + "中文事实"
    preamble = await _preamble(tmp_path / "mixed", body)
    assert any("一" <= c <= "鿿" for c in preamble)


async def _preamble(root: Path, body: str) -> str:
    home = _make_home(root, user_md=body)
    scanner = HermesScanner(hermes_home=home)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    return session.messages[0].content


# -- parse_dry_run_listing -------------------------------------------------

_GOOD = """Would export 2 session(s) (>= 1 messages).
  20260723_153451_9a0929  desktop
  20260727_110342_d612f2ef  qqbot
"""


def test_parses_ids_from_normal_output() -> None:
    listing = parse_dry_run_listing(_GOOD)
    assert listing.expected == 2
    assert listing.session_ids == (
        "20260723_153451_9a0929",
        "20260727_110342_d612f2ef",
    )


def test_extra_column_does_not_break_parsing() -> None:
    text = "Would export 1 session(s).\n  20260723_153451_9a0929  desktop  some title\n"
    assert parse_dry_run_listing(text).session_ids == ("20260723_153451_9a0929",)


def test_malformed_id_lines_are_ignored() -> None:
    text = "Would export 1 session(s).\n  ----------\n  20260723_153451_9a0929  desktop\n"
    assert parse_dry_run_listing(text).session_ids == ("20260723_153451_9a0929",)


def test_count_without_any_parsable_id_is_an_error() -> None:
    text = "Would export 3 session(s).\n  ??? garbage\n"
    with pytest.raises(HermesExportError, match="3"):
        parse_dry_run_listing(text)


def test_unrecognised_header_is_an_error() -> None:
    with pytest.raises(HermesExportError, match="header"):
        parse_dry_run_listing("Exporting sessions now...\n  20260723_153451_9a0929  cli\n")


def test_zero_sessions_is_a_valid_empty_result() -> None:
    listing = parse_dry_run_listing("Would export 0 session(s) (>= 1 messages).\n")
    assert listing.expected == 0
    assert listing.session_ids == ()


def test_uncapped_count_with_short_id_list_is_not_an_error() -> None:
    # The 100-cap truncation: expected > len(ids) is documented, not a format break.
    text = "Would export 3 session(s).\n  20260723_153451_9a0929  desktop\n  ... 2 more\n"
    listing = parse_dry_run_listing(text)
    assert listing.expected == 3
    assert listing.session_ids == ("20260723_153451_9a0929",)


# -- list_exportable_sessions -----------------------------------------------

_TIME_FMT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True)
class _FakeSession:
    session_id: str
    started_at: datetime


def _extract_bound(args: list[str], flag: str) -> datetime | None:
    if flag not in args:
        return None
    return datetime.strptime(args[args.index(flag) + 1], _TIME_FMT)


def _make_fake_runner(sessions: list[_FakeSession], calls: list[list[str]]) -> DryRunRunner:
    async def fake(args: list[str]) -> str:
        calls.append(list(args))
        after = _extract_bound(args, "--after")
        before = _extract_bound(args, "--before")
        matched = sorted(
            (
                s
                for s in sessions
                if (after is None or s.started_at >= after) and (before is None or s.started_at < before)
            ),
            key=lambda s: s.started_at,
        )
        listed = matched[:100]
        lines = [f"Would export {len(matched)} session(s) (>= 1 messages)."]
        lines += [f"  {s.session_id}  fake" for s in listed]
        if len(matched) > 100:
            lines.append(f"  ... {len(matched) - 100} more")
        return "\n".join(lines) + "\n"

    return fake


async def test_root_probe_under_cap_makes_one_unwindowed_call() -> None:
    sessions = [
        _FakeSession("20260723_153451_9a0929", datetime(2026, 7, 23, 15, 34, 51)),
        _FakeSession("20260727_110342_d612f2ef", datetime(2026, 7, 27, 11, 3, 42)),
    ]
    calls: list[list[str]] = []
    listing = await list_exportable_sessions(runner=_make_fake_runner(sessions, calls))
    assert listing.session_ids == tuple(s.session_id for s in sessions)
    assert listing.unlisted == 0
    assert len(calls) == 1
    assert "--after" not in calls[0]
    assert "--before" not in calls[0]


async def test_root_probe_over_cap_fans_out_to_union_with_no_duplicates() -> None:
    sessions = [
        _FakeSession(f"20260101_{i:04d}00_{i:06x}", datetime(2026, 1, 1, 0, 0) + timedelta(minutes=i))
        for i in range(130)
    ]
    calls: list[list[str]] = []
    listing = await list_exportable_sessions(runner=_make_fake_runner(sessions, calls))
    assert listing.unlisted == 0
    assert len(listing.session_ids) == len(set(listing.session_ids))
    assert set(listing.session_ids) == {s.session_id for s in sessions}
    assert len(calls) > 1


async def test_sessions_outside_the_partition_are_counted_not_dropped() -> None:
    """A partition that misses a range returns a well-formed short list, so
    only reconciling against the root count can notice. Pinned with a session
    older than the partition floor -- the shape a later-dated floor would make
    routine."""
    sessions = [
        _FakeSession(f"20260101_{i:04d}00_{i:06x}", datetime(2026, 1, 1, 0, 0) + timedelta(minutes=i))
        for i in range(130)
    ]
    sessions.append(_FakeSession("19690101_000000_a1", datetime(1969, 1, 1)))
    listing = await list_exportable_sessions(runner=_make_fake_runner(sessions, []))
    assert "19690101_000000_a1" not in listing.session_ids
    assert len(listing.session_ids) == 130
    assert listing.unlisted == 1


async def test_partition_floor_reaches_far_enough_back_to_collect_old_sessions() -> None:
    """A recent-looking floor is silently lossy rather than merely slower: a
    session starting before it lands in no window at all."""
    sessions = [
        _FakeSession(f"20260101_{i:04d}00_{i:06x}", datetime(2026, 1, 1, 0, 0) + timedelta(minutes=i))
        for i in range(130)
    ]
    sessions.append(_FakeSession("20150601_120000_b2", datetime(2015, 6, 1, 12, 0)))
    listing = await list_exportable_sessions(runner=_make_fake_runner(sessions, []))
    assert "20150601_120000_b2" in listing.session_ids
    assert listing.unlisted == 0


async def test_every_probe_carries_min_messages_one() -> None:
    sessions = [
        _FakeSession(f"20260101_{i:04d}00_{i:06x}", datetime(2026, 1, 1, 0, 0) + timedelta(minutes=i))
        for i in range(130)
    ]
    calls: list[list[str]] = []
    await list_exportable_sessions(runner=_make_fake_runner(sessions, calls))
    assert len(calls) > 1
    for call in calls:
        assert "--min-messages" in call
        assert call[call.index("--min-messages") + 1] == "1"


async def test_window_bounds_are_always_whole_minutes() -> None:
    sessions = [
        _FakeSession(f"20260101_{i:04d}00_{i:06x}", datetime(2026, 1, 1, 0, 0) + timedelta(minutes=i))
        for i in range(130)
    ]
    calls: list[list[str]] = []
    await list_exportable_sessions(runner=_make_fake_runner(sessions, calls))
    for call in calls:
        for flag in ("--after", "--before"):
            bound = _extract_bound(call, flag)
            if bound is not None:
                assert bound.second == 0
                assert bound.microsecond == 0


async def test_one_minute_window_still_over_cap_reports_unlisted_without_raising() -> None:
    crowded = datetime(2026, 1, 15, 10, 0)
    sessions = [_FakeSession(f"20260115_100000_{i:06x}", crowded) for i in range(150)]
    calls: list[list[str]] = []
    listing = await list_exportable_sessions(runner=_make_fake_runner(sessions, calls))
    assert len(listing.session_ids) == 100
    assert listing.unlisted == 50


async def test_exactly_the_cap_is_listed_in_full_without_windowing() -> None:
    """The boundary the whole task turns on: hermes prints its hundredth row,
    so a count equal to the cap is complete and must not be split."""
    sessions = [
        _FakeSession(f"20260101_{i:04d}00_{i:06x}", datetime(2026, 1, 1, 0, 0) + timedelta(minutes=i))
        for i in range(100)
    ]
    calls: list[list[str]] = []
    listing = await list_exportable_sessions(runner=_make_fake_runner(sessions, calls))
    assert len(listing.session_ids) == 100
    assert listing.unlisted == 0
    assert len(calls) == 1


async def test_a_session_started_this_very_minute_is_still_collected() -> None:
    """The ceiling needs a buffer past the current minute. Bounds are formatted
    to minute precision, so a ceiling of `now` becomes `--before <this minute>`
    and excludes anything started during the minute the scan runs in."""
    now = datetime.now()
    sessions = [
        _FakeSession(f"20260101_{i:04d}00_{i:06x}", datetime(2026, 1, 1, 0, 0) + timedelta(minutes=i))
        for i in range(130)
    ]
    fresh = _FakeSession(f"{now:%Y%m%d_%H%M%S}_ff", now)
    sessions.append(fresh)
    listing = await list_exportable_sessions(runner=_make_fake_runner(sessions, []))
    assert fresh.session_id in listing.session_ids
    assert listing.unlisted == 0


async def test_missing_hermes_executable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(HermesExportError):
        await list_exportable_sessions()


# -- hermes_session_to_messages / strip_images ------------------------------

_SESSION = {
    "id": "20260727_110342_d612f2ef",
    "system_prompt": "You are Hermes Agent...",
    "messages": [
        {"role": "user", "content": "hi", "timestamp": 1785121422.9861},
        {"role": "session_meta", "content": None, "timestamp": 1785121430.8},
        {
            "role": "assistant",
            "content": "",
            "timestamp": 1785121500.0,
            "reasoning_content": "secret thoughts",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "file body", "timestamp": 1785121529.55, "tool_call_id": "c1"},
        {"role": "assistant", "content": "done", "timestamp": 1785121600.0},
        {"role": "user", "content": "no time", "timestamp": None},
        {"role": "user", "content": "", "timestamp": 1785121700.0},
    ],
}


def test_roles_and_order_preserved() -> None:
    msgs = hermes_session_to_messages(_SESSION)
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "assistant"]


def test_session_meta_and_system_prompt_dropped() -> None:
    msgs = hermes_session_to_messages(_SESSION)
    assert all(m.role != "session_meta" for m in msgs)
    assert all("Hermes Agent" not in m.content for m in msgs)


def test_an_unaccepted_role_is_dropped_even_when_it_carries_content() -> None:
    """Real session_meta rows have `content: None`, so the empty-content rule
    alone drops them and leaves the role filter unverified. EverOS accepts only
    user, assistant and tool, so a populated row of any other role must go."""
    session = {
        "id": "s",
        "messages": [
            {"role": "system", "content": "you are hermes", "timestamp": 1.0},
            {"role": "developer", "content": "hidden instruction", "timestamp": 2.0},
            {"role": "user", "content": "kept", "timestamp": 3.0},
        ],
    }
    msgs = hermes_session_to_messages(session)
    assert [m.role for m in msgs] == ["user"]
    assert [m.content for m in msgs] == ["kept"]


def test_reasoning_is_not_carried_over() -> None:
    msgs = hermes_session_to_messages(_SESSION)
    assert all("secret thoughts" not in m.content for m in msgs)


def test_timestamp_converted_from_float_seconds_to_int_ms() -> None:
    msgs = hermes_session_to_messages(_SESSION)
    assert msgs[0].timestamp == 1785121422986
    assert all(isinstance(m.timestamp, int) and m.timestamp > 0 for m in msgs)


def test_message_without_timestamp_dropped() -> None:
    msgs = hermes_session_to_messages(_SESSION)
    assert all(m.content != "no time" for m in msgs)


def test_empty_content_without_tool_calls_dropped() -> None:
    msgs = hermes_session_to_messages(_SESSION)
    assert len([m for m in msgs if m.content == ""]) == 1  # only the tool_calls carrier


def test_tool_calls_and_tool_call_id_pass_through() -> None:
    msgs = hermes_session_to_messages(_SESSION)
    caller = next(m for m in msgs if m.tool_calls)
    assert caller.tool_calls[0]["function"]["name"] == "read"
    result = next(m for m in msgs if m.role == "tool")
    assert result.tool_call_id == "c1"


def test_strip_images_keeps_text_and_counts_images() -> None:
    content = [
        {"type": "text", "text": "look at these"},
        {"type": "image_url", "image_url": "data:image/png;base64,AAAA"},
        {"type": "image_url", "image_url": "data:image/png;base64,BBBB"},
    ]
    out = strip_images(content)
    assert "look at these" in out
    assert "AAAA" not in out
    assert "[image x2]" in out


def test_strip_images_passes_plain_strings_through() -> None:
    assert strip_images("plain") == "plain"


def test_long_content_truncated() -> None:
    session = {"id": "s", "messages": [{"role": "user", "content": "x" * 20_000, "timestamp": 1.0}]}
    (msg,) = hermes_session_to_messages(session)
    assert len(msg.content) == 10_003  # limit + "..."


def test_strip_images_input_text_part_keeps_text_and_is_not_counted_as_image() -> None:
    # input_text carries its text under "content", not "text" -- the shape the
    # brief's version does not handle and falls through to the image branch.
    content = [{"type": "input_text", "content": "hello from input_text"}]
    assert strip_images(content) == "hello from input_text"


def test_strip_images_input_text_mixed_with_a_real_image_counts_only_the_image() -> None:
    content = [
        {"type": "input_text", "content": "an input_text part"},
        {"type": "text", "text": "a text part"},
        {"type": "image_url", "image_url": "data:image/png;base64,AAAA"},
    ]
    out = strip_images(content)
    assert "an input_text part" in out
    assert "a text part" in out
    assert "[image x1]" in out


def test_strip_images_nested_image_url_dict_form_still_counts_as_one_image() -> None:
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    out = strip_images(content)
    assert "[image x1]" in out
    assert "AAAA" not in out


def test_strip_images_input_image_type_counts_as_image() -> None:
    content = [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
    out = strip_images(content)
    assert "[image x1]" in out


def test_strip_images_unknown_dict_part_falls_back_to_text_field() -> None:
    content = [{"type": "mystery", "text": "fallback via text"}]
    out = strip_images(content)
    assert out == "fallback via text"
    assert "[image" not in out


def test_strip_images_unknown_dict_part_falls_back_to_content_field() -> None:
    content = [{"type": "mystery", "content": "fallback via content"}]
    out = strip_images(content)
    assert out == "fallback via content"
    assert "[image" not in out


def test_strip_images_unknown_dict_part_with_neither_field_is_dropped_not_counted() -> None:
    content = [{"type": "mystery", "other": "irrelevant"}]
    out = strip_images(content)
    assert out == ""
    assert "[image" not in out
