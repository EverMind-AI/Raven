"""The Trajectory Export package: its layout and manifest are the contract web-traj-import reads.

Every assertion on a constant, a manifest key set or a member rule below pins
the format at EXPORT_FORMAT_VERSION 1. Changing any of them is a format change:
bump EXPORT_FORMAT_VERSION and update the importer in the same change.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any

import pytest

from raven.tracing import artifact_v2
from raven.trajectory import export as ex
from raven.trajectory import selection as sel

SESSION = "tui:s1"
EXPORT_ID = "ex-20261001-abc123"


@pytest.fixture
def state(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "traces"
    (root / "logs" / "audit-artifacts").mkdir(parents=True)
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(root))
    return root


def _artifact(state: Path, kind: str, name: str, payload: Any) -> Path:
    path = state / "logs" / "audit-artifacts" / kind / "2026-10-01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _blob(state: Path, message: dict[str, Any]) -> str:
    sha1 = artifact_v2.message_sha1(message)
    path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact_v2.message_text(message), encoding="utf-8")
    return sha1


def _span(trace: str, span: str, name: str, *, parent: str | None = None, minute: int = 1, **attrs: Any):
    record: dict[str, Any] = {
        "traceId": trace,
        "spanId": span,
        "name": name,
        "startTime": f"2026-10-01T10:{minute:02d}:00+00:00",
        "endTime": f"2026-10-01T10:{minute:02d}:30+00:00",
        "attributes": {"session.key": SESSION, **attrs},
    }
    if parent:
        record["parentSpanId"] = parent
    return record


@pytest.fixture
def world(state: Path, tmp_path: Path) -> dict[str, Any]:
    """Two turns, a sub-agent of the first, a v2 shell with one blob missing, and a stray pointer."""
    kept = _blob(state, {"role": "user", "content": "plan a trip"})
    prompt = _blob(state, {"role": "system", "content": "you are raven"})
    gone = artifact_v2.message_sha1({"role": "assistant", "content": "this blob was purged"})
    shell = _artifact(
        state,
        "llm.input",
        "a-llm.json",
        {
            "artifactFormat": artifact_v2.ARTIFACT_FORMAT,
            "messages": [artifact_v2.make_ref(kept), artifact_v2.make_ref(gone)],
            "systemPrompt": artifact_v2.make_ref(prompt),
        },
    )
    turn_input = _artifact(state, "turn.input", "a-in.json", {"content": "plan a trip"})
    secret = _artifact(state, "tool.output", "a-tool.txt", "api_key=sk-live-not-redacted-0123456789")
    sub_out = _artifact(state, "turn.output", "sub-out.json", {"content": "found 3 hotels"})
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}", encoding="utf-8")
    other_turn_input = _artifact(state, "turn.input", "b-in.json", {"content": "not selected"})
    records = [
        _span(
            "trace-a",
            "span-a",
            "session.turn",
            minute=1,
            **{
                "turn.input_preview": "plan a trip",
                "turn.in_progress": False,
                "turn.input.artifact_path": str(turn_input),
            },
        ),
        _span("trace-a", "span-a1", "llm.call", parent="span-a", **{"llm.input.artifact_path": str(shell)}),
        _span(
            "trace-a",
            "span-a2",
            "tool.call",
            parent="span-a",
            **{
                "tool.output.artifact_path": str(secret),
                "tool.input.artifact_path": str(outside),
            },
        ),
        _span(
            "trace-sub",
            "span-s",
            "session.turn",
            minute=2,
            **{
                "session.key": "subagent:x",
                "trace.dispatched_in_trace_id": "trace-a",
                "turn.output.artifact_path": str(sub_out),
            },
        ),
        _span(
            "trace-b",
            "span-b",
            "session.turn",
            minute=5,
            **{
                "turn.input_preview": "not selected",
                "turn.input.artifact_path": str(other_turn_input),
            },
        ),
    ]
    log = state / "logs" / "audit-spans.log"
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    session = tmp_path / "s1.jsonl"
    rows = [
        {"_type": "metadata", "key": SESSION, "metadata": {"title": "Trip \u00e9t\u00e9"}, "last_consolidated": 2},
        {"role": "user", "content": "plan a trip", "timestamp": "2026-10-01T10:01:00", "trace_id": "trace-a"},
        {"role": "assistant", "content": "sure", "timestamp": "2026-10-01T10:01:20"},
        {"role": "user", "content": "not selected", "timestamp": "2026-10-01T10:05:00", "trace_id": "trace-b"},
    ]
    session.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return {
        "shell": shell,
        "secret": secret,
        "turn_input": turn_input,
        "sub_out": sub_out,
        "outside": outside,
        "kept": kept,
        "prompt": prompt,
        "gone": gone,
        "session": session,
    }


def _export(state: Path, world: dict[str, Any], tmp_path: Path, **kwargs: Any) -> tuple[Path, ex.ExportSummary]:
    plan = sel.plan_selection(SESSION, ["trace-a"], answering=False)
    selection = sel.collect_selection(plan)
    cut = sel.cut_session_record(world["session"], plan)
    out = tmp_path / "out" / "export.tar.gz"
    out.parent.mkdir()
    return out, ex.write_export(selection, cut, out, export_id=EXPORT_ID, **kwargs)


def _members(path: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(path, "r:gz") as tar:
        return {member.name: member for member in tar.getmembers()}


def _read(path: Path, name: str) -> bytes:
    with tarfile.open(path, "r:gz") as tar:
        handle = tar.extractfile(f"{EXPORT_ID}/{name}")
        assert handle is not None
        return handle.read()


# -- the contract ------------------------------------------------------------


def test_the_format_constants_are_pinned():
    assert ex.EXPORT_SCHEMA == "raven_trajectory_export"
    assert ex.EXPORT_FORMAT_VERSION == 1
    assert (ex.MANIFEST_FILE, ex.SPANS_FILE, ex.SESSION_FILE, ex.ARTIFACTS_DIR, ex.MESSAGES_DIR) == (
        "export.json",
        "spans.jsonl",
        "session.jsonl",
        "artifacts",
        "messages",
    )
    assert ex.MANIFEST_KEYS == (
        "schema",
        "format_version",
        "export_id",
        "created_at",
        "raven_version",
        "source",
        "turns",
        "traces",
        "counts",
        "missing_artifacts",
        "missing_messages",
        "transcript_unmatched",
    )
    assert ex.EXPORT_ID.fullmatch(ex.new_export_id())


def test_the_manifest_has_exactly_the_contract_keys(state, world, tmp_path):
    out, _ = _export(state, world, tmp_path)
    manifest = json.loads(_read(out, ex.MANIFEST_FILE))
    assert tuple(manifest) == ex.MANIFEST_KEYS
    assert set(manifest["source"]) == {"session_key", "title", "transcript_included"}
    assert set(manifest["traces"]) == {"selected", "dispatched"}
    assert set(manifest["counts"]) == {"spans", "artifacts", "messages"}
    assert all(set(turn) == {"number", "trace_id", "start", "input_preview"} for turn in manifest["turns"])
    assert manifest["schema"] == ex.EXPORT_SCHEMA and manifest["format_version"] == 1
    assert manifest["export_id"] == EXPORT_ID
    assert manifest["created_at"].endswith("+00:00")


def test_members_are_plain_files_under_the_export_id(state, world, tmp_path):
    out, _ = _export(state, world, tmp_path)
    members = _members(out)
    for name, info in members.items():
        assert info.isreg(), name
        assert name.startswith(f"{EXPORT_ID}/") and ".." not in name.split("/") and not name.startswith("/")
        assert (info.uid, info.gid, info.uname, info.gname, info.mode) == (0, 0, "", "", 0o644)
    assert set(members) == {
        f"{EXPORT_ID}/{name}"
        for name in (
            "export.json",
            "spans.jsonl",
            "session.jsonl",
            "artifacts/turn.input/2026-10-01/a-in.json",
            "artifacts/llm.input/2026-10-01/a-llm.json",
            "artifacts/tool.output/2026-10-01/a-tool.txt",
            "artifacts/turn.output/2026-10-01/sub-out.json",
            f"messages/{world['kept'][:2]}/{world['kept']}.json",
            f"messages/{world['prompt'][:2]}/{world['prompt']}.json",
        )
    }


# -- the content -------------------------------------------------------------


def test_artifacts_travel_byte_for_byte_unredacted_and_unresolved(state, world, tmp_path):
    out, _ = _export(state, world, tmp_path)
    assert _read(out, "artifacts/tool.output/2026-10-01/a-tool.txt") == world["secret"].read_bytes()
    shell = json.loads(_read(out, "artifacts/llm.input/2026-10-01/a-llm.json"))
    assert artifact_v2.is_v2(shell), "a v2 shell stays a shell"
    blob = _read(out, f"messages/{world['kept'][:2]}/{world['kept']}.json")
    assert hashlib.sha1(blob).hexdigest() == world["kept"]


def test_spans_are_rewritten_only_where_the_artifact_was_exported(state, world, tmp_path):
    out, summary = _export(state, world, tmp_path)
    spans = [json.loads(line) for line in _read(out, ex.SPANS_FILE).decode().splitlines()]
    assert [(s["traceId"], s["spanId"]) for s in spans] == [
        ("trace-a", "span-a"),
        ("trace-a", "span-a1"),
        ("trace-a", "span-a2"),
        ("trace-sub", "span-s"),
    ]
    tool = spans[2]["attributes"]
    assert tool["tool.output.artifact_path"] == "artifacts/tool.output/2026-10-01/a-tool.txt"
    assert tool["tool.input.artifact_path"] == str(world["outside"])
    assert summary.missing_artifacts == (str(world["outside"]),)


def test_the_manifest_says_what_was_exported_and_what_is_missing(state, world, tmp_path):
    out, summary = _export(state, world, tmp_path)
    manifest = json.loads(_read(out, ex.MANIFEST_FILE))
    assert manifest["source"] == {"session_key": SESSION, "title": "Trip \u00e9t\u00e9", "transcript_included": True}
    assert manifest["turns"] == [
        {"number": 1, "trace_id": "trace-a", "start": "2026-10-01T10:01:00+00:00", "input_preview": "plan a trip"}
    ]
    assert manifest["traces"] == {"selected": ["trace-a"], "dispatched": ["trace-sub"]}
    assert manifest["counts"] == {"spans": 4, "artifacts": 4, "messages": 2}
    assert manifest["missing_artifacts"] == [str(world["outside"])]
    assert manifest["missing_messages"] == [world["gone"]]
    assert manifest["transcript_unmatched"] == []
    assert summary.counts == manifest["counts"] and summary.size_bytes == out.stat().st_size


def test_the_session_record_holds_only_the_selected_turns(state, world, tmp_path):
    out, _ = _export(state, world, tmp_path)
    rows = [json.loads(line) for line in _read(out, ex.SESSION_FILE).decode().splitlines()]
    assert rows[0]["_type"] == "metadata" and rows[0]["last_consolidated"] == 0
    assert [r["content"] for r in rows[1:]] == ["plan a trip", "sure"]


def test_a_missing_session_record_is_left_out_and_said_so(state, world, tmp_path):
    world["session"].unlink()
    out, summary = _export(state, world, tmp_path)
    assert f"{EXPORT_ID}/{ex.SESSION_FILE}" not in _members(out)
    manifest = json.loads(_read(out, ex.MANIFEST_FILE))
    assert manifest["source"]["transcript_included"] is False and manifest["source"]["title"] == ""
    assert manifest["transcript_unmatched"] == ["trace-a"] and summary.transcript_unmatched == ("trace-a",)


def test_a_hard_linked_artifact_is_packed_as_a_plain_file(state, world, tmp_path):
    blob = state / "logs" / "audit-artifacts" / "_blobs" / "ab" / "abcdef.txt"
    blob.parent.mkdir(parents=True)
    os.replace(world["secret"], blob)
    os.link(blob, world["secret"])
    out, _ = _export(state, world, tmp_path)
    info = _members(out)[f"{EXPORT_ID}/artifacts/tool.output/2026-10-01/a-tool.txt"]
    assert info.isreg() and not info.islnk()
    assert not any("_blobs" in name for name in _members(out))


def test_a_file_that_vanishes_while_packing_is_reported_missing(state, world, tmp_path, monkeypatch):
    real = ex._read

    def flaky(path: Path):
        return None if path.name == "a-tool.txt" else real(path)

    monkeypatch.setattr(ex, "_read", flaky)
    out, summary = _export(state, world, tmp_path)
    assert str(world["secret"]) in summary.missing_artifacts
    spans = [json.loads(line) for line in _read(out, ex.SPANS_FILE).decode().splitlines()]
    assert spans[2]["attributes"]["tool.output.artifact_path"] == str(world["secret"])
    with tarfile.open(out, "r:gz") as tar:
        tar.extractall(tmp_path / "unpacked", filter="data")


def test_every_pointer_to_a_file_that_fails_to_read_keeps_its_original_value(state, world, tmp_path, monkeypatch):
    log = state / "logs" / "audit-spans.log"
    extra = _span(
        "trace-a", "span-a3", "tool.call", parent="span-a", **{"tool.result.artifact_path": str(world["secret"])}
    )
    log.write_text(log.read_text(encoding="utf-8") + json.dumps(extra) + "\n", encoding="utf-8")
    real = ex._read
    monkeypatch.setattr(ex, "_read", lambda path: None if path.name == "a-tool.txt" else real(path))
    out, summary = _export(state, world, tmp_path)
    members = _members(out)
    assert f"{EXPORT_ID}/artifacts/tool.output/2026-10-01/a-tool.txt" not in members
    spans = {s["spanId"]: s for s in map(json.loads, _read(out, ex.SPANS_FILE).decode().splitlines())}
    assert spans["span-a2"]["attributes"]["tool.output.artifact_path"] == str(world["secret"])
    assert spans["span-a3"]["attributes"]["tool.result.artifact_path"] == str(world["secret"])
    assert summary.missing_artifacts.count(str(world["secret"])) == 1
    manifest = json.loads(_read(out, ex.MANIFEST_FILE))
    assert manifest["counts"]["artifacts"] == 3
    assert str(world["secret"]) in manifest["missing_artifacts"]


def test_cancelling_leaves_nothing_behind(state, world, tmp_path):
    calls = {"n": 0}

    def stop_after_two() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    with pytest.raises(ex.ExportCancelledError):
        _export(state, world, tmp_path, should_stop=stop_after_two)
    assert list((tmp_path / "out").iterdir()) == []


def test_a_bad_export_id_is_refused(state, world, tmp_path):
    plan = sel.plan_selection(SESSION, ["trace-a"], answering=False)
    with pytest.raises(ValueError):
        ex.write_export(
            sel.collect_selection(plan),
            sel.cut_session_record(world["session"], plan),
            tmp_path / "x.tar.gz",
            export_id="../escape",
        )


# -- the download name -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("trip.tar.gz", "trip.tar.gz"),
        ("trip", "trip.tar.gz"),
        ("../../etc/passwd", "passwd.tar.gz"),
        ("C:\\Users\\me\\trip.tar.gz", "trip.tar.gz"),
        ('a<b>c:d"e|f?g*h.tar.gz', "abcdefgh.tar.gz"),
        ("tab\there\n.tar.gz", "tabhere.tar.gz"),
        ("", "raven-export.tar.gz"),
        (".tar.gz", "raven-export.tar.gz"),
        ("  ...  ", "raven-export.tar.gz"),
        ("Trip \u00e9t\u00e9-20261001-1000", "Trip \u00e9t\u00e9-20261001-1000.tar.gz"),
    ],
)
def test_download_names_are_cleaned(raw, clean):
    assert ex.clean_file_name(raw) == clean


def test_a_long_download_name_is_cut_before_the_suffix():
    name = ex.clean_file_name("x" * 500 + ".tar.gz")
    assert len(name) == 120 and name.endswith(".tar.gz")


def test_the_package_reads_back_with_the_standard_library(state, world, tmp_path):
    out, _ = _export(state, world, tmp_path)
    data = out.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(tmp_path / "unpacked", filter="data")
    assert (tmp_path / "unpacked" / EXPORT_ID / ex.MANIFEST_FILE).is_file()
