"""Tests for unpacking and normalizing an imported package (`raven.trajectory.imported_package`)."""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any

import pytest

from raven.tracing import artifact_v2
from raven.tracing.store import TraceStore
from raven.trajectory import imported_package as ipkg
from raven.trajectory import package_check as pcheck
from raven.trajectory.export import EXPORT_SCHEMA
from raven.trajectory.imported_package import ImportCancelledError, ImportLimits
from raven.trajectory.package_check import PackageError
from tests import imported_package_builders as builders
from tests.test_trajectory_package_check import _entries as _report_entries
from tests.test_trajectory_package_check import _metadata as _report_metadata

EXP = "ex-20261001-abc123"
ATT = "att-1"
REG, DIR = tarfile.REGTYPE, tarfile.DIRTYPE
END = b"\0" * 1024


def _block(name: str, data: bytes = b"", kind: bytes = REG, *, size: int | None = None, link: str = "") -> bytes:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if size is None else size
    info.linkname = link
    return info.tobuf(tarfile.GNU_FORMAT) + data + b"\0" * (-len(data) % 512)


def _raw(entries: list[tuple[Any, ...]]) -> bytes:
    out = b""
    for entry in entries:
        name, kind, data = entry[0], entry[1], entry[2] if len(entry) > 2 else b""
        link = entry[3] if len(entry) > 3 else ""
        out += _block(name, data, kind, size=0 if kind != REG and kind != DIR else None, link=link)
    return out + END


def _write(tmp_path: Path, raw: bytes, *, trailing: bytes = b"", name: str = "pkg.tar.gz") -> Path:
    path = tmp_path / name
    path.write_bytes(gzip.compress(raw + trailing, mtime=0))
    return path


def _export_entries(
    *, manifest: dict[str, Any] | None = None, spans: bytes = b"", extra: list[tuple[Any, ...]] = ()
) -> list[tuple[Any, ...]]:
    meta = manifest or {
        "schema": EXPORT_SCHEMA,
        "format_version": 1,
        "export_id": EXP,
        "source": {"session_key": "tui:s", "title": "T"},
        "turns": [],
    }
    return [
        (EXP, DIR),
        (f"{EXP}/export.json", REG, json.dumps(meta).encode()),
        (f"{EXP}/spans.jsonl", REG, spans),
        *extra,
    ]


def _report_tree(
    *, manifest: dict[str, Any] | None = None, spans: bytes = b"", extra: list[tuple[Any, ...]] = (), root: str = ATT
) -> list[tuple[Any, ...]]:
    meta = manifest or {"format_version": 1, "attempt_id": root, "session_key": "tui:s"}
    return [
        (root, DIR),
        (f"{root}/manifest.json", REG, json.dumps(meta).encode()),
        (f"{root}/redaction.json", REG, b"{}"),
        (f"{root}/spans.jsonl", REG, spans),
        *extra,
    ]


def _spans(*records: dict[str, Any]) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in records).encode()


def _unpack(tmp_path: Path, path: Path, **limits: Any) -> ipkg.Unpacked:
    dest = tmp_path / "unpacked"
    dest.mkdir(parents=True, exist_ok=True)
    return ipkg.unpack(path, dest, limits=ImportLimits(**limits))


def _refused(tmp_path: Path, path: Path, code: str, **limits: Any) -> PackageError:
    with pytest.raises(PackageError) as caught:
        _unpack(tmp_path, path, **limits)
    assert caught.value.code == code, str(caught.value)
    return caught.value


def _bug_report(tmp_path: Path, files: dict[str, bytes], name: str = "br.tar.gz") -> Path:
    root = "br-20260904-abcdef"
    entries = _report_entries(root, files=files, metadata=_report_metadata(root, files))
    raw = b"".join(_block(n, d, k) for n, k, d in entries) + END
    return _write(tmp_path, raw, name=name)


def _gz_bytes(raw: bytes) -> bytes:
    return gzip.compress(raw, mtime=0)


def _walk_bytes(data: bytes) -> pcheck.ArchiveStats:
    def drain(_member: pcheck.ArchiveMember, handle: Any) -> None:
        if handle is not None:
            while handle.read(64 * 1024):
                pass

    return pcheck.walk_archive(io.BytesIO(data), max_output=2**30, max_read=64 * 1024, max_members=1000, visit=drain)


# ── real packages ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "build,kind",
    [
        (builders.build_export, ipkg.KIND_EXPORT),
        (builders.build_report, ipkg.KIND_REPORT),
        (builders.build_bug_report, ipkg.KIND_BUGREPORT),
    ],
)
def test_real_packages_unpack_and_normalize(tmp_path, build, kind):
    package, _ = build(tmp_path / "src")
    unpacked = _unpack(tmp_path, package)
    assert unpacked.kind == kind
    assert unpacked.root.parent == tmp_path / "unpacked"
    assert (unpacked.bundle / "spans.jsonl").is_file()
    if kind == ipkg.KIND_BUGREPORT:
        assert unpacked.bundle.parent == unpacked.root / "trajectory"
        assert not list((unpacked.root / "trajectory").glob("*.tar.gz"))
    staging = tmp_path / "staging"
    normalized = ipkg.normalize(unpacked, staging)
    assert normalized.original_session_key == builders.SESSION
    assert normalized.transcript_included
    assert (staging / "session.jsonl").is_file()
    pointers = [
        value
        for line in (staging / "state" / "logs" / "audit-spans.log").read_text(encoding="utf-8").splitlines()
        for key, value in json.loads(line)["attributes"].items()
        if key.endswith(".artifact_path")
    ]
    assert pointers
    assert all(value is None or value.startswith("logs/audit-artifacts/") for value in pointers)
    for value in pointers:
        if value is not None:
            assert (staging / "state" / value).is_file()


def test_export_keeps_shells_and_moves_blobs_into_the_message_store(tmp_path):
    package, source = builders.build_export(tmp_path / "src", long_input=True)
    staging = tmp_path / "staging"
    ipkg.normalize(_unpack(tmp_path, package), staging)
    store = staging / "state" / "logs" / "audit-artifacts"
    shell = json.loads((store / "llm.input" / "2026-10-01" / "a-llm.json").read_text(encoding="utf-8"))
    assert artifact_v2.is_v2(shell)
    resolved = artifact_v2.resolve_payload(shell, store, on_missing=lambda sha: pytest.fail(f"blob {sha} missing"))
    assert resolved["messages"] == source.first_call
    assert resolved["systemPrompt"] == builders.SYSTEM_TEXT


def _transcript_size(unpacked: ipkg.Unpacked) -> tuple[int, int]:
    path = unpacked.bundle / ipkg.SESSION_FILE
    return path.stat().st_size, sum(1 for line in path.read_bytes().splitlines() if line.strip())


def test_a_session_record_at_its_limits_is_kept(tmp_path):
    package, _ = builders.build_export(tmp_path / "src")
    unpacked = _unpack(tmp_path, package)
    size, records = _transcript_size(unpacked)
    limits = ImportLimits(max_transcript_bytes=size, max_transcript_records=records)
    assert ipkg.normalize(unpacked, tmp_path / "staging", limits=limits).transcript_included
    assert (tmp_path / "staging" / ipkg.SESSION_FILE).read_bytes() == (unpacked.bundle / ipkg.SESSION_FILE).read_bytes()


@pytest.mark.parametrize("past", ["bytes", "records"])
def test_a_session_record_too_large_to_read_whole_is_refused(tmp_path, past):
    package, _ = builders.build_export(tmp_path / "src")
    unpacked = _unpack(tmp_path, package)
    size, records = _transcript_size(unpacked)
    limits = (
        ImportLimits(max_transcript_bytes=size - 1)
        if past == "bytes"
        else ImportLimits(max_transcript_records=records - 1)
    )
    with pytest.raises(PackageError) as caught:
        ipkg.normalize(unpacked, tmp_path / "staging", limits=limits)
    assert caught.value.code == ipkg.CODE_TRANSCRIPT_TOO_LARGE
    assert not (tmp_path / "staging" / ipkg.SESSION_FILE).exists()


def test_blank_lines_are_not_records(tmp_path):
    package, _ = builders.build_export(tmp_path / "src")
    unpacked = _unpack(tmp_path, package)
    path = unpacked.bundle / ipkg.SESSION_FILE
    _, records = _transcript_size(unpacked)
    path.write_bytes(path.read_bytes() + b"\n \n\n")
    limits = ImportLimits(max_transcript_records=records)
    assert ipkg.normalize(unpacked, tmp_path / "staging", limits=limits).transcript_included


def test_export_pointer_to_an_unexported_file_becomes_null(tmp_path):
    package, _ = builders.build_export(tmp_path / "src")
    staging = tmp_path / "staging"
    ipkg.normalize(_unpack(tmp_path, package), staging)
    lines = (staging / "state" / "logs" / "audit-spans.log").read_text(encoding="utf-8").splitlines()
    tool = next(json.loads(line) for line in lines if json.loads(line)["spanId"] == "span-a2")
    assert tool["attributes"]["tool.input.artifact_path"] is None
    assert tool["attributes"]["tool.output.artifact_path"] == "logs/audit-artifacts/tool.output/2026-10-01/a-tool.json"


@pytest.mark.parametrize("build", [builders.build_report, builders.build_bug_report])
def test_report_model_inputs_become_shells_that_resolve_to_the_original(tmp_path, build):
    package, source = build(tmp_path / "src", long_input=True)
    unpacked = _unpack(tmp_path, package)
    original = json.loads((unpacked.bundle / "artifacts" / "a-llm.json").read_text(encoding="utf-8"))
    assert not artifact_v2.is_v2(original)
    staging = tmp_path / "staging"
    normalized = ipkg.normalize(unpacked, staging)
    assert normalized.converted_shells == 1
    store = staging / "state" / "logs" / "audit-artifacts"
    shell = json.loads((store / "a-llm.json").read_text(encoding="utf-8"))
    assert artifact_v2.is_v2(shell)
    assert artifact_v2.ref_sha1(shell["systemPrompt"]) is not None
    assert artifact_v2.ref_sha1(shell["prompt"]) is not None
    assert artifact_v2.resolve_payload(shell, store) == original
    assert original["messages"] == source.first_call


def test_conversion_is_skipped_past_the_size_limit(tmp_path):
    package, _ = builders.build_report(tmp_path / "src")
    staging = tmp_path / "staging"
    normalized = ipkg.normalize(_unpack(tmp_path, package), staging, limits=ImportLimits(max_convert_bytes=10))
    assert (normalized.converted_shells, normalized.skipped_shells) == (0, 1)
    shell = json.loads((staging / "state" / "logs" / "audit-artifacts" / "a-llm.json").read_text(encoding="utf-8"))
    assert not artifact_v2.is_v2(shell)


def test_shell_from_v1_keeps_text_that_is_not_its_message(tmp_path):
    store = TraceStore(tmp_path / "state")
    messages = [
        {"role": "system", "content": ""},
        {"role": "system", "content": "the real prompt"},
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    ]
    payload = {"provider": "p", "systemPrompt": "the real prompt", "prompt": "something else", "messages": messages}
    shell = ipkg.shell_from_v1(payload, store)
    assert shell is not None
    assert list(shell) == ["artifactFormat", "provider", "systemPrompt", "prompt", "messages"]
    assert shell["systemPrompt"] == "the real prompt"
    assert shell["prompt"] == "something else"
    assert artifact_v2.resolve_payload(shell, tmp_path / "state" / "logs" / "audit-artifacts") == payload


def test_shell_from_v1_aliases_multimodal_prompts(tmp_path):
    store = TraceStore(tmp_path / "state")
    content = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:x"}}]
    payload = {"messages": [{"role": "user", "content": content}], "prompt": artifact_v2.coerce_text(content)}
    shell = ipkg.shell_from_v1(payload, store)
    assert shell is not None and artifact_v2.ref_sha1(shell["prompt"]) is not None
    assert artifact_v2.resolve_payload(shell, tmp_path / "state" / "logs" / "audit-artifacts") == payload


@pytest.mark.parametrize(
    "payload",
    [None, "text", [], {"messages": "nope"}, {"artifactFormat": "audit.artifact.v2", "messages": []}],
)
def test_shell_from_v1_leaves_anything_else_alone(tmp_path, payload):
    assert ipkg.shell_from_v1(payload, TraceStore(tmp_path / "state")) is None


def test_report_artifacts_that_are_not_json_are_linked_unchanged(tmp_path):
    spans = _spans({"spanId": "s", "traceId": "t", "attributes": {"llm.input.artifact_path": "artifacts/in.txt"}})
    path = _write(tmp_path, _raw(_report_tree(spans=spans, extra=[(f"{ATT}/artifacts/in.txt", REG, b"not json")])))
    staging = tmp_path / "staging"
    normalized = ipkg.normalize(_unpack(tmp_path, path), staging)
    assert normalized.converted_shells == 0
    assert (staging / "state" / "logs" / "audit-artifacts" / "in.txt").read_bytes() == b"not json"


def test_pointers_that_map_to_nothing_become_null(tmp_path):
    attrs = {
        "a.artifact_path": None,
        "b.artifact_path": "/home/someone/.raven/traces/logs/audit-artifacts/x.json",
        "c.artifact_path": "artifacts/absent.json",
        "d.artifact_path": "artifacts/../escape.json",
        "e.artifact_path": "artifacts/kept.json",
        "f.artifact_path": 7,
        "session.key": "tui:s",
    }
    spans = _spans({"spanId": "s", "traceId": "t", "attributes": attrs})
    path = _write(tmp_path, _raw(_report_tree(spans=spans, extra=[(f"{ATT}/artifacts/kept.json", REG, b"{}")])))
    staging = tmp_path / "staging"
    ipkg.normalize(_unpack(tmp_path, path), staging)
    record = json.loads((staging / "state" / "logs" / "audit-spans.log").read_text(encoding="utf-8"))
    out = record["attributes"]
    assert [out[f"{k}.artifact_path"] for k in "abcdf"] == [None] * 5
    assert out["e.artifact_path"] == "logs/audit-artifacts/kept.json"


def test_span_lines_that_are_not_records_are_kept(tmp_path):
    spans = b'not json\n[1, 2]\n\n{"spanId": "s", "attributes": "broken"}\n'
    path = _write(tmp_path, _raw(_report_tree(spans=spans)))
    staging = tmp_path / "staging"
    ipkg.normalize(_unpack(tmp_path, path), staging)
    lines = (staging / "state" / "logs" / "audit-spans.log").read_bytes().splitlines()
    assert lines == [b"not json", b"[1, 2]", b'{"spanId": "s", "attributes": "broken"}']


def test_an_oversized_span_line_is_copied_through_unparsed(tmp_path, monkeypatch):
    monkeypatch.setattr(ipkg, "MAX_LINE_BYTES", 100)
    big = json.dumps({"spanId": "big", "attributes": {"x.artifact_path": "artifacts/a", "pad": "y" * 200}}).encode()
    small = json.dumps({"spanId": "s", "attributes": {"x.artifact_path": "artifacts/a"}}).encode()
    path = _write(tmp_path, _raw(_report_tree(spans=big + b"\n" + small, extra=[(f"{ATT}/artifacts/a", REG, b"1")])))
    staging = tmp_path / "staging"
    ipkg.normalize(_unpack(tmp_path, path), staging)
    lines = (staging / "state" / "logs" / "audit-spans.log").read_bytes().splitlines()
    assert lines[0] == big
    assert json.loads(lines[1])["attributes"]["x.artifact_path"] == "logs/audit-artifacts/a"


@pytest.mark.parametrize(
    "manifest_key,spans,expected",
    [
        ("tui:declared", b"", "tui:declared"),
        (None, _spans({"attributes": {"session.key": ""}}, {"attributes": {"session.key": "tui:span"}}), "tui:span"),
        (None, b"", None),
    ],
)
def test_the_original_session_key(tmp_path, manifest_key, spans, expected):
    manifest = {"format_version": 1, "attempt_id": ATT, "session_key": manifest_key}
    path = _write(tmp_path, _raw(_report_tree(manifest=manifest, spans=spans)))
    assert ipkg.normalize(_unpack(tmp_path, path), tmp_path / "staging").original_session_key == expected


def test_a_package_without_a_session_record_says_so(tmp_path):
    path = _write(tmp_path, _raw(_export_entries()))
    staging = tmp_path / "staging"
    normalized = ipkg.normalize(_unpack(tmp_path, path), staging)
    assert normalized.original_session_key == "tui:s"
    assert not normalized.transcript_included
    assert not (staging / "session.jsonl").exists()


@pytest.mark.parametrize(
    "extra",
    [
        [(f"{EXP}/messages/ab/{'a' * 40}.json", REG, b"{}")],
        [(f"{EXP}/messages/{'b' * 40}.json", REG, b"{}")],
        [(f"{EXP}/messages/bb/{'B' * 40}.json", REG, b"{}")],
        [(f"{EXP}/artifacts/_messages/aa/x.json", REG, b"{}")],
        [(f"{EXP}/artifacts/_Messages/aa/x.json", REG, b"{}")],
        [(f"{EXP}/artifacts/_MESSAGES/aa/x.json", REG, b"{}")],
        [(f"{EXP}/artifacts/_blobs/x", REG, b"{}")],
        [(f"{EXP}/artifacts/_Blobs/x", REG, b"{}")],
    ],
)
def test_names_that_collide_with_the_message_store_are_refused(tmp_path, extra):
    path = _write(tmp_path, _raw(_export_entries(extra=extra)))
    with pytest.raises(PackageError) as caught:
        ipkg.normalize(_unpack(tmp_path, path), tmp_path / "staging")
    assert caught.value.code == pcheck.CODE_BAD_MEMBER_NAME


def test_a_well_named_blob_is_placed(tmp_path):
    message = {"role": "user", "content": "hi"}
    sha1 = artifact_v2.message_sha1(message)
    blob = (f"{EXP}/messages/{sha1[:2]}/{sha1}.json", REG, artifact_v2.message_text(message).encode())
    path = _write(tmp_path, _raw(_export_entries(extra=[blob])))
    staging = tmp_path / "staging"
    ipkg.normalize(_unpack(tmp_path, path), staging)
    assert artifact_v2.message_path(staging / "state" / "logs" / "audit-artifacts", sha1).is_file()


# ── refusals ───────────────────────────────────────────────────────────


def test_empty_and_oversized_packages(tmp_path):
    empty = tmp_path / "empty.tar.gz"
    empty.write_bytes(b"")
    _refused(tmp_path, empty, pcheck.CODE_NOT_GZIP_TAR)
    _refused(tmp_path, _write(tmp_path, _raw(_export_entries())), pcheck.CODE_TOO_LARGE, max_package_bytes=10)


@pytest.mark.parametrize(
    "data",
    [
        gzip.compress(b"gzip but not a tar archive" * 40, mtime=0),
        gzip.compress(_raw([]), mtime=0)[:-12],
        b"\x1f\x8b",
    ],
    ids=["gzip-not-tar", "truncated-gzip", "magic-only"],
)
def test_not_a_gzip_tar(tmp_path, data):
    path = tmp_path / "bad.tar.gz"
    path.write_bytes(data)
    _refused(tmp_path, path, pcheck.CODE_NOT_GZIP_TAR)


@pytest.mark.parametrize(
    "data",
    [b"plain text, not gzip", b"\x1f", b"PK\x03\x04 a zip file", b"\x8b\x1f the magic reversed"],
    ids=["plain-text", "one-byte", "zip", "reversed-magic"],
)
def test_a_file_that_is_not_gzip_is_not_importable(tmp_path, data):
    path = tmp_path / "picked-by-mistake.txt"
    path.write_bytes(data)
    _refused(tmp_path, path, ipkg.CODE_NOT_IMPORTABLE)


def test_a_truncated_member_is_refused(tmp_path):
    raw = _block(f"{EXP}/export.json", b"x" * 100, size=4096)
    path = tmp_path / "short.tar.gz"
    path.write_bytes(gzip.compress(raw, mtime=0))
    _refused(tmp_path, path, pcheck.CODE_NOT_GZIP_TAR)


@pytest.mark.parametrize(
    "entries,code",
    [
        ([(EXP, DIR), (f"{EXP}/../escape", REG, b"x")], pcheck.CODE_BAD_MEMBER_NAME),
        ([("/etc/passwd", REG, b"x")], pcheck.CODE_BAD_MEMBER_NAME),
        ([(EXP, DIR), (f"{EXP}\\win", REG, b"x")], pcheck.CODE_BAD_MEMBER_NAME),
        ([(EXP, DIR), (f"{EXP}/./x", REG, b"x")], pcheck.CODE_BAD_MEMBER_NAME),
        ([(EXP, DIR), (f"{EXP}/link", tarfile.SYMTYPE, b"", "/etc/passwd")], pcheck.CODE_BAD_MEMBER_TYPE),
        ([(EXP, DIR), (f"{EXP}/hard", tarfile.LNKTYPE, b"", f"{EXP}/export.json")], pcheck.CODE_BAD_MEMBER_TYPE),
        ([(EXP, DIR), (f"{EXP}/dev", tarfile.CHRTYPE)], pcheck.CODE_BAD_MEMBER_TYPE),
        ([(EXP, DIR), (f"{EXP}/fifo", tarfile.FIFOTYPE)], pcheck.CODE_BAD_MEMBER_TYPE),
        ([(EXP, DIR), (f"{EXP}/a", REG, b"1"), (f"{EXP}/a", REG, b"2")], pcheck.CODE_DUPLICATE_MEMBER),
        ([(EXP, DIR), ("other/x", REG, b"x")], pcheck.CODE_BAD_MEMBER_NAME),
        ([("lonely-file", REG, b"x")], pcheck.CODE_BAD_MEMBER_NAME),
    ],
)
def test_unsafe_members_are_refused(tmp_path, entries, code):
    _refused(tmp_path, _write(tmp_path, _raw(entries)), code)


def test_a_directory_with_data_is_refused(tmp_path):
    raw = _block(EXP, b"", DIR) + _block(f"{EXP}/d", b"x" * 10, DIR) + END
    _refused(tmp_path, _write(tmp_path, raw), pcheck.CODE_BAD_MEMBER_TYPE)


def test_a_huge_extended_header_is_refused(tmp_path):
    raw = _block("././@LongLink", b"", tarfile.GNUTYPE_LONGNAME, size=1024 * 1024) + b"\0" * (1024 * 1024) + END
    _refused(tmp_path, _write(tmp_path, raw), pcheck.CODE_HEADER_TOO_LARGE)


def test_a_long_chain_of_extended_headers_is_refused(tmp_path):
    chain = _block("././@LongLink", b"././@LongLink", tarfile.GNUTYPE_LONGNAME) * 50
    _refused(tmp_path, _write(tmp_path, chain + _raw(_export_entries())), pcheck.CODE_HEADER_CHAIN_TOO_LONG)


def test_too_many_members(tmp_path):
    extra = [(f"{EXP}/f{i}", REG, b"x") for i in range(10)]
    _refused(
        tmp_path, _write(tmp_path, _raw(_export_entries(extra=extra))), pcheck.CODE_TOO_MANY_MEMBERS, max_members=5
    )


def test_expanding_past_the_export_limit(tmp_path):
    extra = [(f"{EXP}/big", REG, b"\0" * 200_000)]
    path = _write(tmp_path, _raw(_export_entries(extra=extra)))
    assert path.stat().st_size < 5_000
    _refused(tmp_path, path, pcheck.CODE_UNPACKED_TOO_LARGE, max_unpacked_export=100_000)


def test_a_report_past_the_report_limit(tmp_path):
    path = _write(tmp_path, _raw(_report_tree(extra=[(f"{ATT}/artifacts/big", REG, b"\0" * 50_000)])))
    unpacked = _unpack(tmp_path, path)
    _refused(tmp_path / "again", path, pcheck.CODE_UNPACKED_TOO_LARGE, max_unpacked_report=unpacked.unpacked_bytes - 1)


def test_an_export_of_the_same_size_is_held_to_the_export_limit_only(tmp_path):
    extra = [(f"{EXP}/big", REG, b"\0" * 50_000)]
    path = _write(tmp_path, _raw(_export_entries(extra=extra)))
    assert _unpack(tmp_path, path, max_unpacked_report=1_000).kind == ipkg.KIND_EXPORT


@pytest.mark.parametrize("version", [2, True, "1", None])
def test_an_unknown_export_version(tmp_path, version):
    manifest = {"schema": EXPORT_SCHEMA, "format_version": version, "export_id": EXP}
    _refused(tmp_path, _write(tmp_path, _raw(_export_entries(manifest=manifest))), ipkg.CODE_UNSUPPORTED_VERSION)


@pytest.mark.parametrize("version", [2, True, None])
def test_an_unknown_bundle_version(tmp_path, version):
    manifest = {"format_version": version, "attempt_id": ATT}
    _refused(tmp_path, _write(tmp_path, _raw(_report_tree(manifest=manifest))), ipkg.CODE_UNSUPPORTED_VERSION)


@pytest.mark.parametrize(
    "manifest",
    [
        {"schema": EXPORT_SCHEMA, "format_version": 1, "export_id": "ex-20261001-ffffff"},
        {"schema": EXPORT_SCHEMA, "format_version": 1},
    ],
)
def test_an_export_id_that_does_not_name_the_root(tmp_path, manifest):
    _refused(tmp_path, _write(tmp_path, _raw(_export_entries(manifest=manifest))), ipkg.CODE_MANIFEST_INVALID)


def test_an_export_without_spans(tmp_path):
    entries = [e for e in _export_entries() if not e[0].endswith("spans.jsonl")]
    _refused(tmp_path, _write(tmp_path, _raw(entries)), ipkg.CODE_MANIFEST_INVALID)


@pytest.mark.parametrize("body", [b"[1]", b"not json", b"\xff\xfe"])
def test_metadata_that_is_not_a_json_object(tmp_path, body):
    entries = [(ATT, DIR), (f"{ATT}/manifest.json", REG, body), (f"{ATT}/redaction.json", REG, b"{}")]
    _refused(tmp_path, _write(tmp_path, _raw(entries)), ipkg.CODE_MANIFEST_INVALID)


def test_oversized_metadata(tmp_path):
    entries = _export_entries()
    _refused(tmp_path, _write(tmp_path, _raw(entries)), ipkg.CODE_MANIFEST_INVALID, max_metadata_bytes=10)


@pytest.mark.parametrize(
    "entries",
    [
        [("root", DIR), ("root/readme.txt", REG, b"hello")],
        [("root", DIR), ("root/export.json", REG, json.dumps({"schema": "something_else"}).encode())],
        [("root", DIR), ("root/manifest.json", REG, b"{}")],
        [("root", DIR)],
    ],
)
def test_not_importable(tmp_path, entries):
    _refused(tmp_path, _write(tmp_path, _raw(entries)), ipkg.CODE_NOT_IMPORTABLE)


def test_a_bug_report_failing_its_own_check_keeps_its_code(tmp_path):
    package, _ = builders.build_bug_report(tmp_path / "src")
    with tarfile.open(package, "r:gz") as tar:
        members = [(m, tar.extractfile(m).read() if m.isfile() else b"") for m in tar.getmembers()]
    raw = b""
    for member, data in members:
        if member.name.endswith(".tar.gz"):
            data = data + b"tampered"
            member.size = len(data)
        raw += member.tobuf(tarfile.GNU_FORMAT) + data + b"\0" * (-len(data) % 512)
    path = _write(tmp_path, raw + END, name="tampered.tar.gz")
    _refused(tmp_path, path, pcheck.CODE_CONTENTS_MISMATCH)


@pytest.mark.parametrize(
    "files",
    [
        {"notes.txt": b"no trajectory"},
        {"trajectory/a.tar.gz": b"one", "trajectory/b.tar.gz": b"two"},
    ],
)
def test_a_bug_report_must_embed_exactly_one_trajectory(tmp_path, files):
    _refused(tmp_path, _bug_report(tmp_path, files), ipkg.CODE_NOT_IMPORTABLE)


@pytest.mark.parametrize(
    "inner,code",
    [
        ([(ATT, DIR), (f"{ATT}/link", tarfile.SYMTYPE, b"", "/etc/passwd")], pcheck.CODE_BAD_MEMBER_TYPE),
        ([(ATT, DIR), (f"{ATT}/../../escape", REG, b"x")], pcheck.CODE_BAD_MEMBER_NAME),
        ([(ATT, DIR), ("other/x", REG, b"x")], pcheck.CODE_BAD_MEMBER_NAME),
    ],
)
def test_a_malicious_member_inside_the_embedded_trajectory(tmp_path, inner, code):
    path = _bug_report(tmp_path, {"trajectory/att-1.tar.gz": _gz_bytes(_raw(inner))})
    _refused(tmp_path, path, code)
    assert not (tmp_path / "escape").exists()


def test_the_embedded_trajectory_must_be_a_bundle(tmp_path):
    inner = [(ATT, DIR), (f"{ATT}/readme.txt", REG, b"x")]
    _refused(
        tmp_path, _bug_report(tmp_path, {"trajectory/att-1.tar.gz": _gz_bytes(_raw(inner))}), ipkg.CODE_MANIFEST_INVALID
    )


# ── the budget both layers share ───────────────────────────────────────


def _layered(tmp_path: Path, *, trailing: bytes = b"", name: str = "br.tar.gz") -> tuple[Path, bytes]:
    inner = _gz_bytes(_raw(_report_tree(extra=[(f"{ATT}/artifacts/a.txt", REG, b"a" * 3000)])))
    root = "br-20260904-abcdef"
    files = {"trajectory/att-1.tar.gz": inner}
    entries = _report_entries(root, files=files, metadata=_report_metadata(root, files))
    raw = b"".join(_block(n, d, k) for n, k, d in entries) + END
    path = tmp_path / name
    path.write_bytes(gzip.compress(raw + trailing, mtime=0))
    return path, inner


def test_a_walk_counts_everything_the_stream_produced(tmp_path):
    raw = _raw(_export_entries())
    plain = _walk_bytes(gzip.compress(raw, mtime=0))
    padded = _walk_bytes(gzip.compress(raw + b"\0" * 8192, mtime=0))
    assert plain.unpacked_bytes == len(raw)
    assert padded.unpacked_bytes == len(raw) + 8192
    assert plain.members == padded.members == 3


def test_both_layers_spend_one_budget_counted_with_headers_and_padding(tmp_path):
    path, inner = _layered(tmp_path)
    total = _unpack(tmp_path / "probe", path).unpacked_bytes
    outer_stats = _walk_bytes(path.read_bytes())
    inner_stats = _walk_bytes(inner)
    assert total == outer_stats.unpacked_bytes + inner_stats.unpacked_bytes
    with tarfile.open(path, "r:gz") as tar:
        outer_content = sum(m.size for m in tar.getmembers())
    with tarfile.open(fileobj=io.BytesIO(inner), mode="r:gz") as tar:
        inner_content = sum(m.size for m in tar.getmembers())
    limit = total - 1
    assert outer_content + inner_content < limit
    _refused(tmp_path / "over", path, pcheck.CODE_UNPACKED_TOO_LARGE, max_unpacked_export=limit)
    assert _unpack(tmp_path / "fits", path, max_unpacked_export=total).kind == ipkg.KIND_BUGREPORT


def test_padding_after_the_outer_end_marker_leaves_less_for_the_inner_layer(tmp_path):
    plain, _ = _layered(tmp_path, name="plain.tar.gz")
    padded, inner = _layered(tmp_path, trailing=b"\0" * 2048, name="padded.tar.gz")
    assert _walk_bytes(padded.read_bytes()).unpacked_bytes == _walk_bytes(plain.read_bytes()).unpacked_bytes + 2048
    assert _walk_bytes(inner).unpacked_bytes > 2048
    total = _unpack(tmp_path / "probe", plain).unpacked_bytes
    assert _unpack(tmp_path / "plain", plain, max_unpacked_export=total).kind == ipkg.KIND_BUGREPORT
    _refused(tmp_path / "padded", padded, pcheck.CODE_UNPACKED_TOO_LARGE, max_unpacked_export=total)


def test_padding_counts_toward_the_report_limit(tmp_path):
    raw = _raw(_report_tree())
    plain = _write(tmp_path, raw, name="plain.tar.gz")
    padded = _write(tmp_path, raw, trailing=b"\0" * 10_240, name="padded.tar.gz")
    total = _unpack(tmp_path / "probe", plain).unpacked_bytes
    assert _unpack(tmp_path / "plain", plain, max_unpacked_report=total).kind == ipkg.KIND_REPORT
    _refused(tmp_path / "padded", padded, pcheck.CODE_UNPACKED_TOO_LARGE, max_unpacked_report=total)


# ── stopping ───────────────────────────────────────────────────────────


def test_unpack_stops_when_asked(tmp_path):
    path = _write(tmp_path, _raw(_export_entries()))
    dest = tmp_path / "unpacked"
    dest.mkdir()
    with pytest.raises(ImportCancelledError):
        ipkg.unpack(path, dest, should_stop=lambda: True)


def test_normalize_stops_when_asked(tmp_path):
    path = _write(tmp_path, _raw(_report_tree(spans=_spans({"spanId": "s", "attributes": {}}))))
    with pytest.raises(ImportCancelledError):
        ipkg.normalize(_unpack(tmp_path, path), tmp_path / "staging", should_stop=lambda: True)


def test_the_package_limit_matches_the_upload_channel():
    from raven.rpc.files import MAX_UPLOAD_BYTES

    assert ipkg.MAX_PACKAGE_BYTES == MAX_UPLOAD_BYTES == ImportLimits().max_package_bytes


def test_a_case_variant_of_the_message_store_cannot_rewrite_the_package(tmp_path):
    """The artifact a case-insensitive file system would fold onto a blob is refused before either lands."""
    message = {"role": "user", "content": "blob"}
    sha1 = artifact_v2.message_sha1(message)
    extra = [
        (f"{EXP}/artifacts/_Messages/{sha1[:2]}/{sha1}.json", REG, b'{"content":"artifact"}'),
        (f"{EXP}/messages/{sha1[:2]}/{sha1}.json", REG, artifact_v2.message_text(message).encode()),
    ]
    path = _write(tmp_path, _raw(_export_entries(extra=extra)))
    unpacked = _unpack(tmp_path, path)
    original = unpacked.root / "artifacts" / "_Messages" / sha1[:2] / f"{sha1}.json"
    with pytest.raises(PackageError) as caught:
        ipkg.normalize(unpacked, tmp_path / "staging")
    assert caught.value.code == pcheck.CODE_BAD_MEMBER_NAME
    assert original.read_bytes() == b'{"content":"artifact"}'


@pytest.mark.parametrize(
    "pointer", ["artifacts/_Messages/aa/x.json", "artifacts/_MESSAGES/aa/x.json", "artifacts/_BLOBS/x"]
)
def test_a_pointer_into_a_case_variant_of_a_store_maps_to_nothing(tmp_path, pointer):
    artifacts = tmp_path / "artifacts"
    target = artifacts.joinpath(*pointer.split("/")[1:])
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    assert ipkg._artifact_rel(pointer, artifacts) is None
    assert ipkg._artifact_rel("artifacts/kind/ok.json", artifacts) is None
    (artifacts / "kind").mkdir()
    (artifacts / "kind" / "ok.json").write_text("{}", encoding="utf-8")
    assert ipkg._artifact_rel("artifacts/kind/ok.json", artifacts) == "kind/ok.json"


def _source_and_target(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "package" / "a.json"
    src.parent.mkdir(parents=True)
    src.write_text("original", encoding="utf-8")
    return src, tmp_path / "state" / "kind" / "a.json"


def test_placing_onto_an_existing_file_is_refused_and_changes_nothing(tmp_path):
    src, dst = _source_and_target(tmp_path)
    dst.parent.mkdir(parents=True)
    dst.write_text("already here", encoding="utf-8")
    with pytest.raises(PackageError) as caught:
        ipkg._place(src, dst)
    assert caught.value.code == pcheck.CODE_DUPLICATE_MEMBER
    assert (src.read_text(encoding="utf-8"), dst.read_text(encoding="utf-8")) == ("original", "already here")


def test_placing_copies_where_a_link_cannot_be_made_and_never_over_a_file(tmp_path, monkeypatch):
    def no_link(*_args: Any) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(ipkg.os, "link", no_link)
    src, dst = _source_and_target(tmp_path)
    ipkg._place(src, dst)
    assert dst.read_text(encoding="utf-8") == "original"
    assert os.stat(src).st_ino != os.stat(dst).st_ino
    src.write_text("changed", encoding="utf-8")
    with pytest.raises(PackageError) as caught:
        ipkg._place(src, dst)
    assert caught.value.code == pcheck.CODE_DUPLICATE_MEMBER
    assert dst.read_text(encoding="utf-8") == "original"


def test_a_converted_shell_is_never_written_over_a_file(tmp_path):
    dst = tmp_path / "state" / "a.json"
    dst.parent.mkdir(parents=True)
    dst.write_text("there", encoding="utf-8")
    with pytest.raises(PackageError) as caught:
        ipkg._write_new(dst, "{}")
    assert caught.value.code == pcheck.CODE_DUPLICATE_MEMBER
    assert dst.read_text(encoding="utf-8") == "there"


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def test_case_folded_names_collide_where_the_file_system_folds_them(tmp_path):
    if not _case_insensitive(tmp_path):
        pytest.skip("this file system tells names apart by case")
    extra = [(f"{EXP}/artifacts/Kind/a", REG, b"1"), (f"{EXP}/artifacts/kind/a", REG, b"2")]
    _refused(tmp_path, _write(tmp_path, _raw(_export_entries(extra=extra))), pcheck.CODE_DUPLICATE_MEMBER)
