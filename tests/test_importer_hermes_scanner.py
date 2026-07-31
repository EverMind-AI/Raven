"""HermesScanner -- home resolution, memory-file discovery, and session listing."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from raven.importer.scanners import hermes as hermes_module
from raven.importer.scanners.hermes import (
    DryRunRunner,
    HermesExportError,
    HermesScanner,
    SessionListing,
    hermes_session_to_messages,
    list_exportable_sessions,
    parse_dry_run_listing,
    resolve_hermes_home,
    strip_images,
)
from raven.importer.types import Platform, ScanResult, SourceKind


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


# Captured before the autouse guard below can replace it, so the one test that
# needs the real runner's behaviour can still reach it explicitly.
_REAL_HERMES_CLI_RUNNER = hermes_module._run_hermes_cli


@pytest.fixture(autouse=True)
def _forbid_the_real_hermes_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make reaching the real binary a loud failure rather than a silent spawn.

    `scan()` probes for conversations, which shells out. A test that forgets to
    pass a fake would otherwise pass or fail depending on whether the developer
    happens to have hermes installed and what sessions it holds -- so the
    default is replaced with something that names the mistake instead.
    """

    async def _refuse(args: Sequence[str]) -> str:
        raise AssertionError(f"test reached the real hermes CLI with {list(args)!r}; pass run_cli= or runner=")

    monkeypatch.setattr(hermes_module, "_run_hermes_cli", _refuse)


async def _no_conversations(args: list[str]) -> str:
    """A run_cli stub for tests that only care about memory files.

    scan() always probes for conversations too, so a test that only exercises
    memory files still has to answer that probe.
    """
    return "Would export 0 session(s) (>= 1 messages).\n"


def test_env_var_wins_over_platform_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert resolve_hermes_home() == tmp_path / "custom"


def test_blank_env_var_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", "   ")
    assert resolve_hermes_home() != tmp_path / "   "


def test_active_named_profile_resolves_under_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "active_profile").write_text("coder\n", encoding="utf-8")
    assert resolve_hermes_home() == home / "profiles" / "coder"


def test_active_profile_named_default_stays_at_the_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "active_profile").write_text("Default\n", encoding="utf-8")
    assert resolve_hermes_home() == home


def test_missing_active_profile_file_stays_at_the_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_hermes_home() == tmp_path / ".hermes"


def test_env_var_still_wins_over_an_active_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "active_profile").write_text("coder\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert resolve_hermes_home() == tmp_path / "custom"


def test_windows_fallback_without_localappdata_uses_home_appdata_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(hermes_module.sys, "platform", "win32")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_hermes_home() == tmp_path / "AppData" / "Local" / "hermes"


def test_windows_fallback_prefers_localappdata_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setattr(hermes_module.sys, "platform", "win32")
    assert resolve_hermes_home() == tmp_path / "AppData" / "Local" / "hermes"


async def test_missing_home_yields_nothing(tmp_path: Path) -> None:
    results = await HermesScanner(hermes_home=tmp_path / "nope").scan()
    assert results == []


async def test_both_memory_files_discovered(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    results = await HermesScanner(hermes_home=home, run_cli=_no_conversations).scan()
    keys = {r.source_key for r in results}
    assert keys == {"user-md", "memory-md"}
    assert all(r.platform is Platform.HERMES for r in results)
    assert all(r.kind is SourceKind.MEMORY_FILE for r in results)


async def test_lock_sibling_is_not_a_source(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    results = await HermesScanner(hermes_home=home, run_cli=_no_conversations).scan()
    assert not any(".lock" in str(p) for r in results for p in r.file_paths)


async def test_missing_one_file_yields_only_the_other(tmp_path: Path) -> None:
    home = _make_home(tmp_path, memory_md=None)
    results = await HermesScanner(hermes_home=home, run_cli=_no_conversations).scan()
    assert [r.source_key for r in results] == ["user-md"]


async def test_scan_result_carries_size_and_mtime(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="x" * 40)
    (result,) = [
        r for r in await HermesScanner(hermes_home=home, run_cli=_no_conversations).scan() if r.source_key == "user-md"
    ]
    assert result.estimated_size == 40
    assert result.mtime > 0


async def test_user_md_entries_are_all_user_role(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="fact one\n§\nfact two")
    scanner = HermesScanner(hermes_home=home, run_cli=_no_conversations)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert session.session_id == "import-hermes-user-md"
    assert [m.role for m in session.messages] == ["user", "user", "user"]
    assert [m.content for m in session.messages[1:]] == ["fact one", "fact two"]


async def test_memory_md_entries_are_assistant_after_a_user_preamble(tmp_path: Path) -> None:
    home = _make_home(tmp_path, memory_md="agent noted X\n§\nagent noted Y")
    scanner = HermesScanner(hermes_home=home, run_cli=_no_conversations)
    (result,) = [r for r in await scanner.scan() if r.source_key == "memory-md"]
    session = await scanner.read(result)
    assert [m.role for m in session.messages] == ["user", "assistant", "assistant"]


async def test_bare_section_sign_inside_an_entry_is_not_split(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="see § 4.2 for details\n§\nsecond")
    scanner = HermesScanner(hermes_home=home, run_cli=_no_conversations)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    bodies = [m.content for m in session.messages[1:]]
    assert bodies == ["see § 4.2 for details", "second"]


async def test_blank_entries_dropped_and_empty_file_yields_preamble_only(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="a\n§\n\n§\n   \n§\nb")
    scanner = HermesScanner(hermes_home=home, run_cli=_no_conversations)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert [m.content for m in session.messages[1:]] == ["a", "b"]


async def test_empty_file_yields_zero_messages(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="")
    scanner = HermesScanner(hermes_home=home, run_cli=_no_conversations)
    (result,) = [r for r in await scanner.scan() if r.source_key == "user-md"]
    session = await scanner.read(result)
    assert session.messages == ()


async def test_timestamps_are_monotonic_from_mtime(tmp_path: Path) -> None:
    home = _make_home(tmp_path, user_md="a\n§\nb")
    scanner = HermesScanner(hermes_home=home, run_cli=_no_conversations)
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
    scanner = HermesScanner(hermes_home=home, run_cli=_no_conversations)
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


def test_more_rows_than_the_header_counted_is_an_error() -> None:
    """Rows are read structurally, not by guessing at the id's shape, because
    cron ids are `cron_<job>_<stamp>` and ACP ids are bare uuids. The safety net
    is hermes' own invariant: it never prints more rows than it counted, so a
    surplus means something that is not a session row was read as one."""
    text = "Would export 1 session(s).\n  ----------\n  20260723_153451_9a0929  desktop\n"
    with pytest.raises(HermesExportError, match="no longer understood"):
        parse_dry_run_listing(text)


def test_a_cron_or_acp_session_id_is_not_discarded() -> None:
    text = (
        "Would export 3 session(s).\n"
        "  20260723_153451_9a0929  desktop\n"
        "  cron_daily-report_20260725_090000  cron\n"
        "  3f2b1c88-4d5e-4a6b-8c9d-0e1f2a3b4c5d  acp\n"
    )
    listing = parse_dry_run_listing(text)
    assert listing.expected == 3
    assert len(listing.session_ids) == 3
    assert "cron_daily-report_20260725_090000" in listing.session_ids


def test_a_count_with_no_listing_rows_at_all_is_an_error() -> None:
    """Zero rows under a non-zero count means the format changed, and returning
    an empty list would report "no conversations, done" as a success."""
    text = "Would export 3 session(s).\nnot an indented row\n"
    with pytest.raises(HermesExportError, match="3"):
        parse_dry_run_listing(text)


async def test_the_short_path_reports_rows_it_could_not_parse() -> None:
    """The under-100 path is the one nearly every install takes, so a header
    counting more than the rows yielded has to surface there too -- otherwise a
    dropped session is invisible exactly where it is most likely."""

    async def runner(args: Sequence[str]) -> str:
        return "Would export 5 session(s) (>= 1 messages).\n  20260723_153451_9a0929  desktop\n"

    listing = await list_exportable_sessions(runner=runner)
    assert listing.session_ids == ("20260723_153451_9a0929",)
    assert listing.unlisted == 4


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


async def test_ceiling_buffer_covers_a_session_started_on_the_exact_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The buffer only matters when now already sits on a minute boundary.

    `_ceil_to_minute` is a no-op there, and bounds format to minute precision,
    so without the extra minute the ceiling becomes `--before <now>` -- which
    excludes a session started at that very instant. Freezing the clock is the
    only way to reach that case; a wall-clock `now` almost never lands on it,
    which is why the previous version of this test passed either way.
    """
    frozen = datetime(2026, 3, 4, 5, 6, 0)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return frozen

    monkeypatch.setattr(hermes_module, "datetime", _FrozenDatetime)
    sessions = [
        _FakeSession(f"20260101_{i:04d}00_{i:06x}", datetime(2026, 1, 1, 0, 0) + timedelta(minutes=i))
        for i in range(130)
    ]
    fresh = _FakeSession("20260304_050600_ff", frozen)
    sessions.append(fresh)
    listing = await list_exportable_sessions(runner=_make_fake_runner(sessions, []))
    assert fresh.session_id in listing.session_ids
    assert listing.unlisted == 0


async def test_missing_hermes_executable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reaches for the real runner on purpose -- it is the thing under test --
    so it uses the reference captured before the autouse guard replaced it."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(HermesExportError):
        await list_exportable_sessions(runner=_REAL_HERMES_CLI_RUNNER)


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


def test_tool_call_id_stays_absent_on_non_tool_roles() -> None:
    """Real exports carry None there for user and assistant rows, and EverOS
    pairs a tool result to its call by this id -- a stray one would invent a
    pairing that never happened."""
    msgs = hermes_session_to_messages(_SESSION)
    assert [m.tool_call_id for m in msgs if m.role != "tool"] == [None, None, None]


def test_tool_calls_reach_import_message_as_a_tuple() -> None:
    """ImportMessage declares tuple[dict, ...]; hermes hands over a list."""
    caller = next(m for m in hermes_session_to_messages(_SESSION) if m.tool_calls)
    assert isinstance(caller.tool_calls, tuple)


def test_a_zero_timestamp_is_dropped() -> None:
    """EverOS requires timestamp > 0, so zero is as unusable as a missing one."""
    session = {
        "id": "s",
        "messages": [
            {"role": "user", "content": "epoch", "timestamp": 0},
            {"role": "user", "content": "kept", "timestamp": 1.0},
        ],
    }
    assert [m.content for m in hermes_session_to_messages(session)] == ["kept"]


def test_strip_images_keeps_text_and_counts_images() -> None:
    content = [
        {"type": "text", "text": "look at these"},
        {"type": "image_url", "image_url": "data:image/png;base64,AAAA"},
        {"type": "image_url", "image_url": "data:image/png;base64,BBBB"},
    ]
    out = strip_images(content)
    assert "look at these" in out
    assert "AAAA" not in out
    assert "[media x2]" in out


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
    assert "[media x1]" in out


def test_strip_images_nested_image_url_dict_form_still_counts_as_one_image() -> None:
    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    out = strip_images(content)
    assert "[media x1]" in out
    assert "AAAA" not in out


def test_strip_images_input_image_type_counts_as_image() -> None:
    content = [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]
    out = strip_images(content)
    assert "[media x1]" in out


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


def test_strip_images_unknown_part_with_no_text_still_leaves_a_marker() -> None:
    """The goal is to keep the words and mark where the bytes were, so a part
    type neither we nor upstream recognises is counted rather than dropped --
    otherwise a message whose only part it was empties out and the fact that the
    turn happened is lost."""
    assert strip_images([{"type": "mystery", "other": "irrelevant"}]) == "[media x1]"
    assert strip_images([{"type": "text", "text": "hi"}, {"type": "mystery"}]) == "hi\n\n[media x1]"


def test_a_message_of_only_unknown_parts_survives_as_a_marker() -> None:
    session = {"id": "s", "messages": [{"role": "user", "content": [{"type": "video"}], "timestamp": 1.0}]}
    (msg,) = hermes_session_to_messages(session)
    assert msg.content == "[media x1]"


# -- HermesScanner conversation wiring --------------------------------------


class _FakeCli:
    """Stands in for the hermes binary via the scanner's run_cli seam.

    The single seam used for both the dry-run listing and the export takes
    just an argument list and returns stdout -- there is no separate output
    path, since both calls pass "-" for stdout themselves.
    """

    def __init__(self, listing: str, sessions: dict[str, dict]) -> None:
        self.listing = listing
        self.sessions = sessions
        self.calls: list[list[str]] = []

    async def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if "--dry-run" in args:
            return self.listing
        sid = args[args.index("--session-id") + 1]
        return json.dumps(self.sessions[sid]) + "\n"


async def test_conversations_become_scan_results(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    cli = _FakeCli(_GOOD, {})
    results = await HermesScanner(hermes_home=home, run_cli=cli).scan()
    convs = [r for r in results if r.kind is SourceKind.CONVERSATION]
    assert {r.source_key for r in convs} == {
        "20260723_153451_9a0929",
        "20260727_110342_d612f2ef",
    }
    assert all(r.platform is Platform.HERMES for r in convs)
    assert all(r.file_paths == () and r.estimated_size == 0 and r.mtime == 0.0 for r in convs)


async def test_default_run_cli_is_the_module_level_hermes_cli_runner() -> None:
    scanner = HermesScanner(hermes_home=Path("/nonexistent"))
    assert scanner._run_cli is hermes_module._run_hermes_cli


async def test_read_conversation_uses_session_id_export(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    sid = "20260727_110342_d612f2ef"
    cli = _FakeCli(f"Would export 1 session(s).\n  {sid}  qqbot\n", {sid: _SESSION})
    scanner = HermesScanner(hermes_home=home, run_cli=cli)
    (conv,) = [r for r in await scanner.scan() if r.kind is SourceKind.CONVERSATION]
    session = await scanner.read(conv)
    assert session.session_id == f"import-hermes-{sid}"
    assert [m.role for m in session.messages] == ["user", "assistant", "tool", "assistant"]
    export_call = cli.calls[-1]
    assert export_call[export_call.index("--session-id") : export_call.index("--session-id") + 2] == [
        "--session-id",
        sid,
    ]
    assert export_call[-1] == "-"


async def test_read_conversation_tolerates_multiple_export_lines(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    sid = "s1"
    first = {"id": sid, "messages": [{"role": "user", "content": "first", "timestamp": 1.0}]}
    second = {"id": sid, "messages": [{"role": "user", "content": "second", "timestamp": 2.0}]}

    async def cli(args: list[str]) -> str:
        return json.dumps(first) + "\n" + json.dumps(second) + "\n"

    scanner = HermesScanner(hermes_home=home, run_cli=cli)
    result = ScanResult(
        source_key=sid,
        platform=Platform.HERMES,
        kind=SourceKind.CONVERSATION,
        file_paths=(),
        estimated_size=0,
        mtime=0.0,
    )
    session = await scanner.read(result)
    assert [m.content for m in session.messages] == ["first", "second"]


async def test_scan_conversations_logs_a_warning_for_unlisted_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _make_home(tmp_path)
    listing = SessionListing(session_ids=("20260723_153451_9a0929",), unlisted=50)

    async def fake_list_exportable_sessions(*, runner: DryRunRunner) -> SessionListing:
        return listing

    monkeypatch.setattr(hermes_module, "list_exportable_sessions", fake_list_exportable_sessions)
    warnings: list[tuple] = []
    monkeypatch.setattr(hermes_module.logger, "warning", lambda *a, **k: warnings.append(a))

    results = await HermesScanner(hermes_home=home, run_cli=_no_conversations).scan()
    convs = [r for r in results if r.kind is SourceKind.CONVERSATION]
    assert {r.source_key for r in convs} == {"20260723_153451_9a0929"}
    assert any(50 in call for call in warnings)


async def test_a_missing_binary_costs_the_conversations_not_the_memory_files(tmp_path: Path) -> None:
    """The memory files are read straight off disk and never needed the CLI, so
    losing hermes must not lose them too. The earlier version of this test
    asserted the opposite and pinned that loss in place."""
    home = _make_home(tmp_path)

    async def _boom(args: list[str]) -> str:
        raise FileNotFoundError("hermes")

    scanner = HermesScanner(hermes_home=home, run_cli=_boom)
    results = await scanner.scan()
    assert {r.source_key for r in results} == {"user-md", "memory-md"}
    assert all(r.kind is SourceKind.MEMORY_FILE for r in results)
    assert isinstance(scanner.partial_failure, HermesExportError)


async def test_read_conversation_names_the_session_when_hermes_prints_a_failure_to_stdout(
    tmp_path: Path,
) -> None:
    """Hermes can print an error message to stdout and still exit 0, so
    ``json.loads`` fails with a message that names neither the session nor
    what hermes actually said. Both must be surfaced."""
    home = _make_home(tmp_path)
    sid = "s1"

    async def cli(args: list[str]) -> str:
        return "Error: session store is locked by another process\n"

    scanner = HermesScanner(hermes_home=home, run_cli=cli)
    result = ScanResult(
        source_key=sid,
        platform=Platform.HERMES,
        kind=SourceKind.CONVERSATION,
        file_paths=(),
        estimated_size=0,
        mtime=0.0,
    )
    with pytest.raises(HermesExportError, match=sid) as exc_info:
        await scanner.read(result)
    assert "session store is locked by another process" in str(exc_info.value)


async def test_read_wraps_a_missing_binary_as_an_export_error(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    result = ScanResult(
        source_key="sid",
        platform=Platform.HERMES,
        kind=SourceKind.CONVERSATION,
        file_paths=(),
        estimated_size=0,
        mtime=0.0,
    )

    async def _boom(args: list[str]) -> str:
        raise FileNotFoundError("hermes")

    with pytest.raises(HermesExportError, match="hermes"):
        await HermesScanner(hermes_home=home, run_cli=_boom).read(result)
