"""End-to-end checks on the bundled tracing viewer's read path.

Spawns the real Node viewer against a synthetic state dir and drives it over
HTTP, because the behaviour under test lives entirely in
``raven/tracing/viewer/*.js`` and none of it is reachable from Python.

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

VIEWER_DIR = Path(__file__).resolve().parents[2] / "raven" / "tracing" / "viewer"


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
