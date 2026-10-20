"""The signed-records corpus: the machinery, and the checks that can fail.

A corpus that only ever passes proves nothing about the verifier. These cases
therefore include the two that must fail -- a run record that has been changed
under the signature, and an envelope whose signature does not verify -- and the
one that must be reported as *not run* rather than as a pass.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "evidence_corpus.py"

#: A run record with the two frames that matter: the initialize request, and
#: the response that answers it. The response carries no ``method``, which is
#: the detail the parser has to get right.
RUN_FRAMES = [
    {
        "_type": "acp_frame",
        "timestamp": "2026-09-29T02:32:15.000000",
        "dir": "out",
        "frame": {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}},
    },
    {
        "_type": "acp_frame",
        "timestamp": "2026-09-29T02:32:16.000000",
        "dir": "in",
        "frame": {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": 1,
                "agentInfo": {"name": "example-agent", "title": "Example Agent", "version": "1.2.3"},
            },
        },
    },
    {
        "_type": "acp_frame",
        "timestamp": "2026-09-29T02:32:19.000000",
        "dir": "out",
        "frame": {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}},
    },
    {
        "_type": "acp_frame",
        "timestamp": "2026-09-29T02:32:20.000000",
        "dir": "in",
        "frame": {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "sess-1234"}},
    },
]


#: A tool-using run adds the frame that says the agent called something: an
#: ACP ``session/update`` whose payload is a tool call.
TOOL_CALL_FRAMES = [
    {
        "_type": "acp_frame",
        "timestamp": "2026-09-29T02:32:22.000000",
        "dir": "in",
        "frame": {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-1234",
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "web_fetch: https://example.com/",
                    "kind": "fetch",
                },
            },
        },
    },
]


def _write_run(directory: Path, name: str = "run.jsonl", frames=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "\n".join(json.dumps(frame) for frame in (frames if frames is not None else RUN_FRAMES)) + "\n",
        encoding="utf-8",
    )
    return path


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def _build(tmp_path: Path, runs: Path) -> Path:
    out = tmp_path / "corpus"
    key = tmp_path / "key.bin"
    result = _run(["build", "--runs", str(runs), "--out", str(out), "--key", str(key)])
    assert result.returncode == 0, result.stdout + result.stderr
    return out


def test_a_corpus_builds_from_a_run_record_and_verifies(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_run(runs)
    corpus = _build(tmp_path, runs)

    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "raven_evidence_corpus"
    assert len(manifest["entries"]) == 1

    result = _run(["verify", "--corpus", str(corpus), "--runs", str(runs)])
    assert result.returncode == 0, result.stdout
    assert "checks failed: 0" in result.stdout
    assert "checks not run: 0" in result.stdout


def test_the_statement_carries_the_run_and_says_what_it_does_not_establish(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_run(runs)
    corpus = _build(tmp_path, runs)
    entry = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))["entries"][0]
    envelope = json.loads((corpus / entry["entry"]).read_text(encoding="utf-8"))
    statement = json.loads(base64.b64decode(envelope["payload"]))

    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["subject"][0]["digest"]["sha256"]
    run = statement["predicate"]["run"]
    # The response frame carries no ``method``, so the agent is only readable
    # if the parser correlates the response with its request.
    assert run["agent"]["title"] == "Example Agent"
    assert run["agent"]["version"] == "1.2.3"
    assert run["session_id"] == "sess-1234"
    assert run["protocol_version"] == 1
    # A run that called no tool says so rather than looking like agent work.
    assert run["kind"] == "transport-only"
    assert "the transport worked" in statement["predicate"]["what_this_establishes"]


def test_the_manifest_states_the_mix_of_runs_it_holds(tmp_path: Path) -> None:
    """A reader must be able to tell what the corpus is made of without opening every entry.

    The corpus's whole point is the difference between a run that called a
    tool and a run that only proved the transport works. A manifest that
    did not say which it holds would let a reader assume the better kind.
    """
    runs = tmp_path / "runs"
    _write_run(runs, "transport.jsonl", RUN_FRAMES)
    _write_run(runs, "working.jsonl", RUN_FRAMES + TOOL_CALL_FRAMES)
    corpus = _build(tmp_path, runs)

    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_kinds"] == {"tool-using": 1, "transport-only": 1}
    gap = " ".join(manifest["known_gaps"])
    assert "1 tool-using" in gap
    assert "1 transport-only" in gap


def test_a_response_whose_request_frame_is_missing_is_still_read(tmp_path: Path) -> None:
    """The request may be in another log, or the log may start mid-session.

    Dropping the field would read a missing frame as an absent value, which
    is the one reading this corpus exists to avoid. The result's own shape
    says which call it answers, and how it was resolved is recorded.
    """
    runs = tmp_path / "runs"
    without_request = [frame for frame in RUN_FRAMES if frame.get("frame", {}).get("method") != "session/new"]
    _write_run(runs, frames=without_request)
    corpus = _build(tmp_path, runs)

    entry = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))["entries"][0]
    envelope = json.loads((corpus / entry["entry"]).read_text(encoding="utf-8"))
    statement = json.loads(base64.b64decode(envelope["payload"]))
    run = statement["predicate"]["run"]
    assert run["session_id"] == "sess-1234"
    assert run["method_resolution"] == "result-shape"


def test_a_run_record_changed_under_the_signature_fails(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    path = _write_run(runs)
    corpus = _build(tmp_path, runs)

    # Change the run after it was signed.
    changed = [dict(frame) for frame in RUN_FRAMES]
    changed[-1] = {**changed[-1], "timestamp": "2026-09-29T02:32:21.000000"}
    path.write_text("\n".join(json.dumps(frame) for frame in changed) + "\n", encoding="utf-8")

    result = _run(["verify", "--corpus", str(corpus), "--runs", str(runs)])
    assert result.returncode == 1
    assert "checks failed: 1" in result.stdout
    assert "digests to" in result.stdout


def test_a_broken_signature_fails(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_run(runs)
    corpus = _build(tmp_path, runs)

    entry = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))["entries"][0]
    path = corpus / entry["entry"]
    envelope = json.loads(path.read_text(encoding="utf-8"))
    raw = bytearray(base64.b64decode(envelope["signatures"][0]["sig"]))
    raw[0] ^= 0xFF
    envelope["signatures"][0]["sig"] = base64.b64encode(bytes(raw)).decode("ascii")
    path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")

    result = _run(["verify", "--corpus", str(corpus), "--runs", str(runs)])
    assert result.returncode == 1
    assert "checks failed: 1" in result.stdout
    assert "signature does not verify" in result.stdout


def test_a_check_that_could_not_run_is_reported_as_not_run(tmp_path: Path) -> None:
    """The run record is not on hand, so the digest is not recomputed.

    A verifier that reported this as a pass would be claiming a check it did
    not perform, and a corpus that does it is worse than no corpus.
    """
    runs = tmp_path / "runs"
    _write_run(runs)
    corpus = _build(tmp_path, runs)

    result = _run(["verify", "--corpus", str(corpus)])
    assert result.returncode == 0
    assert "checks not run: 1" in result.stdout
    assert "checks failed: 0" in result.stdout


def test_the_private_key_is_not_in_the_corpus(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_run(runs)
    corpus = _build(tmp_path, runs)
    names = {path.name for path in corpus.rglob("*") if path.is_file()}
    assert "key.bin" not in names
    assert not any(name.endswith(".pem") for name in names)
