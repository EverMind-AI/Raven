"""End-to-end checks on the bundled tracing viewer's read path.

Spawns the real Node viewer against a synthetic state dir and drives it over
HTTP, because the behaviour under test lives entirely in
``raven/cli/tracing_viewer/*.js`` and none of it is reachable from Python.

Two properties are pinned here, both of which a plausible refactor can break
silently:

* **Where logs are looked for.** The reader visits the active log and the
  ``archive/`` subtree, and nothing else under ``logs/`` -- notably not
  ``audit-artifacts/``, which holds one file per captured payload and grows to
  hundreds of thousands of entries. A reader that recurses all of ``logs/``
  still passes every functional assertion while taking seconds per request.
* **The tree does not carry a second copy of every span.** ``trace.tree`` is
  nesting plus ids; the spans themselves travel once, in ``trace.spans``. A
  refactor that nests whole spans again passes every render but doubles a
  payload already measured in hundreds of megabytes.
* **A deposit is found wherever the engine writes it.** The walk that feeds the
  ``memory.store`` join skips directories on a denylist of engine state, not an
  allowlist of known deposit directories. A deposit directory's name is not
  knowable from the type map -- agent cases land in ``.cases`` and skills in
  ``skills/``, neither of which appears there -- so an allowlist drops them
  silently, which is exactly what the test below plants.
* **The sharded reader agrees with the whole-corpus one.** The session list and
  a session's detail are built from per-file indexes; ``/api/data`` still builds
  everything at once. Where they disagree the sharded answer is wrong, and the
  disagreement is silent, so the tests below compare them rather than asserting
  a shape.
* **Snapshot reuse stays correct.** Rebuilding reads the whole retained
  history, so an unchanged tree is answered from a held copy. The failure mode
  that matters is the opposite of a slow one: a cache that never notices new
  spans. A reader that has just appended one sees it within a couple of polls
  (the refresh runs behind the response, so the first poll after a write may
  still answer from the previous snapshot).
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

VIEWER_DIR = Path(__file__).resolve().parents[2] / "raven" / "cli" / "tracing_viewer"


def _node() -> str:
    from raven.cli.tui_commands import find_node

    node, _version = find_node()
    if not node:
        pytest.skip("the tracing viewer needs the same Node >= 22 the TUI needs")
    return node


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _span(session_id: str, span_id: str, *, name: str = "tool.call", start: str = "2026-08-01T00:00:00+00:00") -> dict:
    # Not ``session.turn``: the viewer drops a trace whose only spans are turn
    # roots or hidden skills scans, so such a span would never reach the payload.
    return {
        "schemaVersion": "audit.span.v1",
        "traceId": f"trace-{span_id}",
        "spanId": span_id,
        "parentSpanId": None,
        "name": name,
        "kind": "INTERNAL",
        "startTime": start,
        "endTime": start,
        "status": {"code": "OK", "message": ""},
        "attributes": {"span.type": "tool", "session.id": session_id, "session.key": session_id},
        "events": [],
    }


def _write_spans(path: Path, spans: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for span in spans:
            handle.write(json.dumps(span) + "\n")


def _get(port: int, route: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{route}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


@contextmanager
def _viewer(state_dir: Path, everos_root: Path | None = None):
    port = _free_port()
    env = {"PATH": "/usr/bin:/bin", "TRACING_STATE_DIR": str(state_dir), "TRACE_UI_PORT": str(port)}
    if everos_root is not None:
        env["EVEROS_ROOT"] = str(everos_root)
    proc = subprocess.Popen(
        [_node(), str(VIEWER_DIR / "server.js")],
        cwd=str(VIEWER_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"viewer exited early:\n{proc.communicate()[0]}")
            try:
                if _get(port, "/api/health").get("ok"):
                    break
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                time.sleep(0.1)
        else:
            raise AssertionError("viewer never became healthy")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _session_ids(payload: dict) -> set[str]:
    return {session["sessionId"] for session in payload["sessions"]}


def test_reads_the_active_log_and_the_archive(tmp_path):
    logs = tmp_path / "logs"
    _write_spans(logs / "audit-spans.log", [_span("live-session", "span-live")])
    _write_spans(
        logs / "archive" / "2026-07-01" / "audit-spans-2026-07-01-1.log", [_span("archived-session", "span-arch")]
    )

    with _viewer(tmp_path) as port:
        assert _session_ids(_get(port, "/api/data")) == {"live-session", "archived-session"}


def test_ignores_a_log_planted_outside_the_active_and_archive_paths(tmp_path):
    logs = tmp_path / "logs"
    _write_spans(logs / "audit-spans.log", [_span("live-session", "span-live")])
    # Same name the reader matches on, but parked in the payload store, which is
    # the subtree whose size makes a full recursion of logs/ unaffordable.
    _write_spans(
        logs / "audit-artifacts" / "tool" / "2026-07-01" / "audit-spans-decoy.log", [_span("decoy", "span-decoy")]
    )

    with _viewer(tmp_path) as port:
        assert _session_ids(_get(port, "/api/data")) == {"live-session"}


def test_repeated_read_is_served_without_rebuilding(tmp_path):
    _write_spans(tmp_path / "logs" / "audit-spans.log", [_span("live-session", "span-live")])

    with _viewer(tmp_path) as port:
        first = _get(port, "/api/data")
        second = _get(port, "/api/data")

    # generatedAt is stamped per build, so an equal one means the second read
    # never rebuilt.
    assert first["generatedAt"] == second["generatedAt"]
    assert _session_ids(second) == {"live-session"}


def test_tree_carries_ids_and_nesting_only(tmp_path):
    parent = _span("live-session", "span-parent")
    child = _span("live-session", "span-child", start="2026-08-01T00:00:01+00:00")
    child["parentSpanId"] = "span-parent"
    _write_spans(tmp_path / "logs" / "audit-spans.log", [parent, child])

    with _viewer(tmp_path) as port:
        trace = _get(port, "/api/data")["sessions"][0]["traces"][0]

    root = trace["tree"][0]
    assert set(root) == {"spanId", "depth", "children"}
    assert root["spanId"] == "span-parent"
    assert [node["spanId"] for node in root["children"]] == ["span-child"]
    assert root["children"][0]["depth"] == 1
    # Every node still resolves against the spans that came with it, which is
    # what the page needs to render a node at all.
    span_ids = {span["spanId"] for span in trace["spans"]}
    assert {root["spanId"], *(node["spanId"] for node in root["children"])} <= span_ids


def test_search_returns_the_newest_matches_within_the_cap(tmp_path):
    # More matches than the response is allowed to carry, spread over enough
    # traces that a scan has to cross them to see them all.
    spans = []
    for trace_index in range(60):
        for span_index in range(5):
            span = _span(
                f"session-{trace_index}",
                f"span-{trace_index}-{span_index}",
                name="tool.call",
                start=f"2026-08-01T00:{trace_index:02d}:{span_index:02d}+00:00",
            )
            span["attributes"]["tool.name"] = "needle"
            spans.append(span)
    _write_spans(tmp_path / "logs" / "audit-spans.log", spans)

    with _viewer(tmp_path) as port:
        results = _get(port, "/api/search?q=needle")["results"]

    assert len(results) == 50
    stamps = [result["startTime"] for result in results]
    assert stamps == sorted(stamps, reverse=True)
    assert results[0]["startTime"] == "2026-08-01T00:59:04+00:00"


def test_deposit_in_a_directory_the_type_map_does_not_name_is_found(tmp_path):
    """An agent case lives in ``.cases``, which the type map has no key for.

    It is typed by its file name instead, so the walk has to reach it. This
    regressed once already, silently: every store span kept reporting
    ``pending`` for turns that had in fact been distilled.
    """
    stamp = "2026-08-01T00:00:00.500000+00:00"
    cases_dir = tmp_path / "everos" / "agents" / "default" / ".cases"
    cases_dir.mkdir(parents=True)
    (cases_dir / "agent_case-2026-08-01.md").write_text(
        "<!-- entry:ac_1 -->\n"
        "## ac_1\n\n"
        "**session_id**: live-session\n"
        f"**timestamp**: {stamp}\n"
        "**parent_id**: mc_deadbeef\n\n"
        "### Subject\nA case the type map cannot name\n\n"
        "### Summary\nWhat the turn learned.\n"
        "<!-- /entry:ac_1 -->\n",
        encoding="utf-8",
    )
    # A virtualenv beside it, which the walk must still refuse to descend.
    venv = tmp_path / "everos" / ".server-venv" / "lib" / "site-packages" / "somepkg"
    venv.mkdir(parents=True)
    (venv / "README.md").write_text("not a deposit\n", encoding="utf-8")

    store = _span("live-session", "span-store", name="memory.store", start=stamp)
    store["attributes"]["memory.session_id"] = "live-session"
    _write_spans(tmp_path / "logs" / "audit-spans.log", [store])

    with _viewer(tmp_path, everos_root=tmp_path / "everos") as port:
        spans = _get(port, "/api/data")["sessions"][0]["traces"][0]["spans"]

    attrs = next(span for span in spans if span["name"] == "memory.store")["attributes"]
    assert attrs["memory.deposit_status"] == "distilled"
    assert "1 case" in attrs["memory.deposit_summary"]
    families = json.loads(attrs["memory.deposit_json"])["families"]
    assert [entry["subject"] for entry in families["agent_case"]] == ["A case the type map cannot name"]


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _sessions_across_two_files(tmp_path):
    """A session split over an archive and the active log, plus two others.

    Carries events of both shapes on purpose. One is attributed by session id,
    the other only by session key -- and the key-only shape is the one a reader
    can drop silently, which is exactly what happened until a review caught it.
    """
    logs = tmp_path / "logs"
    early = _span("split-session", "span-early", start="2026-07-01T00:00:00+00:00")
    late = _span("split-session", "span-late", start="2026-08-01T00:00:00+00:00")
    late["parentSpanId"] = "span-early"
    _write_spans(logs / "archive" / "2026-07-01" / "audit-spans-2026-07-01-1.log", [early])
    _write_spans(
        logs / "audit-spans.log",
        [late, _span("other-session", "span-other"), _span("third-session", "span-third")],
    )
    _write_events(
        logs / "audit-events.log",
        [
            {
                "type": "note",
                "sessionId": "other-session",
                "traceId": "trace-span-other",
                "timestamp": "2026-08-01T00:00:00+00:00",
                "event": {},
            },
            {
                "type": "note",
                "sessionKey": "split-session",
                "traceId": "trace-span-early",
                "timestamp": "2026-07-01T00:00:00+00:00",
                "event": {},
            },
        ],
    )


def test_a_span_with_only_a_session_key_lands_in_the_elected_session(tmp_path):
    """The case the whole per-file split had to be designed around.

    Most records in a real store carry no ``session.id``; they are attributed by
    electing a canonical id for their ``session.key`` across the whole corpus. A
    reader that elected per file, or skipped the election, would drop them or
    file them somewhere else.
    """
    logs = tmp_path / "logs"
    identified = _span("elected-session", "span-identified")
    identified["attributes"]["session.key"] = "shared-key"
    # No session.id at all, and in a different file from the span that names it.
    keyed = _span("elected-session", "span-keyed", start="2026-08-01T00:00:01+00:00")
    keyed["attributes"].pop("session.id")
    keyed["attributes"]["session.key"] = "shared-key"
    keyed["parentSpanId"] = "span-identified"
    _write_spans(logs / "archive" / "2026-07-01" / "audit-spans-2026-07-01-1.log", [identified])
    _write_spans(logs / "audit-spans.log", [keyed])

    with _viewer(tmp_path) as port:
        listed = _get(port, "/api/sessions")["sessions"]
        detail = _get(port, "/api/sessions/elected-session")["session"]
        full = _get(port, "/api/data")

    assert [row["sessionId"] for row in listed] == ["elected-session"]
    span_ids = {span["spanId"] for trace in detail["traces"] for span in trace["spans"]}
    assert span_ids == {"span-identified", "span-keyed"}
    assert listed[0]["spanCount"] == 2
    # And the whole-corpus reader agrees, which is the actual contract.
    assert {span["spanId"] for trace in full["sessions"][0]["traces"] for span in trace["spans"]} == span_ids


def test_session_list_matches_the_whole_corpus_reader(tmp_path):
    _sessions_across_two_files(tmp_path)

    with _viewer(tmp_path) as port:
        full = _get(port, "/api/data")
        listed = _get(port, "/api/sessions")

    # traceCount is deliberately absent from a list row and spanCount stands in
    # its place; see the note in shard-index.js. Everything else must agree.
    want = [{k: v for k, v in session.items() if k not in ("traces", "traceCount")} for session in full["sessions"]]
    got = [{k: v for k, v in session.items() if k != "spanCount"} for session in listed["sessions"]]
    assert got == want
    assert all(row["spanCount"] > 0 for row in listed["sessions"])


def test_session_detail_matches_the_whole_corpus_reader(tmp_path):
    _sessions_across_two_files(tmp_path)

    with _viewer(tmp_path) as port:
        full = _get(port, "/api/data")
        details = {
            session["sessionId"]: _get(port, f"/api/sessions/{session['sessionId']}")["session"]
            for session in full["sessions"]
        }

    for session in full["sessions"]:
        assert details[session["sessionId"]] == session, session["sessionId"]
    # The split session is the one that proves a detail reads more than one file.
    assert len(details["split-session"]["traces"][0]["spans"]) == 2
    # And that a key-matched event survived the trip. Comparing against
    # /api/data covers it, but assert it outright so the reason is visible.
    key_matched = [
        event
        for trace in details["split-session"]["traces"]
        for event in trace["events"]
        if event.get("sessionKey") == "split-session"
    ]
    assert key_matched, "an event attributed by session key alone was dropped"


def test_unknown_session_is_a_404(tmp_path):
    _write_spans(tmp_path / "logs" / "audit-spans.log", [_span("live-session", "span-live")])

    with _viewer(tmp_path) as port:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(port, "/api/sessions/nope")
    assert excinfo.value.code == 404


def test_trace_owner_resolves_a_trace_to_its_session(tmp_path):
    _sessions_across_two_files(tmp_path)

    with _viewer(tmp_path) as port:
        trace_id = _get(port, "/api/sessions/other-session")["session"]["traces"][0]["traceId"]
        owner = _get(port, f"/api/trace-owner?traceId={trace_id}")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(port, "/api/trace-owner?traceId=trace-does-not-exist")

    assert owner["sessionId"] == "other-session"
    assert excinfo.value.code == 404


def test_llm_calls_match_the_whole_corpus_reader(tmp_path):
    logs = tmp_path / "logs"
    call = _span("live-session", "span-call", name="llm.call")
    # A call the election cannot place in a session. The whole-corpus reader drops
    # it, so this one has to as well.
    orphan = {**_span("x", "span-orphan", name="llm.call"), "attributes": {"span.type": "model"}}
    _write_spans(logs / "audit-spans.log", [call, orphan, _span("live-session", "span-tool")])

    with _viewer(tmp_path) as port:
        full = _get(port, "/api/data")
        calls = _get(port, "/api/llm-calls?window=all")["calls"]

    want = sorted(
        span["spanId"]
        for session in full["sessions"]
        for trace in session["traces"]
        for span in trace["spans"]
        if span["name"] == "llm.call"
    )
    assert sorted(entry["span"]["spanId"] for entry in calls) == want
    assert "span-orphan" not in {entry["span"]["spanId"] for entry in calls}


def test_a_stale_sidecar_is_rebuilt_rather_than_trusted(tmp_path):
    logs = tmp_path / "logs"
    archive = logs / "archive" / "2026-07-01" / "audit-spans-2026-07-01-1.log"
    _write_spans(archive, [_span("archived-session", "span-arch")])
    _write_spans(logs / "audit-spans.log", [_span("live-session", "span-live")])

    with _viewer(tmp_path) as port:
        assert _session_ids(_get(port, "/api/sessions")) == {"archived-session", "live-session"}

    sidecar = logs / "index" / "archive" / "2026-07-01" / "audit-spans-2026-07-01-1.log.json"
    assert sidecar.exists(), "a rotated file should have been indexed"

    # Corruption, a wrong schema and an outright lie about the contents must all
    # send the reader back to the log rather than costing it a session.
    for bad in ('{"nope"', '{"schema": 0, "pairs": []}', '{"schema": 99, "pairs": [], "source": {}}'):
        sidecar.write_text(bad, encoding="utf-8")
        with _viewer(tmp_path) as port:
            assert _session_ids(_get(port, "/api/sessions")) == {"archived-session", "live-session"}, bad
    # The stale case that matters most, because the sidecar is perfectly
    # well-formed: the log grew after it was written. Only the size and mtime it
    # recorded say so.
    _write_spans(archive, [_span("appended-session", "span-appended")])
    with _viewer(tmp_path) as port:
        assert _session_ids(_get(port, "/api/sessions")) == {
            "archived-session",
            "live-session",
            "appended-session",
        }


def test_appended_span_reaches_a_later_poll(tmp_path):
    active = tmp_path / "logs" / "audit-spans.log"
    _write_spans(active, [_span("first-session", "span-first")])

    with _viewer(tmp_path) as port:
        assert _session_ids(_get(port, "/api/data")) == {"first-session"}
        _write_spans(active, [_span("second-session", "span-second", start="2026-08-02T00:00:00+00:00")])

        deadline = time.monotonic() + 30
        seen: set[str] = set()
        while time.monotonic() < deadline:
            seen = _session_ids(_get(port, "/api/data"))
            if "second-session" in seen:
                break
            time.sleep(0.5)

    assert seen == {"first-session", "second-session"}


def _v2_shell(state_dir, messages, *, extra_messages=()):
    """A v2 shell plus its message blobs under the viewer's state dir."""
    from raven.tracing import artifact_v2 as v2

    artifacts = state_dir / "logs" / "audit-artifacts"
    refs = []
    for message in messages:
        sha1 = v2.message_sha1(message)
        blob = v2.message_path(artifacts, sha1)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text(v2.message_text(message), encoding="utf-8")
        refs.append(v2.make_ref(sha1))
    payload = {
        "artifactFormat": v2.ARTIFACT_FORMAT,
        "provider": "openrouter",
        "model": "openrouter/x",
        "systemPrompt": refs[0],
        "prompt": refs[-1],
        "messages": [*refs, *extra_messages],
        "tools": [{"function": {"name": "grep"}}],
    }
    path = artifacts / "llm.input" / "2026-08-01" / "in.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, payload


def test_the_viewer_resolves_a_v2_artifact_to_v1_shape(tmp_path):
    messages = [
        {"role": "system", "content": "you are raven"},
        {"role": "user", "content": [{"type": "text", "text": "latest"}]},
    ]
    shell, _ = _v2_shell(tmp_path, messages)

    with _viewer(tmp_path) as port:
        got = _get(port, f"/api/artifact?path={shell}")

    assert got["parsed"]["messages"] == messages
    assert got["parsed"]["systemPrompt"] == "you are raven"
    assert isinstance(got["parsed"]["prompt"], str), "app.js calls .match() on this"
    assert got["parsed"]["prompt"] == '[{"type":"text","text":"latest"}]'
    assert "artifactFormat" not in got["parsed"]


def test_the_viewer_renders_a_placeholder_for_a_message_blob_that_is_gone(tmp_path):
    from raven.tracing import artifact_v2 as v2

    messages = [
        {"role": "system", "content": "you are raven"},
        {"role": "user", "content": "hi"},
    ]
    shell, _ = _v2_shell(tmp_path, messages)
    gone = v2.message_sha1(messages[1])
    v2.message_path(tmp_path / "logs" / "audit-artifacts", gone).unlink()

    with _viewer(tmp_path) as port:
        got = _get(port, f"/api/artifact?path={shell}")

    resolved = got["parsed"]["messages"]
    assert len(resolved) == 2, "position is preserved; a message is never dropped"
    assert gone in resolved[1]["content"]


def test_the_viewer_leaves_a_reference_that_is_not_a_sha1_alone(tmp_path):
    from raven.tracing import artifact_v2 as v2

    artifacts = tmp_path / "logs" / "audit-artifacts"
    shell = artifacts / "llm.input" / "2026-08-01" / "in.json"
    shell.parent.mkdir(parents=True, exist_ok=True)
    shell.write_text(
        json.dumps(
            {
                "artifactFormat": v2.ARTIFACT_FORMAT,
                "messages": [{"$msg": "../../../etc/passwd"}, {"$msg": "0" * 39}],
            }
        ),
        encoding="utf-8",
    )

    with _viewer(tmp_path) as port:
        got = _get(port, f"/api/artifact?path={shell}")

    assert got["parsed"]["messages"] == [
        {"$msg": "../../../etc/passwd"},
        {"$msg": "0" * 39},
    ], "an invalid reference is data, not an address: pass it through untouched"


def test_a_v1_artifact_is_returned_unchanged(tmp_path):
    artifacts = tmp_path / "logs" / "audit-artifacts"
    shell = artifacts / "llm.input" / "2026-08-01" / "v1.json"
    shell.parent.mkdir(parents=True, exist_ok=True)
    v1 = {"messages": [{"role": "user", "content": "hi"}], "systemPrompt": "sys"}
    shell.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")

    with _viewer(tmp_path) as port:
        got = _get(port, f"/api/artifact?path={shell}")

    assert got["parsed"] == v1


def test_python_and_the_viewer_resolve_a_shell_identically(tmp_path):
    """The one gate on 'one rule, two implementations, two languages'.

    Includes non-string content on purpose: Python's default JSON separators
    carry spaces and JavaScript's do not, so a text field built from a
    multimodal message is where the two would first disagree.
    """
    from raven.tracing import artifact_v2 as v2

    artifacts = tmp_path / "logs" / "audit-artifacts"
    messages = [
        {"role": "system", "content": "you are raven"},
        {"role": "user", "content": "plain"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "name": "grep"}]},
        {"role": "user", "content": [{"type": "text", "text": "latest"}, {"type": "image"}]},
    ]
    refs = []
    for message in messages:
        blob = v2.message_path(artifacts, v2.message_sha1(message))
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text(v2.message_text(message), encoding="utf-8")
        refs.append(v2.make_ref(v2.message_sha1(message)))
    inlined = {"role": "user", "content": "this one was never addressed"}
    shell_payload = {
        "artifactFormat": v2.ARTIFACT_FORMAT,
        "provider": "openrouter",
        "model": "openrouter/x",
        "systemPrompt": refs[0],
        "prompt": refs[3],
        "messages": [*refs, inlined, {"$msg": "not-a-sha1"}],
        "tools": [{"function": {"name": "grep"}}],
    }
    shell = artifacts / "llm.input" / "2026-08-01" / "in.json"
    shell.parent.mkdir(parents=True, exist_ok=True)
    shell.write_text(json.dumps(shell_payload, ensure_ascii=False), encoding="utf-8")

    from_python = v2.resolve_payload(shell_payload, artifacts)
    with _viewer(tmp_path) as port:
        from_viewer = _get(port, f"/api/artifact?path={shell}")["parsed"]

    assert json.dumps(from_python, ensure_ascii=False, sort_keys=True) == json.dumps(
        from_viewer, ensure_ascii=False, sort_keys=True
    )
