"""Tests for Bug Report Package inspection (`raven.trajectory.package_check`)."""

from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
import tracemalloc
from pathlib import Path

import pytest

from raven.trajectory import bugreport as breport
from raven.trajectory import package_check as pcheck
from raven.trajectory.package_check import PackageError, PackageLimits, inspect_package
from tests.test_trajectory_bugreport import _full_flow

ROOT = "br-20260904-abcdef"
OTHER_ID = "br-20260904-ffffff"
TRAJECTORY = "trajectory/att-x.tar.gz"
DEFAULT_FILES = {TRAJECTORY: b"not really a tarball"}


@pytest.fixture
def state(tmp_path, monkeypatch):
    state = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    return state


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture(autouse=True)
def _isolated_secrets(monkeypatch):
    monkeypatch.setattr(breport, "collect_known_secrets", lambda _p: ([], True))


def _header(name, size=0, kind=tarfile.REGTYPE):
    info = tarfile.TarInfo(name)
    info.size = size
    info.type = kind
    return info.tobuf(tarfile.GNU_FORMAT)


def _padded(data):
    return data + b"\0" * (-len(data) % 512)


def _pax_member(name, data, pax_headers):
    """A PAX-format member (extended header + file header + padded data)."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.pax_headers = dict(pax_headers)
    return info.tobuf(tarfile.PAX_FORMAT) + _padded(data)


END = b"\0" * 1024


def _tar(entries):
    """Raw tar bytes from `(name, kind, data)` triples, data padded, end marker appended."""
    out = b""
    for name, kind, data in entries:
        out += _header(name, len(data), kind) + _padded(data)
    return out + END


def _metadata(root=ROOT, files=None, **overrides):
    contents = [{"path": "bugreport.json", "sha256": "", "size_bytes": 0}]
    for rel, data in (DEFAULT_FILES if files is None else files).items():
        contents.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
    meta = {
        "schema": breport.PACKAGE_SCHEMA,
        "schema_version": 1,
        "report_id": root,
        "problem": {"description": "it broke"},
        "redaction": {"classification": "clean", "risk_accepted": False},
        "contents": contents,
    }
    meta.update(overrides)
    return meta


def _entries(root=ROOT, *, files=None, metadata=None, with_metadata=True):
    files = DEFAULT_FILES if files is None else files
    meta = _metadata(root, files) if metadata is None else metadata
    meta_bytes = meta if isinstance(meta, bytes) else json.dumps(meta).encode("utf-8")
    entries = [(root, tarfile.DIRTYPE, b"")]
    if with_metadata:
        entries.append((f"{root}/bugreport.json", tarfile.REGTYPE, meta_bytes))
    entries.append((f"{root}/trajectory", tarfile.DIRTYPE, b""))
    entries.extend((f"{root}/{rel}", tarfile.REGTYPE, data) for rel, data in files.items())
    return entries


def _write(tmp_path, data, name="pkg.tar.gz"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _package(tmp_path, entries=None, *, raw=None, name="pkg.tar.gz"):
    return _write(
        tmp_path, gzip.compress(_tar(_entries() if entries is None else entries) if raw is None else raw), name
    )


def _rejects(path, code, **kw):
    with pytest.raises(PackageError) as exc:
        inspect_package(path, **kw)
    assert exc.value.code == code, str(exc.value)
    return exc.value


# --- passing packages -------------------------------------------------------


def test_real_package_passes(state, workspace):
    _, record = _full_flow(state, workspace)
    package = Path(record["package"]["path"])
    summary = inspect_package(package, expected_report_id=record["report_id"])
    assert summary.report_id == record["report_id"]
    assert summary.notices == ()
    assert summary.members[0] == record["report_id"]
    assert f"{record['report_id']}/trajectory" in summary.members
    assert len(summary.metadata["contents"]) == 2
    assert inspect_package(package).report_id == record["report_id"]


def test_hand_built_package_passes(tmp_path):
    summary = inspect_package(_package(tmp_path))
    assert summary.report_id == ROOT
    assert summary.members == (ROOT, f"{ROOT}/bugreport.json", f"{ROOT}/trajectory", f"{ROOT}/{TRAJECTORY}")
    assert summary.notices == ()


def test_empty_and_oversized_files(tmp_path):
    _rejects(_write(tmp_path, b""), pcheck.CODE_NOT_GZIP_TAR)
    _rejects(_package(tmp_path), pcheck.CODE_TOO_LARGE, limits=PackageLimits(max_package_bytes=10))


# --- bounded decompression --------------------------------------------------


def _bomb(tmp_path, kind, name):
    declared = 32 * 2**20
    raw = _header(name, declared, kind) + b"\0" * declared + END
    return _package(tmp_path, raw=raw)


@pytest.mark.parametrize(
    "kind,name", [(tarfile.XHDTYPE, "./PaxHeaders/x"), (tarfile.GNUTYPE_LONGNAME, "././@LongLink")]
)
def test_huge_extended_header_is_refused_before_allocation(tmp_path, kind, name):
    path = _bomb(tmp_path, kind, name)
    assert path.stat().st_size < 100_000
    tracemalloc.start()
    try:
        _rejects(path, pcheck.CODE_HEADER_TOO_LARGE)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 2**20


def test_truncated_gzip(tmp_path):
    data = gzip.compress(_tar(_entries()))
    _rejects(_write(tmp_path, data[: len(data) // 2]), pcheck.CODE_NOT_GZIP_TAR)


def test_corrupt_gzip_trailer(tmp_path):
    data = bytearray(gzip.compress(_tar(_entries())))
    data[-5] ^= 0xFF
    error = _rejects(_write(tmp_path, bytes(data)), pcheck.CODE_NOT_GZIP_TAR)
    assert "invalid gzip data" in error.message


@pytest.mark.parametrize(
    "tail", [b"junk", gzip.compress(b"second member", mtime=0)], ids=["junk", "second-gzip-member"]
)
def test_trailing_data_after_gzip(tmp_path, tail):
    data = gzip.compress(_tar(_entries())) + tail
    error = _rejects(_write(tmp_path, data), pcheck.CODE_NOT_GZIP_TAR)
    assert "trailing data" in error.message


def test_member_declaring_more_than_it_carries(tmp_path):
    raw = _header(ROOT, 0, tarfile.DIRTYPE) + _header(f"{ROOT}/a", 1000) + b"x" * 100 + END
    _rejects(_package(tmp_path, raw=raw), pcheck.CODE_NOT_GZIP_TAR)
    _rejects(_package(tmp_path, raw=raw), pcheck.CODE_UNPACKED_TOO_LARGE, limits=PackageLimits(max_unpacked_bytes=1500))


def test_unpacked_budget_is_enforced_while_reading(tmp_path):
    entries = _entries(files={TRAJECTORY: b"x" * 8192})
    error = _rejects(
        _package(tmp_path, entries), pcheck.CODE_UNPACKED_TOO_LARGE, limits=PackageLimits(max_unpacked_bytes=4096)
    )
    assert "4096" in error.message


def test_skipping_padding_counts_against_the_budget(tmp_path):
    entries = _entries(files={TRAJECTORY: b"x" * 100})
    data_end = sum(512 + len(_padded(data)) for _, _, data in entries[:-1]) + 512 + 100
    inspect_package(_package(tmp_path, entries), limits=PackageLimits(max_unpacked_bytes=data_end + 2048))
    _rejects(
        _package(tmp_path, entries), pcheck.CODE_UNPACKED_TOO_LARGE, limits=PackageLimits(max_unpacked_bytes=data_end)
    )


# --- archive termination ----------------------------------------------------


def test_padding_truncated_inside_a_complete_gzip(tmp_path):
    raw = _header(ROOT, 0, tarfile.DIRTYPE) + _header(f"{ROOT}/a", 100) + b"x" * 100
    error = _rejects(_package(tmp_path, raw=raw), pcheck.CODE_NOT_GZIP_TAR)
    assert "padding" in error.message


def test_garbage_block_after_a_member(tmp_path):
    raw = _header(ROOT, 0, tarfile.DIRTYPE) + _header(f"{ROOT}/a", 100) + _padded(b"x" * 100) + b"G" * 512 + END
    error = _rejects(_package(tmp_path, raw=raw), pcheck.CODE_NOT_GZIP_TAR)
    assert "end-of-archive" in error.message


def test_data_after_the_end_marker(tmp_path):
    raw = _tar(_entries()) + b"Z" * 512
    error = _rejects(_package(tmp_path, raw=raw), pcheck.CODE_NOT_GZIP_TAR)
    assert "after the end-of-archive" in error.message


def test_missing_end_marker(tmp_path):
    raw = _tar(_entries())[: -len(END)]
    error = _rejects(_package(tmp_path, raw=raw), pcheck.CODE_NOT_GZIP_TAR)
    assert "end-of-archive" in error.message


# --- member rules -----------------------------------------------------------


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_special_members_are_refused(tmp_path, kind):
    entries = _entries() + [(f"{ROOT}/link", kind, b"")]
    _rejects(_package(tmp_path, entries), pcheck.CODE_BAD_MEMBER_TYPE)


def test_directory_with_data_is_refused_not_swallowed(tmp_path):
    good = _entries()
    raw = _header(ROOT, 512, tarfile.DIRTYPE) + b"D" * 512 + _tar(good[1:])[: -len(END)] + END
    error = _rejects(_package(tmp_path, raw=raw), pcheck.CODE_BAD_MEMBER_TYPE)
    assert "carries data" in error.message


@pytest.mark.parametrize("name", ["../x", "/abs", f"{ROOT}//b", f"{ROOT}/./b", "other-root/x", "not-an-id/x"])
def test_unsafe_member_names(tmp_path, name):
    entries = [(ROOT, tarfile.DIRTYPE, b""), (name, tarfile.REGTYPE, b"x")]
    if name.startswith("not-an-id"):
        entries = entries[1:]
    _rejects(_package(tmp_path, entries), pcheck.CODE_BAD_MEMBER_NAME)


def test_root_name_may_only_be_a_directory(tmp_path):
    _rejects(_package(tmp_path, [(ROOT, tarfile.REGTYPE, b"x")]), pcheck.CODE_BAD_MEMBER_NAME)
    assert inspect_package(_package(tmp_path, _entries())).members[0] == ROOT


def test_duplicate_members(tmp_path):
    _rejects(_package(tmp_path, [(ROOT, tarfile.DIRTYPE, b"")] + _entries()), pcheck.CODE_DUPLICATE_MEMBER)
    same = _entries() + [(f"{ROOT}/{TRAJECTORY}", tarfile.REGTYPE, b"not really a tarball")]
    _rejects(_package(tmp_path, same), pcheck.CODE_DUPLICATE_MEMBER)
    different = _entries() + [(f"{ROOT}/{TRAJECTORY}", tarfile.REGTYPE, b"other bytes")]
    _rejects(_package(tmp_path, different), pcheck.CODE_DUPLICATE_MEMBER)


def test_too_many_members(tmp_path):
    _rejects(_package(tmp_path), pcheck.CODE_TOO_MANY_MEMBERS, limits=PackageLimits(max_members=3))


# --- metadata ---------------------------------------------------------------


def test_metadata_missing(tmp_path):
    _rejects(_package(tmp_path, _entries(with_metadata=False)), pcheck.CODE_METADATA_MISSING)


def test_metadata_too_large(tmp_path):
    _rejects(_package(tmp_path), pcheck.CODE_METADATA_TOO_LARGE, limits=PackageLimits(max_metadata_bytes=16))


@pytest.mark.parametrize("payload", [b"\xff\xfe\x00", b"not json", b"[]"])
def test_metadata_invalid(tmp_path, payload):
    _rejects(_package(tmp_path, _entries(metadata=payload)), pcheck.CODE_METADATA_INVALID)


@pytest.mark.parametrize("overrides", [{"schema": "other"}, {"schema_version": 2}, {"schema_version": "1"}])
def test_schema_mismatch(tmp_path, overrides):
    _rejects(_package(tmp_path, _entries(metadata=_metadata(**overrides))), pcheck.CODE_SCHEMA_MISMATCH)


def test_report_id_mismatch(tmp_path):
    _rejects(_package(tmp_path, _entries(metadata=_metadata(report_id=OTHER_ID))), pcheck.CODE_REPORT_ID_MISMATCH)
    _rejects(_package(tmp_path, _entries(metadata=_metadata(report_id="nope"))), pcheck.CODE_REPORT_ID_MISMATCH)
    _rejects(_package(tmp_path), pcheck.CODE_REPORT_ID_MISMATCH, expected_report_id=OTHER_ID)


def _contents(**entry):
    base = {"path": TRAJECTORY, "sha256": "0" * 64, "size_bytes": 20}
    base.update(entry)
    return [{"path": "bugreport.json", "sha256": "", "size_bytes": 0}, base]


@pytest.mark.parametrize(
    "contents",
    [
        {},
        [{"path": "bugreport.json"}],
        _contents(size_bytes=True),
        _contents(path="../x"),
        _contents(sha256=None),
        _contents() + [_contents()[1]],
    ],
)
def test_contents_invalid(tmp_path, contents):
    _rejects(_package(tmp_path, _entries(metadata=_metadata(contents=contents))), pcheck.CODE_CONTENTS_INVALID)


def test_contents_mismatch(tmp_path):
    files = {TRAJECTORY: b"not really a tarball"}
    extra_in_manifest = _metadata(files={**files, "trajectory/other.bin": b"zz"})
    error = _rejects(_package(tmp_path, _entries(metadata=extra_in_manifest)), pcheck.CODE_CONTENTS_MISMATCH)
    assert "missing=['trajectory/other.bin']" in error.message
    extra_in_archive = _entries() + [(f"{ROOT}/trajectory/stray.bin", tarfile.REGTYPE, b"zz")]
    error = _rejects(_package(tmp_path, extra_in_archive), pcheck.CODE_CONTENTS_MISMATCH)
    assert "extra=['trajectory/stray.bin']" in error.message
    wrong_sha = _metadata(files=files)
    wrong_sha["contents"][1]["sha256"] = "0" * 64
    _rejects(_package(tmp_path, _entries(metadata=wrong_sha)), pcheck.CODE_CONTENTS_MISMATCH)
    wrong_size = _metadata(files=files)
    wrong_size["contents"][1]["size_bytes"] += 1
    _rejects(_package(tmp_path, _entries(metadata=wrong_size)), pcheck.CODE_CONTENTS_MISMATCH)


@pytest.mark.parametrize(
    "redaction",
    [
        {"classification": "needs_review", "risk_accepted": False},
        {"classification": "needs_review"},
        {"classification": "blocked", "risk_accepted": True},
        "not a block",
    ],
)
def test_risk_not_accepted(tmp_path, redaction):
    _rejects(_package(tmp_path, _entries(metadata=_metadata(redaction=redaction))), pcheck.CODE_RISK_NOT_ACCEPTED)


def test_reviewed_package_passes(tmp_path):
    metadata = _metadata(redaction={"classification": "needs_review", "risk_accepted": True})
    assert inspect_package(_package(tmp_path, _entries(metadata=metadata))).report_id == ROOT


def test_absolute_path_in_metadata_is_a_notice_not_a_rejection(tmp_path):
    metadata = _metadata(problem={"description": "see /Users/alice/x for details"})
    summary = inspect_package(_package(tmp_path, _entries(metadata=metadata)))
    assert summary.notices and summary.notices[0].startswith("absolute path in bugreport.json: /Users/alice/x")


def test_long_json_integer_is_metadata_invalid(tmp_path):
    error = _rejects(_package(tmp_path, _entries(metadata=b'{"x":' + b"1" * 5000 + b"}")), pcheck.CODE_METADATA_INVALID)
    assert isinstance(error, PackageError)


# --- sparse members ---------------------------------------------------------


def test_sparse_1_0_map_is_refused_before_it_is_read(tmp_path):
    pax = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.realsize": "0"}
    data = b"100000\n" + b"0\n0\n" * 100000
    raw = _header(ROOT, 0, tarfile.DIRTYPE) + _pax_member(f"{ROOT}/sparse", data, pax) + END
    path = _package(tmp_path, raw=raw)
    tracemalloc.start()
    try:
        error = _rejects(path, pcheck.CODE_BAD_MEMBER_TYPE, limits=PackageLimits(max_unpacked_bytes=2**20))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "sparse" in error.message
    assert peak < 8 * 2**20


def test_old_gnu_sparse_type_is_refused(tmp_path):
    raw = _header(ROOT, 0, tarfile.DIRTYPE) + _header(f"{ROOT}/s", 0, tarfile.GNUTYPE_SPARSE) + END
    error = _rejects(_package(tmp_path, raw=raw), pcheck.CODE_BAD_MEMBER_TYPE)
    assert "sparse" in error.message


@pytest.mark.parametrize(
    "pax",
    [
        {"GNU.sparse.size": "10", "GNU.sparse.numblocks": "1", "GNU.sparse.offset": "0", "GNU.sparse.numbytes": "10"},
        {"GNU.sparse.map": "0,10"},
        {"GNU.sparse.map": "nonsense"},
        {"GNU.sparse.realsize": "x"},
        {"GNU.sparse.name": "elsewhere"},
    ],
    ids=["0.0", "0.1", "0.1-nonsense", "realsize-only", "name-only"],
)
def test_pax_sparse_headers_are_refused(tmp_path, pax):
    raw = _header(ROOT, 0, tarfile.DIRTYPE) + _pax_member(f"{ROOT}/sparse", b"x" * 10, pax) + END
    error = _rejects(_package(tmp_path, raw=raw), pcheck.CODE_BAD_MEMBER_TYPE)
    assert "sparse" in error.message


def test_harmless_pax_headers_still_pass(tmp_path):
    pax = {"mtime": "1700000000.5", "comment": "written by a test"}
    raw = (
        _header(ROOT, 0, tarfile.DIRTYPE)
        + _tar(_entries()[1:3])[: -len(END)]
        + _pax_member(f"{ROOT}/{TRAJECTORY}", DEFAULT_FILES[TRAJECTORY], pax)
        + END
    )
    summary = inspect_package(_package(tmp_path, raw=raw))
    assert summary.members[-1] == f"{ROOT}/{TRAJECTORY}"


def test_tarfile_value_errors_become_package_errors(tmp_path, monkeypatch):
    def _boom(cls, *args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(pcheck._StrictTarInfo, "frombuf", classmethod(_boom))
    error = _rejects(_package(tmp_path), pcheck.CODE_NOT_GZIP_TAR)
    assert error.message == "boom"
