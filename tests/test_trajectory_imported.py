"""Tests for the Imported Conversation store and its runner (`raven.trajectory.imported`)."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from raven.session.title import TITLE_STORAGE_MAX
from raven.trajectory import imported as imp
from raven.trajectory import imported_package as ipkg
from raven.trajectory import package_check as pcheck
from raven.trajectory.imported_package import ImportCancelledError, ImportLimits
from raven.trajectory.package_check import PackageError
from tests import imported_package_builders as builders
from tests.test_trajectory_imported_package import ATT, EXP, _export_entries, _layered, _raw, _report_tree

REG, DIR = tarfile.REGTYPE, tarfile.DIRTYPE


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "imports"


def _residue(root: Path) -> list[str]:
    """Everything under ``root`` except an empty staging directory."""
    if not root.exists():
        return []
    out = []
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if rel != imp.STAGING_DIR:
            out.append(rel)
    return out


def _tar_gz(entries: list[tuple[Any, ...]]) -> bytes:
    return gzip.compress(_raw(entries), mtime=0)


def _export_bytes(**manifest: Any) -> bytes:
    base = {"schema": "raven_trajectory_export", "format_version": 1, "export_id": EXP}
    return _tar_gz(_export_entries(manifest={**base, **manifest}))


def _report_bytes(
    *, session: bytes | None = None, manifest: dict[str, Any] | None = None, redaction: bytes = b"{}"
) -> bytes:
    tree = _report_tree(manifest=manifest)
    tree = [(n, k, redaction) if n.endswith("redaction.json") else (n, k, *rest) for n, k, *rest in tree]
    if session is not None:
        tree.append((f"{ATT}/session.jsonl", REG, session))
    return _tar_gz(tree)


def _session_record(title: str | None) -> bytes:
    rows = [{"_type": "metadata", "metadata": {"title": title} if title is not None else {}}]
    rows += [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


# ── keys ───────────────────────────────────────────────────────────────


def test_keys():
    cid = "0123456789abcdef0123456789abcdef"
    assert imp.key_of(cid) == f"imported:{cid}"
    assert imp.parse_key(f"imported:{cid}") == cid
    for bad in (
        "imported:../x",
        f"imported:{cid.upper()}",
        f"imported:{cid}0",
        "tui:abc",
        None,
        7,
        f"imported:{cid}\n",
    ):
        assert imp.parse_key(bad) is None
    assert imp.is_imported_key("imported:anything")
    assert not imp.is_imported_key("tui:imported:x")
    assert not imp.is_imported_key(None)


def test_get_never_leaves_the_imports_directory(root, tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "meta.json").write_text("{}", encoding="utf-8")
    assert imp.get("imported:../x", root) is None
    assert imp.get("imported:" + "a" * 32, root) is None


def test_imports_live_under_the_raven_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    assert imp.imports_root() == tmp_path / "home" / "imports"


# ── importing real packages ────────────────────────────────────────────


@pytest.mark.parametrize(
    "build,kind",
    [
        (builders.build_export, "export"),
        (builders.build_report, "report"),
        (builders.build_bug_report, "bugreport"),
    ],
)
def test_import_lays_out_the_conversation(tmp_path, root, build, kind):
    package, _ = build(tmp_path / "src")
    data = package.read_bytes()
    outcome = imp.import_bytes(data, f"/somewhere/{package.name}", root=root)
    assert outcome.status == imp.STATUS_IMPORTED
    conversation = outcome.conversation
    assert conversation.kind == kind
    assert conversation.key == imp.key_of(conversation.id)
    files = {p.name for p in conversation.path.iterdir()}
    assert files == {"meta.json", "package", "state", "session.jsonl"}
    assert (conversation.path / "state" / "logs" / "audit-spans.log").is_file()
    assert _residue(root) == [p for p in _residue(root) if p.startswith(conversation.id)]
    meta = conversation.meta
    assert meta["schema"] == imp.SCHEMA and meta["format_version"] == 1 and meta["id"] == conversation.id
    assert meta["source_name"] == package.name
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()
    assert meta["size_bytes"] == len(data)
    assert meta["title_auto"] is True
    assert meta["original"]["session_key"] == builders.SESSION
    assert meta["message_count"] == 4
    assert meta["transcript_included"] is True
    assert meta["imported_at"].endswith("+00:00")


def test_meta_of_an_export(tmp_path, root):
    package, _ = builders.build_export(tmp_path / "src")
    meta = imp.import_bytes(package.read_bytes(), package.name, root=root).conversation.meta
    assert meta["title"] == builders.TITLE
    assert meta["original"]["title"] == builders.TITLE
    assert meta["turns"] == 2
    assert meta["transcript_unmatched"] == []
    assert meta["missing"] == {"artifacts": 1, "messages": 0}
    assert meta["report"] is None
    assert meta["converted_shells"] == 0


def test_meta_of_a_bug_report(tmp_path, root):
    package, _ = builders.build_bug_report(tmp_path / "src", description="\n  The hotel   was never booked \nmore")
    meta = imp.import_bytes(package.read_bytes(), package.name, root=root).conversation.meta
    assert meta["title"] == "The hotel was never booked"
    assert meta["report"]["description_first_line"] == "The hotel was never booked"
    assert meta["report"]["severity"] == "high"
    assert meta["report"]["report_id"].startswith("br-")
    assert meta["original"]["title"] == builders.TITLE
    assert meta["turns"] is None
    assert meta["converted_shells"] == 1


def test_meta_of_a_trajectory_report(tmp_path, root):
    package, _ = builders.build_report(tmp_path / "src")
    meta = imp.import_bytes(package.read_bytes(), package.name, root=root).conversation.meta
    assert meta["title"] == builders.TITLE
    assert meta["original"]["created_at"]
    assert meta["report"] is None


# ── default titles ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "manifest,expected",
    [
        ({"source": {"title": "  Named\n  session "}, "turns": [{"input_preview": "first"}]}, "Named session"),
        ({"source": {"title": ""}, "turns": [{"input_preview": "first input"}]}, "first input"),
        ({"source": {"title": 7}, "turns": [{"input_preview": "  "}]}, EXP),
        ({"source": "nope", "turns": "nope"}, EXP),
    ],
)
def test_export_titles(root, manifest, expected):
    meta = imp.import_bytes(_export_bytes(**manifest), "x.tar.gz", root=root).conversation.meta
    assert meta["title"] == expected


@pytest.mark.parametrize(
    "session,manifest_attempt,expected",
    [
        (_session_record("Kept title"), ATT, "Kept title"),
        (_session_record(None), ATT, ATT),
        (None, "", ATT),
    ],
)
def test_report_titles(root, session, manifest_attempt, expected):
    manifest = {"format_version": 1, "attempt_id": manifest_attempt, "session_key": "tui:s"}
    meta = imp.import_bytes(_report_bytes(session=session, manifest=manifest), "r.tar.gz", root=root).conversation.meta
    assert meta["title"] == expected


def test_a_long_title_is_cut_to_the_storage_limit(root):
    meta = imp.import_bytes(_export_bytes(source={"title": "x" * 500}), "x.tar.gz", root=root).conversation.meta
    assert meta["title"] == "x" * TITLE_STORAGE_MAX


def test_bug_report_titles(tmp_path, root):
    package, _ = builders.build_bug_report(tmp_path / "src", description="  \n\n Second line wins\nthird")
    meta = imp.import_bytes(package.read_bytes(), package.name, root=root).conversation.meta
    assert meta["title"] == "Second line wins"


def test_odd_metadata_values_fall_back_instead_of_failing(root):
    manifest = {"format_version": 1, "attempt_id": ATT, "session_key": 5, "missing_artifacts": "x"}
    data = _report_bytes(manifest=manifest, redaction=b"[1]")
    meta = imp.import_bytes(data, "r.tar.gz", root=root).conversation.meta
    assert meta["original"]["session_key"] is None
    assert meta["original"]["created_at"] == ""
    assert meta["missing"] == {"artifacts": 0, "messages": 0}
    assert meta["message_count"] == 0
    assert meta["transcript_included"] is False


@pytest.mark.parametrize(
    "value,expected",
    [
        (7, []),
        (True, []),
        ("abc", []),
        ({"x": 1}, []),
        (None, []),
        (["trace-a", 1, None, {"x": 1}, "trace-b"], ["trace-a", "trace-b"]),
    ],
)
def test_transcript_unmatched_of_an_odd_type_falls_back(root, value, expected):
    meta = imp.import_bytes(_export_bytes(transcript_unmatched=value), "x.tar.gz", root=root).conversation.meta
    assert meta["transcript_unmatched"] == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("pkg.tar.gz", "pkg.tar.gz"),
        ("C:\\Users\\me\\pkg.tar.gz", "pkg.tar.gz"),
        ("/tmp/a/b.tgz", "b.tgz"),
        ("bad\x00\x1fname.tar.gz", "badname.tar.gz"),
        ("", imp.DEFAULT_SOURCE_NAME),
        ("dir/", imp.DEFAULT_SOURCE_NAME),
        (None, imp.DEFAULT_SOURCE_NAME),
    ],
)
def test_source_names_are_cleaned(name, expected):
    assert imp.clean_source_name(name) == expected


# ── refusals leave nothing behind ──────────────────────────────────────


@pytest.mark.parametrize(
    "data,code",
    [
        (b"", pcheck.CODE_NOT_GZIP_TAR),
        (b"not gzip at all", ipkg.CODE_NOT_IMPORTABLE),
        (_tar_gz([(EXP, DIR), (f"{EXP}/../x", REG, b"x")]), pcheck.CODE_BAD_MEMBER_NAME),
        (_tar_gz([(EXP, DIR), (f"{EXP}/l", tarfile.SYMTYPE, b"", "/etc")]), pcheck.CODE_BAD_MEMBER_TYPE),
        (_tar_gz([("root", DIR), ("root/x", REG, b"x")]), ipkg.CODE_NOT_IMPORTABLE),
        (_export_bytes(format_version=2), ipkg.CODE_UNSUPPORTED_VERSION),
    ],
    ids=["empty", "not-gzip", "dot-dot", "symlink", "not-importable", "unknown-version"],
)
def test_a_refused_package_leaves_no_residue(root, data, code):
    with pytest.raises(PackageError) as caught:
        imp.import_bytes(data, "x.tar.gz", root=root)
    assert caught.value.code == code
    assert _residue(root) == []
    assert imp.list_conversations(root) == []


def test_an_oversized_package_is_refused_before_anything_is_written(root):
    with pytest.raises(PackageError) as caught:
        imp.import_bytes(_export_bytes(), "x.tar.gz", root=root, limits=ImportLimits(max_package_bytes=10))
    assert caught.value.code == pcheck.CODE_TOO_LARGE
    assert not root.exists()


def test_a_layered_package_over_the_shared_budget_leaves_no_residue(tmp_path, root):
    path, _ = _layered(tmp_path)
    dest = tmp_path / "count"
    dest.mkdir()
    total = ipkg.unpack(path, dest).unpacked_bytes
    with pytest.raises(PackageError) as caught:
        imp.import_bytes(path.read_bytes(), "br.tar.gz", root=root, limits=ImportLimits(max_unpacked_export=total - 1))
    assert caught.value.code == pcheck.CODE_UNPACKED_TOO_LARGE
    assert _residue(root) == []
    assert imp.import_bytes(path.read_bytes(), "br.tar.gz", root=root).status == imp.STATUS_IMPORTED


def test_a_failure_while_normalizing_leaves_no_residue(root, monkeypatch):
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ipkg, "normalize", boom)
    with pytest.raises(OSError):
        imp.import_bytes(_export_bytes(), "x.tar.gz", root=root)
    assert _residue(root) == []


def test_a_stopped_import_leaves_no_residue(root):
    with pytest.raises(ImportCancelledError):
        imp.import_bytes(_export_bytes(), "x.tar.gz", root=root, should_stop=lambda: True)
    assert _residue(root) == []


# ── duplicates ─────────────────────────────────────────────────────────


def test_the_same_bytes_are_found_again(root):
    data = _export_bytes()
    first = imp.import_bytes(data, "a.tar.gz", root=root)
    again = imp.import_bytes(data, "b.tar.gz", root=root)
    assert again.status == imp.STATUS_DUPLICATE
    assert again.conversation.id == first.conversation.id
    assert len(imp.list_conversations(root)) == 1
    assert not any((root / imp.STAGING_DIR).iterdir())
    forced = imp.import_bytes(data, "c.tar.gz", root=root, force=True)
    assert forced.status == imp.STATUS_IMPORTED
    assert forced.conversation.id != first.conversation.id
    assert len(imp.list_conversations(root)) == 2
    assert imp.find_by_sha256(first.conversation.meta["sha256"], root) is not None


def test_different_bytes_are_not_duplicates(root):
    imp.import_bytes(_export_bytes(), "a.tar.gz", root=root)
    second = imp.import_bytes(_export_bytes(source={"title": "other"}), "a.tar.gz", root=root)
    assert second.status == imp.STATUS_IMPORTED


# ── listing, reading, renaming, deleting ───────────────────────────────


def test_list_is_newest_first_and_skips_what_is_not_an_import(root):
    first = imp.import_bytes(_export_bytes(source={"title": "one"}), "a", root=root).conversation
    time.sleep(0.01)
    second = imp.import_bytes(_export_bytes(source={"title": "two"}), "b", root=root).conversation
    (root / "not-hex").mkdir()
    (root / ("f" * 32)).mkdir()
    (root / ("e" * 32)).mkdir()
    (root / ("e" * 32) / "meta.json").write_text("{broken", encoding="utf-8")
    (root / ("d" * 32)).mkdir()
    (root / ("d" * 32) / "meta.json").write_text(json.dumps({"schema": imp.SCHEMA, "format_version": 1, "id": "x"}))
    os.symlink(first.path, root / ("c" * 32))
    assert [c.id for c in imp.list_conversations(root)] == [second.id, first.id]


def test_list_of_a_missing_directory_is_empty(root):
    assert imp.list_conversations(root) == []


def test_read_session_and_trajectory_target(tmp_path, root):
    package, source = builders.build_export(tmp_path / "src")
    conversation = imp.import_bytes(package.read_bytes(), package.name, root=root).conversation
    session = imp.read_session(conversation.key, root)
    assert session is not None
    assert [m["content"] for m in session.messages] == [m["content"] for m in source.messages]
    assert session.metadata["title"] == builders.TITLE
    state, key = imp.trajectory_target(conversation.key, root)
    assert state == conversation.path / "state"
    assert key == builders.SESSION
    assert imp.read_session("imported:" + "0" * 32, root) is None
    assert imp.trajectory_target("tui:x", root) is None


def test_rename(root):
    conversation = imp.import_bytes(_export_bytes(), "a", root=root).conversation
    renamed = imp.rename(conversation.key, "  My\n  analysis  ", root)
    assert renamed.title == "My analysis"
    again = imp.get(conversation.key, root)
    assert again is not None and again.title == "My analysis" and again.meta["title_auto"] is False
    assert not [p for p in conversation.path.iterdir() if p.name.endswith(".tmp")]
    with pytest.raises(ValueError):
        imp.rename(conversation.key, "   ", root)
    with pytest.raises(ValueError):
        imp.rename(conversation.key, "x" * (TITLE_STORAGE_MAX + 1), root)
    assert imp.rename(conversation.key, "x" * TITLE_STORAGE_MAX, root).title == "x" * TITLE_STORAGE_MAX
    with pytest.raises(LookupError):
        imp.rename("imported:" + "0" * 32, "x", root)


def test_delete(root):
    conversation = imp.import_bytes(_export_bytes(), "a", root=root).conversation
    assert imp.delete(conversation.key, root) is True
    assert not conversation.path.exists()
    assert imp.list_conversations(root) == []
    assert not [p for p in root.iterdir() if p.name.startswith(imp.TRASH_PREFIX)]
    assert imp.delete(conversation.key, root) is False
    assert imp.delete("imported:../../etc", root) is False


def test_sweep(root):
    staging = root / imp.STAGING_DIR
    old, new = staging / "old", staging / "new"
    old.mkdir(parents=True)
    new.mkdir()
    (staging / "stray-file").write_text("x", encoding="utf-8")
    trash = root / f"{imp.TRASH_PREFIX}{'a' * 32}"
    trash.mkdir()
    long_ago = time.time() - 25 * 3600
    os.utime(old, (long_ago, long_ago))
    os.utime(staging / "stray-file", (long_ago, long_ago))
    assert imp.sweep(root) == 3
    assert sorted(p.name for p in staging.iterdir()) == ["new"]
    assert not trash.exists()
    assert imp.sweep(root / "missing") == 0


# ── the runner ─────────────────────────────────────────────────────────


@pytest.fixture
def runner(root):
    made = imp.ImportRunner(root=lambda: root)
    yield made
    assert made.stop(timeout=10)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class _Gate:
    """Holds an import inside ``import_bytes`` until released, counting how many run at once."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()
        real = imp.import_bytes

        def held(*args: Any, **kwargs: Any) -> imp.ImportOutcome:
            with self._lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
            try:
                self.entered.set()
                assert self.release.wait(10)
                return real(*args, **kwargs)
            finally:
                with self._lock:
                    self.running -= 1

        monkeypatch.setattr(imp, "import_bytes", held)


def test_one_import_at_a_time(runner, root, monkeypatch):
    gate = _Gate(monkeypatch)
    data = _export_bytes()
    first = runner.try_submit(_b64(data), "a.tar.gz")
    assert gate.entered.wait(5)
    assert runner.busy
    with pytest.raises(imp.ImportBusyError):
        runner.try_submit(_b64(data), "a.tar.gz")
    gate.release.set()
    assert first.result(10).status == imp.STATUS_IMPORTED
    deadline = time.monotonic() + 5
    while runner.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not runner.busy
    second = runner.try_submit(_b64(data), "a.tar.gz")
    assert second.result(10).status == imp.STATUS_DUPLICATE
    assert gate.peak == 1


def test_the_slot_outlives_a_caller_that_stops_waiting(runner, root, monkeypatch):
    gate = _Gate(monkeypatch)
    data = _export_bytes()
    first = runner.try_submit(_b64(data), "a.tar.gz")
    assert gate.entered.wait(5)
    first.cancel()
    assert first.running()
    with pytest.raises(imp.ImportBusyError):
        runner.try_submit(_b64(data), "again.tar.gz")
    gate.release.set()
    assert first.result(10).status == imp.STATUS_IMPORTED
    assert len(imp.list_conversations(root)) == 1


@pytest.mark.parametrize(
    "content,code",
    [
        ("not base64 at all!", ipkg.CODE_BAD_ENCODING),
        (_b64(b"not a package"), ipkg.CODE_NOT_IMPORTABLE),
        (_b64(b"\x1f\x8b not gzip after all"), pcheck.CODE_NOT_GZIP_TAR),
    ],
)
def test_a_failed_import_releases_the_slot(runner, root, content, code):
    failed = runner.try_submit(content, "x.tar.gz")
    with pytest.raises(PackageError) as caught:
        failed.result(10)
    assert caught.value.code == code
    assert _residue(root) == []
    deadline = time.monotonic() + 5
    while runner.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    ok = runner.try_submit(_b64(_export_bytes()), "y.tar.gz")
    assert ok.result(10).status == imp.STATUS_IMPORTED


def test_an_unexpected_failure_is_handed_to_the_caller(runner, monkeypatch):
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(imp, "import_bytes", broken)
    with pytest.raises(RuntimeError):
        runner.try_submit(_b64(b"x"), "x").result(10)


def test_stop_calls_off_a_running_import_and_the_runner_goes_on(root, monkeypatch):
    runner = imp.ImportRunner(root=lambda: root)
    entered = threading.Event()
    real_unpack = ipkg.unpack

    def slow_unpack(package: Path, dest: Path, **kwargs: Any) -> ipkg.Unpacked:
        entered.set()
        should_stop = kwargs["should_stop"]
        deadline = time.monotonic() + 10
        while not should_stop() and time.monotonic() < deadline:
            time.sleep(0.01)
        return real_unpack(package, dest, **kwargs)

    monkeypatch.setattr(ipkg, "unpack", slow_unpack)
    future = runner.try_submit(_b64(_export_bytes()), "x.tar.gz")
    assert entered.wait(5)
    assert runner.stop(timeout=10) is True
    with pytest.raises(ImportCancelledError):
        future.result(1)
    assert _residue(root) == []
    assert not runner.busy
    monkeypatch.setattr(ipkg, "unpack", real_unpack)
    assert runner.try_submit(_b64(_export_bytes()), "x.tar.gz").result(10).status == imp.STATUS_IMPORTED


def test_a_slot_whose_import_outlives_stop_stays_taken_until_it_ends(root, monkeypatch):
    runner = imp.ImportRunner(root=lambda: root)
    gate = _Gate(monkeypatch)
    first = runner.try_submit(_b64(_export_bytes()), "x.tar.gz")
    assert gate.entered.wait(5)
    assert runner.stop(timeout=0.05) is False
    with pytest.raises(imp.ImportBusyError):
        runner.try_submit(_b64(_export_bytes()), "y.tar.gz")
    gate.release.set()
    with pytest.raises(ImportCancelledError):
        first.result(10)
    deadline = time.monotonic() + 5
    while runner.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runner.try_submit(_b64(_export_bytes()), "y.tar.gz").result(10).status == imp.STATUS_IMPORTED
    assert _residue(root) == [p for p in _residue(root) if not p.startswith(imp.STAGING_DIR + "/")]


def test_stop_without_an_import(root):
    runner = imp.ImportRunner(root=lambda: root)
    assert runner.stop(timeout=1) is True


def test_an_import_granted_before_its_thread_runs_survives_its_caller_leaving(root, monkeypatch):
    import asyncio

    runner = imp.ImportRunner(root=lambda: root)
    started = threading.Event()
    release = threading.Event()
    real_run = imp.ImportRunner._run

    def late_run(self: imp.ImportRunner, *args: Any) -> None:
        started.set()
        assert release.wait(10)
        real_run(self, *args)

    monkeypatch.setattr(imp.ImportRunner, "_run", late_run)
    future = runner.try_submit(_b64(_export_bytes()), "x.tar.gz")

    async def caller_leaves() -> None:
        waiting = asyncio.ensure_future(asyncio.wrap_future(future))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    asyncio.run(caller_leaves())
    assert started.wait(5)
    assert not future.cancelled()
    release.set()
    assert future.result(10).status == imp.STATUS_IMPORTED
    assert len(imp.list_conversations(root)) == 1
    assert runner.stop(timeout=10)


def test_a_thread_that_cannot_start_gives_the_slot_back(root, monkeypatch):
    runner = imp.ImportRunner(root=lambda: root)
    real_start = threading.Thread.start

    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    with pytest.raises(RuntimeError):
        runner.try_submit(_b64(_export_bytes()), "x.tar.gz")
    assert not runner.busy
    monkeypatch.setattr(threading.Thread, "start", real_start)
    assert runner.try_submit(_b64(_export_bytes()), "x.tar.gz").result(10).status == imp.STATUS_IMPORTED
    assert runner.stop(timeout=10) is True
