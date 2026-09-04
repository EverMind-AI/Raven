"""Tests for the interactive trajectory browser (`raven.cli.trajectory_browse`)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from raven.cli import trajectory_browse as tbrowse
from raven.trajectory import store as tstore
from raven.trajectory import verdict as tverdict

_CANCEL = object()
_BACK = tbrowse._BACK


def _write_log(path, spans):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _span(trace_id, *, attempt_id=None, session_key=None, name="session.turn", attrs=None, span_id=None, end=None):
    attributes = {"session.key": session_key}
    if attempt_id is not None:
        attributes["attempt.id"] = attempt_id
    if attrs:
        attributes.update(attrs)
    return {
        "schemaVersion": "audit.span.v1",
        "traceId": trace_id,
        "spanId": span_id or f"span-{trace_id}-{name}",
        "name": name,
        "startTime": "2026-08-20T10:00:00+00:00",
        "endTime": end or "2026-08-20T10:00:01+00:00",
        "attributes": attributes,
    }


class _FakeQuestionary:
    """Scripted questionary stand-in.

    Each prompt pops one scripted answer. An answer may be:
    - ``("pick", substr)``  — select the first choice whose title contains substr;
    - ``("pickall", [substr, ...])`` — checkbox: the values of all matching choices;
    - ``("hit", key, substr)`` — an injected extra key fired on the matching
      choice: returns ``_KeyHit(key, value)``;
    - ``_CANCEL``           — Ctrl+C: ``unsafe_ask()`` raises KeyboardInterrupt
      (the production path; the fallback ``ask()`` would return None);
    - ``_BACK``             — the injected Esc binding fired (returned verbatim);
    - a zero-arg callable   — invoked at answer time (side effects), its return used;
    - anything else         — returned verbatim.
    Every prompt is recorded as (kind, message, [choice titles]); separator
    lines are recorded like choices but are never matched by a pick. A title
    may be a styled token list (table rows) — it is flattened to plain text
    for recording and matching, like a terminal renders it.
    """

    class Choice:
        def __init__(self, title, value=None):
            self.title = title
            self.value = value if value is not None else title

    class Separator:
        def __init__(self, line=""):
            self.title = line

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[tuple[str, str, list[str]]] = []
        self.calls: list[tuple[str, str, dict]] = []

    @staticmethod
    def _title_text(choice):
        title = getattr(choice, "title", "")
        if isinstance(title, list):
            return "".join(seg[1] for seg in title)
        return title

    def _resolve(self, kind, message, choices, kwargs=None):
        titles = [self._title_text(c) for c in choices] if choices else []
        self.prompts.append((kind, message, titles))
        self.calls.append((kind, message, kwargs or {}))
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer()
        if answer is _CANCEL:
            value = None
        elif isinstance(answer, tuple) and answer and answer[0] == "pick":
            value = next(c.value for c in choices if hasattr(c, "value") and answer[1] in self._title_text(c))
        elif isinstance(answer, tuple) and answer and answer[0] == "pickall":
            value = [
                next(c.value for c in choices if hasattr(c, "value") and s in self._title_text(c)) for s in answer[1]
            ]
        elif isinstance(answer, tuple) and answer and answer[0] == "hit":
            picked = next(c.value for c in choices if hasattr(c, "value") and answer[2] in self._title_text(c))
            value = tbrowse._KeyHit(answer[1], picked)
        else:
            value = answer

        def unsafe_ask():
            if value is None:
                raise KeyboardInterrupt
            return value

        return type("_Ask", (), {"ask": staticmethod(lambda: value), "unsafe_ask": staticmethod(unsafe_ask)})()

    def select(self, message, choices=None, **kw):
        return self._resolve("select", message, choices, kw)

    def checkbox(self, message, choices=None, **kw):
        return self._resolve("checkbox", message, choices, kw)

    def confirm(self, message, **kw):
        return self._resolve("confirm", message, None, kw)

    def text(self, message, **kw):
        return self._resolve("text", message, None, kw)

    def press_any_key_to_continue(self, message=None, **kw):
        return self._resolve("press", message, None, kw)


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


def _browse(monkeypatch, answers, workspace):
    fake = _FakeQuestionary(answers)
    monkeypatch.setattr(tbrowse, "_require_questionary", lambda: fake)
    tbrowse.browse_trajectories(workspace=workspace)
    return fake


def _two_turn_log(state, session_key="cli:a"):
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key=session_key, attrs={"turn.input_preview": "fix the bug"}),
            _span("trace-2", session_key=session_key, name="tool.call"),
            _span("trace-2", session_key=session_key, span_id="span-t2-turn"),
        ],
    )


_IDS = ("trace-1", "trace-2", "cli:a", "att-")


def _assert_no_ids(text: str, ids=_IDS) -> None:
    for id_ in ids:
        assert id_ not in text, f"{id_!r} leaked into: {text!r}"


def _data_rows(titles):
    """Attempt-table data rows among one screen's recorded titles (the header
    line also starts with '#', but is followed by padding, not a digit)."""
    return [t for t in titles if t[:1] == "#" and t[1:2].isdigit()]


def _cell_of(titles, row_text, col):
    """Text under fixed column ``col`` of one data row, located by the header
    (fixed columns are exactly as wide as their headers here)."""
    header = next(t for t in titles if t.startswith("#") and "STARTED" in t)
    start = header.index(col)
    return row_text[start : start + len(col)]


# ── aggregation / labels (pure functions) ─────────────────────────────


def test_labels_carry_no_ids(state, workspace):
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a"),
            _span("trace-3", attempt_id="att-old", session_key="cli:a"),
        ],
    )
    tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    sessions = tbrowse.scan_sessions(workspace)

    assert sessions
    for srow in sessions:
        _assert_no_ids(tbrowse.session_label(srow), ("trace-", "cli:a", "att-"))
        for i, row in enumerate(srow.attempts, start=1):
            _assert_no_ids(tbrowse.attempt_label(i, row), ("trace-", "cli:a", "att-"))


def test_orphan_session_falls_back_label(state, workspace):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    (srow,) = tbrowse.scan_sessions(workspace)
    assert srow.title.startswith("cli session")


def test_titled_session_uses_metadata_title(state, workspace):
    from raven.session.manager import SessionManager

    manager = SessionManager(workspace)
    session = manager.get_or_create("cli:a")
    session.add_message("user", "hello")
    session.set_title("My debugging run")
    manager.save(session)
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    (srow,) = tbrowse.scan_sessions(workspace)

    assert srow.title == "My debugging run"


def test_workspace_read_failure_degrades(state, tmp_path):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    (srow,) = tbrowse.scan_sessions(tmp_path / "missing-ws")
    assert srow.title.startswith("cli session")


def test_no_session_bucket(state, workspace):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])
    (srow,) = tbrowse.scan_sessions(workspace)
    assert srow.key is None
    assert srow.title == "(no session)"


def test_legacy_multi_trace_is_not_merged(state, workspace):
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", attempt_id="att-old", session_key="cli:a"),
            _span("trace-2", attempt_id="att-old", session_key="cli:a"),
            _span("trace-3", session_key="cli:a"),
        ],
    )
    aid = tstore.merge_attempts(["trace-3", "att-old"], state_dir=state)
    tstore.split_attempt(aid, state_dir=state)

    (srow,) = tbrowse.scan_sessions(workspace)
    by_traces = {row.traces: row for row in srow.attempts}

    assert by_traces[("trace-1", "trace-2")].merged is False
    aid2 = tstore.merge_attempts(["trace-3", "att-old"], state_dir=state)
    (srow,) = tbrowse.scan_sessions(workspace)
    (row,) = srow.attempts
    assert row.key == aid2 and row.merged is True


def test_verdict_via_alias_and_pin_via_definition(state, workspace):
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span("trace-1", session_key="cli:a"),
            _span("trace-2", session_key="cli:a"),
            _span("trace-3", session_key="cli:a"),
        ],
    )
    d1 = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    tverdict.record_verdict(d1, "fail", source="user", state_dir=state)
    tstore.pin(d1, state_dir=state)
    d2 = tstore.merge_attempts([d1, "trace-3"], state_dir=state)

    (srow,) = tbrowse.scan_sessions(workspace)
    (row,) = srow.attempts

    assert row.key == d2
    assert row.verdict == "fail"  # surfaced through the absorbed alias
    assert row.pinned is True  # alias pin extends to the definition


def test_defective_records_tolerated(state, workspace):
    good = _span("trace-1", session_key="cli:a")
    bad_preview = _span("trace-2", session_key="cli:a", attrs={"turn.input_preview": 123})
    bad_attempt = _span("trace-3", session_key="cli:a")
    bad_attempt["attributes"]["attempt.id"] = ["not", "a", "string"]
    bad_container = {"schemaVersion": "audit.span.v1", "traceId": "trace-4", "attributes": "bad"}
    _write_log(state / "logs" / "audit-spans.log", [good, bad_preview, bad_attempt, bad_container])

    sessions = tbrowse.scan_sessions(workspace)

    keys = {row.key for srow in sessions for row in srow.attempts}
    assert keys == {"trace-1", "trace-2", "trace-3", "trace-4"}


def test_label_normalization(state, workspace):
    from raven.session.manager import SessionManager

    manager = SessionManager(workspace)
    session = manager.get_or_create("cli:a")
    session.add_message("user", "x")
    session.set_title("x[red]y\nmulti\tline\x07title")
    manager.save(session)
    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a", attrs={"turn.input_preview": "pre[/dim]view\nwith\nnewlines"})],
    )

    (srow,) = tbrowse.scan_sessions(workspace)

    assert "\\" not in srow.title  # plain text boundary: no Rich escaping
    assert "\n" not in srow.title and "\t" not in srow.title and "\x07" not in srow.title
    assert "x[red]y" in srow.title
    (row,) = srow.attempts
    assert "\n" not in (row.preview or "")
    assert "pre[/dim]view" in row.preview


def test_fallback_title_prefers_span_channel(state, workspace):
    """With no session file and a separator-less key, the span's own channel
    attribute still yields a human label (normalized to one line). The time
    lives in the table's LAST ACTIVITY column, not in the title."""
    span = _span("trace-1", session_key="weirdkey", attrs={"channel": "discord\nextra"})
    _write_log(state / "logs" / "audit-spans.log", [span])

    (srow,) = tbrowse.scan_sessions(workspace)

    assert srow.title == "discord extra session"
    assert "\n" not in srow.title
    assert "weirdkey" not in srow.title


def test_malformed_timestamps_and_session_key_stay_one_line(state, workspace):
    """Corrupt startTime strings and a separator-less session key must not
    break the one-line layout or surface the raw key in a fallback title."""
    bad_key = "weird session key without separator\nline2"
    span = _span("trace-1", session_key=bad_key)
    span["startTime"] = "2026-\n08\t20T1\x07:00:00"
    span["endTime"] = "2026-\n08\t20T1\x07:00:01"
    _write_log(state / "logs" / "audit-spans.log", [span])

    (srow,) = tbrowse.scan_sessions(workspace)

    label = tbrowse.session_label(srow)
    assert "\n" not in label and "\t" not in label and "\x07" not in label
    assert bad_key not in label
    assert label.startswith("unknown session")
    (row,) = srow.attempts
    attempt = tbrowse.attempt_label(1, row)
    assert "\n" not in attempt and "\t" not in attempt and "\x07" not in attempt


def test_malformed_session_entries_degrade(state, workspace, monkeypatch):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    monkeypatch.setattr(
        tbrowse.SessionManager,
        "list_sessions",
        lambda self, channel=None: ["junk", {"key": 1, "metadata": {"title": "x"}}, {"key": "cli:a", "metadata": 5}],
    )
    (srow,) = tbrowse.scan_sessions(workspace)
    assert srow.title.startswith("cli session")


# ── logical spans ─────────────────────────────────────────────────────


def test_checkpoint_and_final_count_as_one_span(state, workspace):
    checkpoint = _span("trace-1", session_key="cli:a", span_id="span-root", end="2026-08-20T10:00:00+00:00")
    final = _span("trace-1", session_key="cli:a", span_id="span-root", end="2026-08-20T10:05:00+00:00")
    _write_log(state / "logs" / "audit-spans.log", [checkpoint, final])

    (srow,) = tbrowse.scan_sessions(workspace)
    (row,) = srow.attempts

    assert row.turns == 1 and row.spans == 1
    assert row.end == "2026-08-20T10:05:00+00:00"  # the final record wins


def test_missing_span_ids_never_merge(state, workspace):
    a = _span("trace-1", session_key="cli:a")
    b = _span("trace-1", session_key="cli:a")
    del a["spanId"], b["spanId"]
    _write_log(state / "logs" / "audit-spans.log", [a, b])

    (srow,) = tbrowse.scan_sessions(workspace)
    (row,) = srow.attempts

    assert row.spans == 2


def test_same_span_id_across_traces_stays_separate(state, workspace):
    a = _span("trace-1", session_key="cli:a", span_id="span-shared")
    b = _span("trace-2", session_key="cli:a", span_id="span-shared")
    _write_log(state / "logs" / "audit-spans.log", [a, b])

    (srow,) = tbrowse.scan_sessions(workspace)

    assert {row.key: row.turns for row in srow.attempts} == {"trace-1": 1, "trace-2": 1}


def test_snapshot_discipline(state, workspace, monkeypatch):
    _two_turn_log(state)
    calls = {"definitions": 0, "pins": 0, "read_verdicts": 0}
    real_defs, real_pins, real_verdicts = tstore.definitions, tstore.pins, tbrowse.read_verdicts

    def _count(name, real):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(tbrowse.tstore, "definitions", _count("definitions", real_defs))
    monkeypatch.setattr(tbrowse.tstore, "pins", _count("pins", real_pins))
    monkeypatch.setattr(tbrowse, "read_verdicts", _count("read_verdicts", real_verdicts))

    def _forbidden(*_a, **_k):
        raise AssertionError("per-row store reads are forbidden in the scan")

    monkeypatch.setattr(tbrowse.tstore, "attempt_alias_ids", _forbidden)
    monkeypatch.setattr(tbrowse.tstore, "is_pinned", _forbidden)

    tbrowse.scan_sessions(workspace)

    assert calls == {"definitions": 1, "pins": 1, "read_verdicts": 1}


# ── flows ─────────────────────────────────────────────────────────────


def test_save_flow_bundles_by_member(state, workspace, monkeypatch, capsys):
    _two_turn_log(state)
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    _browse(
        monkeypatch,
        [("pick", "session"), ("pick", "#1"), ("pick", "Save"), _BACK, _CANCEL],
        workspace,
    )

    assert (state / "bundles" / aid / "manifest.json").exists()
    out = capsys.readouterr().out
    assert str(state / "bundles") in out.replace("\n", "")


def test_minimize_flow_and_repeat(state, workspace, monkeypatch):
    _two_turn_log(state)
    script = [("pick", "session"), ("pick", "#1"), ("pick", "Minimize"), _BACK, _CANCEL]
    monkeypatch.setattr(tbrowse, "minimize_bundle", _fake_minimize)

    _browse(monkeypatch, script, workspace)
    assert (state / "cassettes" / "trace-1" / "manifest.json").exists()

    _browse(monkeypatch, script, workspace)  # repeat replaces / reuses the CLI path
    assert (state / "cassettes" / "trace-1" / "manifest.json").exists()


def _fake_minimize(bundle_dir, dest_dir, config_path=None):
    """Stand-in cassette writer: real minimize needs a replayable bundle."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "manifest.json").write_text("{}", encoding="utf-8")
    return type("_Rep", (), {"cassette_dir": dest_dir, "span_count": 1, "source_span_count": 1})()


def test_verdict_flow_records(state, workspace, monkeypatch):
    _two_turn_log(state)

    _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("pick", "#1"),
            ("pick", "Verdict"),
            "fail",
            "wrong answer",
            "",
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    got = tverdict.read_verdicts(state, attempt_id="trace-1")
    assert len(got) == 1 and got[0].status == "fail" and got[0].why == "wrong answer" and got[0].notes is None


def test_full_cycle_merge_verdict_save_split(state, workspace, monkeypatch, capsys):
    """One browser session: merge -> verdict -> save -> split, each step's
    menu labels and action set coming from freshly rescanned state."""
    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("hit", "m", "#1"),
            ("pickall", ["#1", "#2"]),
            ("pick", "#1"),
            ("pick", "Verdict"),
            "fail",
            "",
            "",
            ("pick", "#1"),
            ("pick", "Save"),
            ("pick", "#1"),
            ("pick", "Split"),
            True,
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    selects = [(m, titles) for kind, m, titles in fake.prompts if kind == "select"]
    attempt_screens = [titles for m, titles in selects if m == "Attempt:"]
    # after merge: one row with a MERGED check, no more merge entry
    merged_rows = _data_rows(attempt_screens[1])
    assert sum(_cell_of(attempt_screens[1], t, "MERGED").strip() == "✓" for t in merged_rows) == 1
    assert not any("Merge attempts" in t for titles in attempt_screens for t in titles)
    # after verdict: the row shows it
    assert any("fail" in t for t in _data_rows(attempt_screens[2]))
    # after save: the action menu offers Unpin (auto-pin happened)
    action_screens = [titles for m, titles in selects if m == "Action:"]
    assert any("Unpin" in t for t in action_screens[2])
    # after split: two rows again, none merged
    split_rows = _data_rows(attempt_screens[4])
    assert len(split_rows) == 2
    assert not any(_cell_of(attempt_screens[4], t, "MERGED").strip() == "✓" for t in split_rows)

    assert tstore.definitions(state) == {}
    assert {"trace-1", "trace-2"} <= set(tstore.pins(state))  # split moved the pin down
    _assert_no_ids("".join(m for _, m, _ in fake.prompts))


def test_report_declined_still_refreshes(state, workspace, monkeypatch, capsys):
    """Report bundles (and pins) before confirming: a 'No' must still rescan."""
    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("pick", "#1"),
            ("pick", "Report (redact"),
            False,
            ("pick", "#1"),
            _BACK,
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    out = capsys.readouterr().out
    assert "Aborted" in out
    action_screens = [t for kind, m, t in fake.prompts if kind == "select" and m == "Action:"]
    assert any("Unpin" in title for title in action_screens[1])  # stale menus would still say Pin


def test_minimize_failure_after_pack_refreshes(state, workspace, monkeypatch, capsys):
    _two_turn_log(state)
    monkeypatch.setattr(
        tbrowse, "minimize_bundle", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom trace-1 cli:a"))
    )

    fake = _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("pick", "#1"),
            ("pick", "Minimize"),
            ("pick", "#1"),
            _BACK,
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    out = " ".join(capsys.readouterr().out.split())
    assert tbrowse._STALE_MESSAGE in out
    _assert_no_ids(out.split("Scanning")[0] if "Scanning" in out else out)
    action_screens = [t for kind, m, t in fake.prompts if kind == "select" and m == "Action:"]
    assert any("Unpin" in title for title in action_screens[1])


def test_action_outputs_carry_no_ids(state, workspace, monkeypatch, capsys):
    """Non-artifact actions must never print ids; artifact paths may."""
    _two_turn_log(state)

    _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("hit", "m", "#1"),
            ("pickall", ["#1", "#2"]),
            ("pick", "#1"),
            ("pick", "Verdict"),
            "pass",
            "",
            "",
            ("pick", "#1"),
            ("pick", "Pin"),
            "keep",
            ("pick", "#1"),
            ("pick", "Unpin"),
            True,
            ("pick", "#1"),
            ("pick", "Split"),
            True,
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    _assert_no_ids(capsys.readouterr().out)


@pytest.mark.parametrize(
    "script, patch_target",
    [
        ([("pick", "session"), ("pick", "#1"), ("pick", "Save"), _BACK, _CANCEL], "collect_bundle"),
        (
            [("pick", "session"), ("pick", "#1"), ("pick", "Report (redact"), _BACK, _CANCEL],
            "collect_bundle",
        ),
        (
            [("pick", "session"), ("pick", "#1"), ("pick", "Minimize"), _BACK, _CANCEL],
            "collect_bundle",
        ),
        (
            [
                ("pick", "session"),
                ("hit", "m", "#1"),
                ("pickall", ["#1", "#2"]),
                _BACK,
                _CANCEL,
            ],
            "merge_attempts",
        ),
        (
            [("pick", "session"), ("pick", "#1"), ("pick", "Pin"), "keep", _BACK, _CANCEL],
            "pin_attempt",
        ),
    ],
    ids=["save", "report", "minimize", "merge", "pin"],
)
def test_failed_actions_show_safe_message(state, workspace, monkeypatch, capsys, script, patch_target):
    """Stale/invalid inputs fail with the fixed id-free message and still
    refresh (the data-layer error text embeds ids and must stay off-screen)."""
    _two_turn_log(state)

    def _raise(*_a, **_k):
        raise LookupError("unknown id 'trace-1': not a definition, alias, or recorded trace (cli:a)")

    if patch_target == "collect_bundle":
        monkeypatch.setattr(tbrowse, "collect_bundle", _raise)
        monkeypatch.setattr(tbrowse.tcmd, "collect_bundle", _raise)
    else:
        monkeypatch.setattr(tbrowse.tstore, patch_target, _raise)

    _browse(monkeypatch, script, workspace)

    out = " ".join(capsys.readouterr().out.split())
    assert tbrowse._STALE_MESSAGE in out
    _assert_no_ids(out)


def test_failed_split_shows_safe_message(state, workspace, monkeypatch, capsys):
    """Split's controlled-exception path (not the None sentinel) must present
    the fixed id-free message and keep the browser usable."""
    _two_turn_log(state)
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    def _raise(*_a, **_k):
        raise LookupError(f"stale definition {aid} member trace-1 of cli:a")

    monkeypatch.setattr(tbrowse.tstore, "split_attempt", _raise)

    fake = _browse(
        monkeypatch,
        [("pick", "session"), ("pick", "#1"), ("pick", "Split"), True, _BACK, _CANCEL],
        workspace,
    )

    out = " ".join(capsys.readouterr().out.split())
    assert tbrowse._STALE_MESSAGE in out
    _assert_no_ids(out, (*_IDS, aid))
    assert fake.answers == []  # the refreshed menu stayed operable to the end


def test_artifact_action_outputs_confine_ids_to_paths(state, workspace, monkeypatch, capsys, tmp_path):
    """Save/Report/Minimize success output may carry ids only inside artifact
    paths (state-root prefixed); everything else must be id-free."""
    from rich.console import Console

    wide = Console(width=400)
    monkeypatch.setattr(tbrowse, "console", wide)
    monkeypatch.setattr(tbrowse.tcmd, "console", wide)
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("raven.config.loader._current_config_path", cfg)
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: workspace)
    _two_turn_log(state)
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    monkeypatch.setattr(tbrowse, "minimize_bundle", _fake_minimize)

    _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("pick", "#1"),
            ("pick", "Save"),
            ("pick", "#1"),
            ("pick", "Minimize"),
            ("pick", "#1"),
            ("pick", "Report (redact"),
            True,
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    out = capsys.readouterr().out
    assert "Bundled the selected attempt" in out  # the browser's id-free progress note
    assert (state / "reports" / f"{aid}.tar.gz").exists()
    residue = " ".join(token for token in out.split() if str(state) not in token)
    _assert_no_ids(residue, (*_IDS, aid))


def test_split_none_shows_stale_not_success(state, workspace, monkeypatch, capsys):
    _two_turn_log(state)
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)

    def _confirm_then_drop():
        tstore.split_attempt(aid, state_dir=state)  # concurrent split wins the race
        return True

    _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("pick", "#1"),
            ("pick", "Split"),
            _confirm_then_drop,
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    out = capsys.readouterr().out
    assert "no longer merged" in out
    assert "Split into" not in out
    _assert_no_ids(out)


def test_unpin_false_shows_stale_not_success(state, workspace, monkeypatch, capsys):
    _two_turn_log(state)
    tstore.pin("trace-1", state_dir=state)

    def _confirm_then_unpin():
        tstore.unpin_attempt("trace-1", state)  # concurrent unpin wins the race
        return True

    _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("pick", "#1"),
            ("pick", "Unpin"),
            _confirm_then_unpin,
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    out = capsys.readouterr().out
    assert "Nothing was pinned" in out
    assert "Unpinned the selected attempt" not in out
    _assert_no_ids(out)


def test_stale_pin_shows_safe_message(state, workspace, monkeypatch, capsys):
    _two_turn_log(state)

    def _reason_then_wipe():
        (state / "logs" / "audit-spans.log").write_text("", encoding="utf-8")
        return "keep"

    _browse(
        monkeypatch,
        [("pick", "session"), ("pick", "#1"), ("pick", "Pin"), _reason_then_wipe],
        workspace,
    )

    out = " ".join(capsys.readouterr().out.split())
    assert tbrowse._STALE_MESSAGE in out
    _assert_no_ids(out)


# ── empty states ──────────────────────────────────────────────────────


def test_empty_log_prints_notice_without_prompts(state, workspace, monkeypatch, capsys):
    fake = _browse(monkeypatch, [], workspace)
    assert "No trajectories found" in capsys.readouterr().out
    assert fake.prompts == []


def test_all_defective_records_prints_notice(state, workspace, monkeypatch, capsys):
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "audit-spans.log").write_text('not json\n{"traceId": 5}\n', encoding="utf-8")
    fake = _browse(monkeypatch, [], workspace)
    assert "No trajectories found" in capsys.readouterr().out
    assert fake.prompts == []


def test_data_wiped_after_action_ends_controlled(state, workspace, monkeypatch, capsys):
    _two_turn_log(state)

    def _notes_then_wipe():
        (state / "logs" / "audit-spans.log").write_text("", encoding="utf-8")
        return ""

    fake = _browse(
        monkeypatch,
        [("pick", "session"), ("pick", "#1"), ("pick", "Verdict"), "pass", "", _notes_then_wipe],
        workspace,
    )

    assert "No trajectories found" in capsys.readouterr().out
    assert fake.answers == []


# ── cancellation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "script",
    [
        [("pick", "session"), ("pick", "#1"), ("pick", "Report (redact"), _CANCEL],
        [("pick", "session"), ("hit", "m", "#1"), _CANCEL],
        [("pick", "session"), ("pick", "#1"), ("pick", "Verdict"), "fail", _CANCEL],
        [("pick", "session"), ("pick", "#1"), ("pick", "Split"), _CANCEL],
        [("pick", "session"), ("pick", "#1"), ("pick", "Unpin"), _CANCEL],
        [_CANCEL],
    ],
    ids=["report-confirm", "merge-checkbox", "verdict-text", "split-confirm", "unpin-confirm", "session-select"],
)
def test_cancel_exits_cleanly_without_further_prompts(state, workspace, monkeypatch, capsys, script):
    _two_turn_log(state)
    if "Split" in str(script):
        tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    if "Unpin" in str(script):
        tstore.pin("trace-1", state_dir=state)

    fake = _browse(monkeypatch, script, workspace)

    out = capsys.readouterr().out
    assert "Cancelled" in out
    assert tbrowse._STALE_MESSAGE not in out
    assert fake.answers == []  # nothing consumed past the cancel
    assert len(fake.prompts) == len(script)


def test_ask_prefers_unsafe_ask_and_maps_ctrl_c():
    """_ask must take the unsafe_ask path and convert KeyboardInterrupt into
    the browser-wide cancel: the safe ask() fallback would print questionary's
    own "Cancelled by user" line next to the browser's exit notice."""

    class _Prompt:
        def unsafe_ask(self):
            raise KeyboardInterrupt

        def ask(self):
            raise AssertionError("safe ask() must not be used when unsafe_ask exists")

    with pytest.raises(tbrowse._CancelledError):
        tbrowse._ask(_Prompt())


def test_escape_walks_up_one_level_at_a_time(state, workspace, monkeypatch, capsys):
    """Esc: action -> attempt list -> session list, then Ctrl+C quits."""
    _two_turn_log(state)

    fake = _browse(monkeypatch, [("pick", "session"), ("pick", "#1"), _BACK, _BACK, _CANCEL], workspace)

    messages = [m for kind, m, _ in fake.prompts if kind == "select"]
    assert messages == ["Session:", "Attempt:", "Action:", "Attempt:", "Session:"]
    assert capsys.readouterr().out.count("Cancelled") == 1  # only the final Ctrl+C


def test_escape_on_top_level_stays_until_ctrl_c(state, workspace, monkeypatch, capsys):
    """Esc on the session screen is a guarded no-op (reflexive Esc must not
    drop the browser); Ctrl+C is the only quit."""
    _two_turn_log(state)
    fake = _browse(monkeypatch, [_BACK, _BACK, _CANCEL], workspace)
    assert [m for _, m, _ in fake.prompts] == ["Session:", "Session:", "Session:"]
    assert "Cancelled" in capsys.readouterr().out


def test_menu_screens_hide_back_exit_and_show_help(state, workspace, monkeypatch):
    """No Back/Exit entries anywhere; every list screen opens with its help
    separators directly under the title."""
    _two_turn_log(state)

    fake = _browse(monkeypatch, [("pick", "session"), ("pick", "#1"), _BACK, _BACK, _CANCEL], workspace)

    screens = {m: titles for kind, m, titles in fake.prompts if kind == "select"}
    for message, help_lines in [
        ("Session:", tbrowse._HELP_SESSION),
        ("Attempt:", tbrowse._HELP_ATTEMPT_MERGE),  # two attempts: merge advertised
        ("Action:", tbrowse._HELP_ACTION),
    ]:
        titles = screens[message]
        assert titles[: len(help_lines)] == list(help_lines)
        assert "Back" not in titles and "Exit" not in titles
    # the table header separator sits right after the help lines
    assert "LAST ACTIVITY" in screens["Session:"][len(tbrowse._HELP_SESSION)]
    assert "STARTED" in screens["Attempt:"][len(tbrowse._HELP_ATTEMPT_MERGE)]


@pytest.mark.parametrize(
    "extra_answers, action_pick, prep, absent",
    [
        ([], "Merge attempts", None, "Merged"),
        ([], "Verdict", None, "Recorded verdict"),
        (["fail"], "Verdict", None, "Recorded verdict"),
        (["fail", "why"], "Verdict", None, "Recorded verdict"),
        ([], "Pin", None, "Pinned"),
        ([], "Unpin", "pin", "Unpinned"),
        ([], "Split", "merge", "Split into"),
    ],
    ids=[
        "merge-checkbox",
        "verdict-status",
        "verdict-why",
        "verdict-notes",
        "pin-reason",
        "unpin-confirm",
        "split-confirm",
    ],
)
def test_escape_cancels_action_without_side_effects(
    state, workspace, monkeypatch, capsys, extra_answers, action_pick, prep, absent
):
    """Esc inside an action cancels just that action: no data change, no
    stale/error output, and the refreshed browser stays operable."""
    _two_turn_log(state)
    if prep == "merge":
        tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    if prep == "pin":
        tstore.pin("trace-1", state_dir=state)
    pins_before = set(tstore.pins(state))
    defs_before = dict(tstore.definitions(state))

    if action_pick == "Merge attempts":
        script = [("pick", "session"), ("hit", "m", "#1"), *extra_answers, _BACK, _BACK, _CANCEL]
    else:
        script = [("pick", "session"), ("pick", "#1"), ("pick", action_pick), *extra_answers, _BACK, _BACK, _CANCEL]

    fake = _browse(monkeypatch, script, workspace)

    out = capsys.readouterr().out
    assert fake.answers == []
    assert len(fake.prompts) == len(script)  # no extra prompt after the Esc
    assert absent not in out
    assert tbrowse._STALE_MESSAGE not in out
    assert out.count("Cancelled") == 1  # only the final Ctrl+C quit, not the Esc
    assert set(tstore.pins(state)) == pins_before
    assert dict(tstore.definitions(state)) == defs_before
    assert tverdict.read_verdicts(state) == []


def test_escape_on_report_confirm_keeps_bundle_and_pin(state, workspace, monkeypatch, capsys):
    """Esc on the report confirm skips the tarball only: bundling and the
    auto-pin happen before the confirm (the declined-report contract), so the
    refreshed action menu must offer Unpin."""
    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [("pick", "session"), ("pick", "#1"), ("pick", "Report (redact"), _BACK, ("pick", "#1"), _BACK, _BACK, _CANCEL],
        workspace,
    )

    out = capsys.readouterr().out
    assert fake.answers == []
    reports = state / "reports"
    assert not (reports.exists() and any(reports.glob("*.tar.gz")))
    assert "Report ready" not in out
    assert "Aborted" not in out  # that message belongs to the answered-No path
    assert tbrowse._STALE_MESSAGE not in out
    assert (state / "bundles" / "trace-1" / "manifest.json").exists()
    assert "trace-1" in tstore.pins(state)
    action_screens = [t for kind, m, t in fake.prompts if kind == "select" and m == "Action:"]
    assert any("Unpin" in title for title in action_screens[1])


# ── breadcrumbs ───────────────────────────────────────────────────────


def test_crumb_escapes_untrusted_text(monkeypatch):
    """Markup in a title must render literally (no color, no MarkupError),
    on a console that actually emits ANSI (pytest capture alone would pass
    vacuously with colors off)."""
    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    monkeypatch.setattr(tbrowse, "console", Console(file=buf, force_terminal=True, color_system="truecolor", width=200))

    tbrowse._crumb("Session", "x[red]y")
    tbrowse._crumb("Session", "pre[/dim]view")
    tbrowse._crumb("Session", "multi\nline\rwith\x07controls")

    out = buf.getvalue()
    assert "x[red]y" in out
    assert "pre[/dim]view" in out
    assert "multi line with controls" in out
    assert "\x07" not in out and "\r" not in out
    assert out.count("\n") == 3
    assert "\x1b[31" not in out


def test_breadcrumbs_echo_navigation(state, workspace, monkeypatch, capsys):
    from raven.session.manager import SessionManager

    manager = SessionManager(workspace)
    session = manager.get_or_create("cli:a")
    session.add_message("user", "x")
    session.set_title("x[red]y title")
    manager.save(session)
    _two_turn_log(state)

    _browse(
        monkeypatch,
        [("pick", "x[red]y"), ("pick", "#1"), ("pick", "Verdict"), "pass", "", "", _BACK, _CANCEL],
        workspace,
    )

    out = capsys.readouterr().out
    assert "Session ❯ x[red]y title" in out
    assert "Attempt ❯ #1" in out
    assert "Action ❯ Verdict" in out


# ── table layout ──────────────────────────────────────────────────────


def _mk_attempt(**kw):
    base = dict(
        key="trace-x",
        traces=("trace-x",),
        session_key="cli:a",
        start="2026-08-20T10:00:00+00:00",
        end="2026-08-20T10:00:01+00:00",
        spans=1,
        turns=1,
        verdict=None,
        pinned=False,
        preview=None,
        merged=False,
    )
    base.update(kw)
    return tbrowse.AttemptRow(**base)


def _flat(tokens):
    return "".join(t[1] for t in tokens)


def _row_cap(width):
    return width - tbrowse._ROW_INDENT - tbrowse._WIDTH_MARGIN


def test_attempt_table_fits_budget_and_drops_preview():
    rows = [
        _mk_attempt(verdict="pass", pinned=True, merged=True, preview="fix the bug in the long preview"),
        _mk_attempt(key="trace-y", preview="another attempt preview text"),
    ]

    h80, rows80 = tbrowse._attempt_table(rows, 80)
    h60, rows60 = tbrowse._attempt_table(rows, 60)

    assert "PREVIEW" in h80 and "PREVIEW" not in h60
    for width, header, row_tokens in [(80, h80, rows80), (60, h60, rows60)]:
        assert tbrowse._cell_width(header) <= _row_cap(width)
        for tokens in row_tokens:
            line = _flat(tokens)
            assert "\n" not in line
            assert tbrowse._cell_width(line) <= _row_cap(width)
            _assert_no_ids(line, ("trace-", "cli:a"))


def test_table_headers_complete_and_columns_aligned():
    rows = [_mk_attempt(verdict="pass", pinned=True, preview="p"), _mk_attempt(key="trace-y", merged=True)]

    header, row_tokens = tbrowse._attempt_table(rows, 100)

    for col in ("#", "STARTED", "TURNS", "SPANS", "VERDICT", "PIN", "MERGED", "PREVIEW"):
        assert col in header
    layout = tbrowse._attempt_layout(100, len(rows))
    gap = tbrowse._cell_width(tbrowse._COL_GAP)
    offsets, off = [], 0
    for _h, w in layout:
        offsets.append(off)
        off += w + gap
    for (h, _w), start in zip(layout, offsets):
        assert header[start : start + len(h)] == h
    lines = [header, *(_flat(t) for t in row_tokens)]
    for start in offsets[1:]:
        for line in lines:
            if len(line) >= start:
                assert line[start - gap : start] == tbrowse._COL_GAP


def test_session_table_time_column_and_fit():
    sessions = [
        tbrowse.SessionRow(
            key="cli:a", title="My debugging run", attempts=[_mk_attempt()], end="2026-08-20T11:22:33+00:00"
        ),
        tbrowse.SessionRow(key="cli:b", title="cli session", attempts=[_mk_attempt(key="t2")], end=None),
    ]

    header, rows = tbrowse._session_table(sessions, 80)

    assert "TITLE" in header and "ATTEMPTS" in header and "LAST ACTIVITY" in header
    line0, line1 = _flat(rows[0]), _flat(rows[1])
    assert "2026-08-20 11:22" in line0
    assert line1.rstrip().endswith("?")
    for line in (line0, line1):
        assert tbrowse._cell_width(line) <= _row_cap(80)


def test_table_min_width_boundary_and_growth():
    rows = [_mk_attempt(preview="p", verdict="pass")]
    floor = tbrowse._table_min_width(tbrowse._attempt_fixed(len(rows)))

    h_at, rows_at = tbrowse._attempt_table(rows, floor)
    h_below, _ = tbrowse._attempt_table(rows, floor - 1)

    assert "PREVIEW" not in h_at
    assert tbrowse._cell_width(h_at) <= _row_cap(floor)
    assert all(tbrowse._cell_width(_flat(t)) <= _row_cap(floor) for t in rows_at)
    assert h_below == h_at  # the tightest layout stops deforming below the floor
    assert tbrowse._cell_width(h_below) > _row_cap(floor - 1)  # the declared clipping floor
    assert (
        tbrowse._table_min_width(tbrowse._attempt_fixed(10)) == tbrowse._table_min_width(tbrowse._attempt_fixed(9)) + 1
    )


def test_table_cjk_cells_stay_within_budget():
    rows = [_mk_attempt(preview="宽字符预览" * 8, verdict="pass")]

    _header, row_tokens = tbrowse._attempt_table(rows, 80)
    assert tbrowse._cell_width(_flat(row_tokens[0])) <= _row_cap(80)

    sessions = [tbrowse.SessionRow(key="cli:a", title="中文标题" * 12, attempts=rows, end=None)]
    _sh, srows = tbrowse._session_table(sessions, 80)
    assert tbrowse._cell_width(_flat(srows[0])) <= _row_cap(80)


def test_table_sanitizes_untrusted_cells(state, workspace):
    """A corrupt verdict sidecar or malformed timestamp must not split a table
    row: read_verdicts only guarantees a non-empty string status, and cwidth
    math alone would let newlines and control characters through."""
    span = _span("trace-1", session_key="cli:a")
    span["startTime"] = "2026-\n08\t20T1\x07:00:00"
    span["endTime"] = "2026-\r08-20T11:00:00"
    _write_log(state / "logs" / "audit-spans.log", [span])
    (state / "verdicts.jsonl").write_text(
        json.dumps({"attempt_id": "trace-1", "status": "bad\nsta\rtus\x07x", "source": "user", "ts": "t"}) + "\n",
        encoding="utf-8",
    )

    (srow,) = tbrowse.scan_sessions(workspace)
    _ah, row_tokens = tbrowse._attempt_table(srow.attempts, 100)
    _sh, srows = tbrowse._session_table([srow], 100)

    for line in [_flat(row_tokens[0]), _flat(srows[0])]:
        assert "\n" not in line and "\r" not in line and "\x07" not in line
        assert tbrowse._cell_width(line) <= _row_cap(100)


def test_boolean_cells_use_success_style_and_resolve_green():
    rows = [_mk_attempt(pinned=True, merged=False), _mk_attempt(key="t2", pinned=False, merged=True)]

    _h, row_tokens = tbrowse._attempt_table(rows, 100)

    assert [(s, t) for s, t in row_tokens[0] if t == "✓"] == [("class:success", "✓")]
    assert [(s, t) for s, t in row_tokens[1] if t == "✓"] == [("class:success", "✓")]

    questionary = pytest.importorskip("questionary")
    create_pipe_input, DummyOutput = _pipe_io()
    from raven.cli._theme import PALETTE, build_questionary_style

    with create_pipe_input() as pipe:
        q = questionary.select(
            "m",
            choices=[questionary.Choice(tokens, value=i) for i, tokens in enumerate(row_tokens)],
            input=pipe,
            output=DummyOutput(),
        )
        tbrowse._inject_bindings(q)
        control = tbrowse._find_inquirer_control(q.application)
        styles = [tok[0] for tok in control.text() if tok[1] == "✓"]

    style = build_questionary_style("dark")
    success = PALETTE["dark"]["success"].lstrip("#")
    pointed = style.get_attrs_for_style_str(styles[0])
    plain = style.get_attrs_for_style_str(styles[1])
    assert pointed.color == success and pointed.bold
    assert plain.color == success and not plain.bold


def test_below_floor_rows_render_clipped_and_selectable():
    """Below the floor the real renderer clips overlong lines at the terminal
    edge (prompt_toolkit never wraps option rows) and the menu stays usable.

    The width puts the MERGED column entirely beyond the right edge: a
    clipping renderer never emits that column name at all, while a wrapping
    one would write it contiguously on a continuation row — so its absence
    from the raw output stream pins clipping without any ANSI parsing."""
    import io

    questionary = pytest.importorskip("questionary")
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.vt100 import Vt100_Output

    create_pipe_input, _dummy = _pipe_io()

    rows = [_mk_attempt(verdict="pass", pinned=True, merged=True)]
    layout = tbrowse._attempt_fixed(len(rows))
    width = tbrowse._table_min_width(layout) - 10
    gap = tbrowse._cell_width(tbrowse._COL_GAP)
    merged_start = tbrowse._ROW_INDENT + sum(w + gap for _h, w in layout[:-1])
    assert merged_start >= width  # premise: MERGED lies fully beyond the edge
    header, row_tokens = tbrowse._attempt_table(rows, width)

    class _Buf(io.StringIO):
        encoding = "utf-8"

    buf = _Buf()
    output = Vt100_Output(buf, lambda: Size(rows=24, columns=width), term="xterm-256color")
    with create_pipe_input() as pipe:
        q = questionary.select(
            "m",
            choices=[questionary.Separator(header), questionary.Choice(row_tokens[0], value="v")],
            input=pipe,
            output=output,
        )
        tbrowse._inject_bindings(q)
        pipe.send_text("\r")
        assert q.application.run() == "v"

    raw = buf.getvalue()
    assert "VERDICT" in raw  # the visible region reached the stream
    assert "MERGED" not in raw  # the clipped tail was never emitted, so no wrap row exists


# ── space preview ─────────────────────────────────────────────────────


def _turn_span(trace_id, span_id, start, inp=None, out=None):
    s = _span(trace_id, session_key="cli:a", span_id=span_id)
    s["startTime"] = start
    if inp is not None:
        s["attributes"]["turn.input_preview"] = inp
    if out is not None:
        s["attributes"]["turn.output_preview"] = out
    return s


def test_turn_previews_sorted_and_table_preview_derived(state, workspace):
    """Turn order and the table PREVIEW cell both come from the sorted
    collection, never from log source order."""
    spans = [
        _turn_span("trace-1", "s3", "2026-08-20T10:02:00+00:00", inp="third in", out="third out"),
        _turn_span("trace-1", "s1", "2026-08-20T10:00:00+00:00", inp="first in"),
        _turn_span("trace-1", "s2", "2026-08-20T10:01:00+00:00", out="second out"),
    ]
    _write_log(state / "logs" / "audit-spans.log", spans)
    (srow,) = tbrowse.scan_sessions(workspace)
    (row,) = srow.attempts

    assert [t.input for t in row.turn_previews] == ["first in", "", "third in"]
    assert [t.output for t in row.turn_previews] == ["", "second out", "third out"]
    assert row.preview == "first in"

    _write_log(state / "logs" / "audit-spans.log", list(reversed(spans)))
    (srow2,) = tbrowse.scan_sessions(workspace)
    (row2,) = srow2.attempts
    assert row2.turn_previews == row.turn_previews
    assert row2.preview == "first in"


def test_turn_previews_checkpoint_final_dedup(state, workspace):
    checkpoint = _turn_span("trace-1", "root", "2026-08-20T10:00:00+00:00", inp="ask")
    final = _turn_span("trace-1", "root", "2026-08-20T10:00:00+00:00", inp="ask", out="answer")
    _write_log(state / "logs" / "audit-spans.log", [checkpoint, final])

    (srow,) = tbrowse.scan_sessions(workspace)
    (row,) = srow.attempts

    (turn,) = row.turn_previews
    assert turn.input == "ask" and turn.output == "answer"  # the final record won


def test_turn_previews_missing_span_ids_stable(state, workspace):
    """Same start, one record with a spanId and one without: no TypeError,
    and the order does not follow the input order."""
    start = "2026-08-20T10:00:00+00:00"
    a = _turn_span("trace-1", "sX", start, inp="with id")
    b = _turn_span("trace-1", "tmp", start, inp="without id")
    del b["spanId"]

    _write_log(state / "logs" / "audit-spans.log", [a, b])
    (srow,) = tbrowse.scan_sessions(workspace)
    (row,) = srow.attempts
    assert len(row.turn_previews) == 2

    _write_log(state / "logs" / "audit-spans.log", [b, a])
    (srow2,) = tbrowse.scan_sessions(workspace)
    (row2,) = srow2.attempts
    assert row2.turn_previews == row.turn_previews


def test_turn_previews_sanitize_types_and_length(state, workspace):
    bad = _turn_span("trace-1", "s1", "2026-08-20T10:00:00+00:00")
    bad["attributes"]["turn.input_preview"] = 123
    long_turn = _turn_span("trace-1", "s2", "2026-08-20T10:01:00+00:00", inp="x" * 500, out="y\nz\x07w")
    _write_log(state / "logs" / "audit-spans.log", [bad, long_turn])

    (srow,) = tbrowse.scan_sessions(workspace)
    (row,) = srow.attempts

    t1, t2 = row.turn_previews
    assert t1.input == ""
    assert len(t2.input) == 400 and t2.input.endswith("…")
    assert t2.output == "y z w"
    assert len(row.preview) == 32 and row.preview.endswith("…")


def test_preview_screen_placeholder_and_escape(monkeypatch):
    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    monkeypatch.setattr(tbrowse, "console", Console(file=buf, force_terminal=True, color_system="truecolor", width=500))

    tbrowse._preview_screen(1, _mk_attempt())
    tbrowse._preview_screen(
        2, _mk_attempt(turn_previews=(tbrowse._TurnPreview("s", "i", "t", "x[red]y", "pre[/dim]view"),))
    )
    tbrowse._preview_screen(3, _mk_attempt(turn_previews=(tbrowse._TurnPreview("s", "i", "t", "", ""),)))

    out = buf.getvalue()
    assert "(no turns recorded)" in out
    assert "x[red]y" in out and "pre[/dim]view" in out
    assert "(no preview recorded)" in out  # turns exist, but nothing is displayable
    assert "\x1b[31" not in out


@pytest.mark.parametrize(
    "key, done",
    [("x", True), ("\x1b", True), ("\x1b[A", True), ("\x7f", True), ("\x03", False)],
    ids=["any-key", "esc", "up", "backspace", "ctrl-c"],
)
def test_preview_wait_key_paths(key, done):
    """The waiter maps any key/Esc to the non-None _DONE; only Ctrl+C keeps
    the browser-wide cancel result (None)."""
    questionary = pytest.importorskip("questionary")
    create_pipe_input, DummyOutput = _pipe_io()

    with create_pipe_input() as pipe:
        q = tbrowse._wait_question(questionary, None, input=pipe, output=DummyOutput())
        assert q.application.ttimeoutlen == tbrowse._ESC_FLUSH_TIMEOUT
        pipe.send_text(key)
        result = q.application.run()

    assert (result is tbrowse._DONE) if done else (result is None)


def test_preview_wait_passes_cpr_response_through():
    """A terminal's CPR reply is an internal event, not a keypress: it must
    reach the renderer (default semantics) and never finish the waiter."""
    import threading

    questionary = pytest.importorskip("questionary")
    create_pipe_input, DummyOutput = _pipe_io()

    with create_pipe_input() as pipe:
        q = tbrowse._wait_question(questionary, None, input=pipe, output=DummyOutput())
        reported = []
        q.application.renderer.report_absolute_cursor_row = reported.append
        result = []
        thread = threading.Thread(target=lambda: result.append(q.application.run()), daemon=True)
        thread.start()
        pipe.send_text("\x1b[35;1R")
        thread.join(timeout=1)
        assert thread.is_alive()  # the CPR reply alone must not finish the waiter
        pipe.send_text("x")
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert result == [tbrowse._DONE]
    assert reported == [35]


def test_space_key_binding_returns_pointed_attempt():
    """The real Space keypress must reach the injected binding (questionary
    has its own catch-all bindings a fake-returned _KeyHit would bypass)."""
    questionary = pytest.importorskip("questionary")
    create_pipe_input, DummyOutput = _pipe_io()

    with create_pipe_input() as pipe:
        q = questionary.select(
            "m",
            choices=[questionary.Choice("first", value=(1, "row")), questionary.Choice("second", value=(2, "row"))],
            input=pipe,
            output=DummyOutput(),
        )
        tbrowse._inject_bindings(q, extra_keys=(" ",))
        pipe.send_text(" ")
        hit = q.application.run()

    assert isinstance(hit, tbrowse._KeyHit)
    assert hit.key == " " and hit.value == (1, "row")


def test_space_previews_and_keeps_cursor(state, workspace, monkeypatch, capsys):
    _two_turn_log(state)
    assert "[Space] preview" in tbrowse._HELP_ATTEMPT[1]
    scans = []
    real_scan = tbrowse.scan_sessions
    monkeypatch.setattr(tbrowse, "scan_sessions", lambda ws: scans.append(1) or real_scan(ws))

    fake = _browse(
        monkeypatch,
        [("pick", "session"), ("hit", " ", "#1"), tbrowse._DONE, _BACK, _CANCEL],
        workspace,
    )

    out = capsys.readouterr().out
    assert "Preview ❯ #1" in out
    assert "fix the bug" in out
    assert [k for k, _m, _t in fake.prompts] == ["select", "select", "press", "select", "select"]
    attempt_calls = [kw for kind, m, kw in fake.calls if kind == "select" and m == "Attempt:"]
    assert attempt_calls[0].get("default") is None
    kept = attempt_calls[1].get("default")
    assert kept is not None and kept[0] == 1
    assert len(scans) == 2  # the preview itself never rescans


def test_non_space_keyhit_on_attempt_row_is_noop(state, workspace, monkeypatch, capsys):
    """Dispatch is by key, not by value shape: a foreign injected key
    pointing at an attempt tuple must not fall into the preview path."""
    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [("pick", "session"), ("hit", "z", "#1"), _BACK, _CANCEL],
        workspace,
    )

    out = capsys.readouterr().out
    assert "Preview ❯" not in out
    assert "press" not in [k for k, _m, _t in fake.prompts]
    attempt_calls = [kw for kind, m, kw in fake.calls if kind == "select" and m == "Attempt:"]
    kept = attempt_calls[1]["default"]
    assert kept is not None and kept[0] == 1  # cursor kept on the row
    assert fake.answers == []


@pytest.mark.parametrize("key", ["m", "M"])
def test_m_key_opens_merge_and_merges(state, workspace, monkeypatch, capsys, key):
    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [("pick", "session"), ("hit", key, "#2"), ("pickall", ["#1", "#2"]), _BACK, _CANCEL],
        workspace,
    )

    assert len(tstore.definitions(state)) == 1
    assert "Merged 2 attempts into one" in capsys.readouterr().out
    assert [k for k, _m, _t in fake.prompts if k == "checkbox"] == ["checkbox"]


def test_merge_checkbox_shows_help_and_suppresses_instruction(state, workspace, monkeypatch):
    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [("pick", "session"), ("hit", "m", "#1"), _BACK, _BACK, _CANCEL],
        workspace,
    )

    (checkbox,) = [
        (titles, kw) for (kind, _m, titles), (_k, _m2, kw) in zip(fake.prompts, fake.calls) if kind == "checkbox"
    ]
    titles, kw = checkbox
    assert titles[0] == tbrowse._MERGE_HINT
    assert kw.get("instruction") == " "


def test_merge_key_only_bound_with_multiple_attempts(state, workspace, monkeypatch):
    _two_turn_log(state)
    recorded = []
    real = tbrowse._select_screen

    def spy(questionary, style, message, help_lines, choices, **kw):
        recorded.append((message, help_lines, kw.get("extra_keys", ())))
        return real(questionary, style, message, help_lines, choices, **kw)

    monkeypatch.setattr(tbrowse, "_select_screen", spy)
    _browse(monkeypatch, [("pick", "session"), _BACK, _CANCEL], workspace)

    message, help_lines, extra_keys = next(r for r in recorded if r[0] == "Attempt:")
    assert "[M] merge" in help_lines[1]
    assert {"m", "M"} <= set(extra_keys)


def test_merge_key_absent_with_single_attempt(state, workspace, monkeypatch):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    recorded = []
    real = tbrowse._select_screen

    def spy(questionary, style, message, help_lines, choices, **kw):
        recorded.append((message, help_lines, kw.get("extra_keys", ()), [getattr(c, "title", "") for c in choices]))
        return real(questionary, style, message, help_lines, choices, **kw)

    monkeypatch.setattr(tbrowse, "_select_screen", spy)
    fake = _browse(monkeypatch, [("pick", "session"), _BACK, _CANCEL], workspace)

    message, help_lines, extra_keys, _titles = next(r for r in recorded if r[0] == "Attempt:")
    assert "[M] merge" not in help_lines[1]
    assert "m" not in extra_keys and "M" not in extra_keys
    assert not any("Merge attempts" in t for _k, _m, titles in fake.prompts for t in titles)


# ── real questionary key bindings ─────────────────────────────────────


def _pipe_io():
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    return create_pipe_input, DummyOutput


@pytest.mark.parametrize("kind", ["select", "checkbox", "text", "confirm"])
def test_escape_binding_installs_on_every_prompt_kind(kind):
    """text/confirm expose a read-only _MergedKeyBindings: the merge-based
    injection must install on all four prompt kinds, not just select."""
    questionary = pytest.importorskip("questionary")
    create_pipe_input, DummyOutput = _pipe_io()

    with create_pipe_input() as pipe:
        kwargs = {"input": pipe, "output": DummyOutput()}
        if kind == "select":
            q = questionary.select("m", choices=["a"], **kwargs)
        elif kind == "checkbox":
            q = questionary.checkbox("m", choices=["a"], **kwargs)
        elif kind == "text":
            q = questionary.text("m", **kwargs)
        else:
            q = questionary.confirm("m", **kwargs)
        tbrowse._inject_bindings(q)
        assert q.application.erase_when_done is True
        assert q.application.ttimeoutlen == tbrowse._ESC_FLUSH_TIMEOUT
        pipe.send_text("\x1b")
        assert q.application.run() is tbrowse._BACK


@pytest.mark.parametrize("key", ["m", "M"])
def test_extra_key_reports_pointed_row(key):
    questionary = pytest.importorskip("questionary")
    create_pipe_input, DummyOutput = _pipe_io()

    with create_pipe_input() as pipe:
        q = questionary.select(
            "m",
            choices=[questionary.Choice("first", value="v1"), questionary.Choice("second", value="v2")],
            input=pipe,
            output=DummyOutput(),
        )
        tbrowse._inject_bindings(q, extra_keys=("m", "M"))
        pipe.send_text(key)
        hit = q.application.run()

    assert isinstance(hit, tbrowse._KeyHit)
    assert hit.key == key and hit.value == "v1"


def test_pointed_row_restyle_keeps_semantic_color():
    """The pointed row gains bold without a foreground override: a green cell
    resolves to the success color on the pointed row and the plain row alike
    (asserting final attrs, not class names)."""
    questionary = pytest.importorskip("questionary")
    create_pipe_input, DummyOutput = _pipe_io()
    from raven.cli._theme import PALETTE, build_questionary_style

    def title(word):
        return [("class:success", "✓"), ("class:text", f" {word}")]

    with create_pipe_input() as pipe:
        q = questionary.select(
            "m",
            choices=[questionary.Choice(title("one"), value="1"), questionary.Choice(title("two"), value="2")],
            input=pipe,
            output=DummyOutput(),
        )
        tbrowse._inject_bindings(q)
        control = tbrowse._find_inquirer_control(q.application)
        checks = [tok[0] for tok in control.text() if "class:success" in tok[0]]

    style = build_questionary_style("dark")
    success = PALETTE["dark"]["success"].lstrip("#")
    pointed = style.get_attrs_for_style_str(checks[0])
    plain = style.get_attrs_for_style_str(checks[1])
    assert pointed.color == success and pointed.bold
    assert plain.color == success and not plain.bold


def test_separator_lines_restyle_to_help_color():
    """Help lines and table headers render with the readable ``help`` class,
    not questionary's near-invisible ``separator`` — asserted on final attrs."""
    questionary = pytest.importorskip("questionary")
    create_pipe_input, DummyOutput = _pipe_io()
    from raven.cli._theme import PALETTE, build_questionary_style

    with create_pipe_input() as pipe:
        q = questionary.select(
            "m",
            choices=[questionary.Separator("help line"), questionary.Choice("row", value="v")],
            input=pipe,
            output=DummyOutput(),
        )
        tbrowse._inject_bindings(q)
        control = tbrowse._find_inquirer_control(q.application)
        (sep_style,) = [tok[0] for tok in control.text() if tok[1] == "help line"]

    assert "class:separator" not in sep_style
    style = build_questionary_style("dark")
    assert style.get_attrs_for_style_str(sep_style).color == PALETTE["dark"]["help"].lstrip("#")


def test_selects_share_prompt_chrome(state, workspace, monkeypatch):
    """Every select/checkbox passes the shared QMARK and POINTER so the
    browser's prompt chrome matches the rest of the CLI."""
    from raven.cli._theme import POINTER, QMARK

    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("hit", "m", "#1"),
            ("pickall", ["#1", "#2"]),
            ("pick", "#1"),
            ("pick", "Verdict"),
            "pass",
            "",
            "",
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    pickers = [(m, kw) for kind, m, kw in fake.calls if kind in ("select", "checkbox")]
    # The flow must actually visit every picker call site before the chrome
    # check means anything (a short script would silently skip Action/Verdict).
    assert {"Session:", "Attempt:", "Action:", "Verdict:", "Select 2+ attempts to merge:"} <= {m for m, _ in pickers}
    for _m, kw in pickers:
        assert kw.get("qmark") == QMARK
        assert kw.get("pointer") == POINTER


def test_merge_checkbox_validates_minimum(state, workspace, monkeypatch):
    """The checkbox itself refuses <2 picks; an empty submit must not reach
    the data layer and read as a stale/rejected error."""
    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("hit", "m", "#1"),
            ("pickall", ["#1", "#2"]),
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    (checkbox_kw,) = [kw for kind, _m, kw in fake.calls if kind == "checkbox"]
    validate = checkbox_kw["validate"]
    assert validate([]) != True  # noqa: E712 - questionary contract: True or an error message
    assert validate(["only-one"]) != True  # noqa: E712
    assert validate(["a", "b"]) is True


def test_pin_flow_uses_locked_pin_attempt(state, workspace, monkeypatch):
    _two_turn_log(state)
    calls = []
    monkeypatch.setattr(
        tbrowse.tstore, "pin_attempt", lambda id_, *, reason="", state_dir=None: calls.append((id_, reason)) or id_
    )
    monkeypatch.setattr(
        tbrowse.tstore,
        "pin",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser must pin via pin_attempt")),
    )

    _browse(
        monkeypatch,
        [("pick", "session"), ("pick", "#1"), ("pick", "Pin"), "keep", _BACK, _CANCEL],
        workspace,
    )

    assert calls == [("trace-1", "keep")]


# ── dependency and import boundaries ──────────────────────────────────


def test_commands_import_does_not_load_questionary():
    code = "import sys; import raven.cli.trajectory_commands; assert 'questionary' not in sys.modules"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_bare_browser_without_questionary_hints_install(state, workspace, monkeypatch, capsys):
    import builtins

    real_import = builtins.__import__

    def _no_questionary(name, *args, **kwargs):
        if name == "questionary":
            raise ModuleNotFoundError("No module named 'questionary'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_questionary)
    import typer

    with pytest.raises(typer.Exit):
        tbrowse.browse_trajectories(workspace=workspace)

    assert "questionary" in capsys.readouterr().out


def test_subcommands_work_without_questionary(state, monkeypatch):
    import builtins

    from typer.testing import CliRunner

    from raven.cli.trajectory_commands import trajectory_app

    real_import = builtins.__import__

    def _no_questionary(name, *args, **kwargs):
        if name == "questionary":
            raise ModuleNotFoundError("No module named 'questionary'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_questionary)
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1")])

    r = CliRunner().invoke(trajectory_app, ["list"])

    assert r.exit_code == 0
    assert "trace-1" in r.stdout


# ── workspace propagation ─────────────────────────────────────────────


def test_nondefault_workspace_flows_into_save_and_minimize(state, tmp_path, monkeypatch):
    from raven.session.manager import SessionManager

    other = tmp_path / "other-ws"
    manager = SessionManager(other)
    session = manager.get_or_create("cli:a")
    session.add_message("user", "hello")
    manager.save(session)
    _two_turn_log(state)
    aid = tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
    monkeypatch.setattr(tbrowse, "minimize_bundle", _fake_minimize)

    _browse(
        monkeypatch,
        [
            ("pick", "hello"),
            ("pick", "#1"),
            ("pick", "Save"),
            ("pick", "#1"),
            ("pick", "Minimize"),
            _BACK,
            _CANCEL,
        ],
        other,
    )

    manifest = json.loads((state / "bundles" / aid / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_included"] is True
    assert (state / "bundles" / aid / "session.jsonl").exists()
    assert (state / "cassettes" / aid / "manifest.json").exists()


# ── report-a-bug flow ──────────────────────────────────────────────────

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIabcdef\n-----END PRIVATE KEY-----"
_ENTROPY_TOKEN = "aB3xK9mQ7pL2vR8sT4wZ6yN1"


@pytest.fixture
def _no_machine_secrets(monkeypatch):
    """Keep the test machine's real config/env out of bug report classification."""
    monkeypatch.setattr(tbrowse.breport, "collect_known_secrets", lambda _p: ([], True))


def _bug_flow(monkeypatch, workspace, answers):
    return _browse(monkeypatch, [("pick", "session"), ("pick", "#1"), ("pick", "Report a bug"), *answers], workspace)


def _single_report(state):
    from raven.trajectory import bugreport as breport

    ((record_dir, record),) = breport.list_reports(state)
    return record_dir, record


def test_bug_report_happy_path(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    fake = _bug_flow(
        monkeypatch,
        workspace,
        ["the agent replied in the wrong language", False, True, _CANCEL],
    )

    out = capsys.readouterr().out
    _record_dir, record = _single_report(state)
    assert record["status"] == "local_ready"
    assert record["problem"]["description"] == "the agent replied in the wrong language"
    assert f"Bug report {record['report_id']} ready (local_ready)" in out
    assert out.count("Package:") == 1
    assert "Not uploaded — hand the package file to a developer yourself." in out
    assert "the attempt was pinned" in out
    assert "Nothing will be uploaded — the package stays on this machine." in out
    assert "residual scan: clean" in out
    assert "NOT anonymized" in out
    assert "not yet evaluated in this version" in out
    first_action_menu = next(titles for kind, msg, titles in fake.prompts if msg == "Action:")
    assert not any("Bug reports" in t for t in first_action_menu)


def test_bug_report_menu_counts_existing_reports(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    _bug_flow(monkeypatch, workspace, ["first problem", False, True, _CANCEL])
    capsys.readouterr()

    fake = _browse(monkeypatch, [("pick", "session"), ("pick", "#1"), _BACK, _BACK, _CANCEL], workspace)

    action_menu = next(titles for kind, msg, titles in fake.prompts if msg == "Action:")
    assert any("Bug reports (1)" in t for t in action_menu)
    assert any("Report a bug" in t for t in action_menu)


def test_bug_report_empty_description_reprompts(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    _bug_flow(monkeypatch, workspace, ["", "real description", False, True, _CANCEL])

    out = capsys.readouterr().out
    assert "A problem description is required to file a bug report." in out
    _record_dir, record = _single_report(state)
    assert record["problem"]["description"] == "real description"


def test_bug_report_optional_details_reach_summary_and_record(
    state, workspace, monkeypatch, capsys, _no_machine_secrets
):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    _bug_flow(
        monkeypatch,
        workspace,
        [
            "wrong language",
            True,
            "a reply in Chinese",
            "the reply was in English",
            ("pick", "medium"),
            "ask anything in Chinese",
            "forrest",
            True,
            _CANCEL,
        ],
    )

    out = capsys.readouterr().out
    assert "forrest (included in the package)" in out
    _record_dir, record = _single_report(state)
    assert record["problem"]["severity"] == "medium"
    assert record["reporter"] == "forrest"


def test_bug_report_cancel_keeps_nothing(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    _bug_flow(monkeypatch, workspace, [_BACK, _CANCEL])

    out = capsys.readouterr().out
    assert "Cancelled — no bug report was created." in out
    assert breport.list_reports(state) == []
    staging = breport.bugreports_root(state) / breport.STAGING_DIR
    assert not any(staging.iterdir())


def test_bug_report_declined_confirmation_keeps_nothing(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    _bug_flow(monkeypatch, workspace, ["it broke", False, False, _CANCEL])

    assert "Cancelled — no bug report was created." in capsys.readouterr().out
    assert breport.list_reports(state) == []


def test_bug_report_needs_review_requires_second_confirm(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a", attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})],
    )

    fake = _bug_flow(monkeypatch, workspace, ["it broke", False, True, False, _CANCEL])

    out = capsys.readouterr().out
    assert "NEEDS REVIEW" in out
    assert "residual scan flagged" in out
    assert "Cancelled — no bug report was created." in out
    assert breport.list_reports(state) == []
    ship_prompt = next(msg for kind, msg, _t in fake.prompts if "Ship the package anyway?" in msg)
    assert "flagged content may include real secrets" in ship_prompt


def test_bug_report_needs_review_accepted_ships(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    import tarfile as _tarfile

    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a", attrs={"llm.output": f"token {_ENTROPY_TOKEN}"})],
    )

    _bug_flow(monkeypatch, workspace, ["it broke", False, True, True, _CANCEL])

    _record_dir, record = _single_report(state)
    assert record["status"] == "local_ready"
    assert record["redaction"]["classification"] == "needs_review"
    with _tarfile.open(record["package"]["path"]) as tar:
        meta = json.loads(tar.extractfile(f"{record['report_id']}/bugreport.json").read().decode("utf-8"))
    assert meta["redaction"]["risk_accepted"] is True


def test_bug_report_blocked_trajectory_stops_before_input(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a", attrs={"llm.output": _PEM})],
    )

    fake = _bug_flow(monkeypatch, workspace, [_CANCEL])

    out = capsys.readouterr().out
    assert "Cannot create a bug report from this attempt." in out
    assert "private key block" in out
    assert "raven trajectory report" in out
    assert breport.list_reports(state) == []
    assert not any(kind == "text" for kind, _m, _t in fake.prompts)


def test_bug_report_blocked_description_stops_before_confirm(
    state, workspace, monkeypatch, capsys, _no_machine_secrets
):
    from raven.trajectory import bugreport as breport

    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    fake = _bug_flow(monkeypatch, workspace, [f"look: {_PEM}", False, _CANCEL])

    out = capsys.readouterr().out
    assert "Cannot create a bug report with these details." in out
    assert "without pasting the key itself" in out
    assert breport.list_reports(state) == []
    assert not any("Create the bug report?" in msg for _k, msg, _t in fake.prompts)


def test_bug_report_concurrent_member_change_rejected(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    from raven.trajectory import bugreport as breport

    _write_log(
        state / "logs" / "audit-spans.log",
        [_span("trace-1", session_key="cli:a"), _span("trace-2", session_key="cli:a")],
    )

    def _merge_then_confirm():
        tstore.merge_attempts(["trace-1", "trace-2"], state_dir=state)
        return True

    _bug_flow(monkeypatch, workspace, ["it broke", False, _merge_then_confirm, _BACK, _CANCEL])

    out = capsys.readouterr().out
    assert "The attempt changed while the report was being prepared — nothing was created." in out
    assert breport.list_reports(state) == []


def test_bug_report_retry_from_reports_menu(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    import os as _os

    from raven.trajectory import bugreport as breport

    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])
    prep = breport.prepare_trajectory("trace-1", workspace=workspace, state_dir=state)
    prep = breport.freeze_export(prep, description="tool call crashed the session")
    breport.save_record(prep.staging_dir, breport._new_record_payload(prep))
    record_dir = breport.bugreports_root(state) / prep.report_id
    _os.replace(prep.staging_dir, record_dir)

    _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("pick", "#1"),
            ("pick", "Bug reports (1)"),
            ("pick", "br-"),
            ("pick", "Retry packaging"),
            _BACK,
            _BACK,
            _CANCEL,
        ],
        workspace,
    )

    out = capsys.readouterr().out
    record = breport.load_record(record_dir)
    assert record["status"] == "local_ready"
    assert f"Bug report {record['report_id']} ready (local_ready)" in out
    assert "interrupted before the package was written" in out


def test_bug_report_outputs_confine_ids_to_paths(state, workspace, monkeypatch, capsys, _no_machine_secrets):
    _write_log(state / "logs" / "audit-spans.log", [_span("trace-1", session_key="cli:a")])

    _bug_flow(monkeypatch, workspace, ["it broke", False, True, _CANCEL])

    out = capsys.readouterr().out
    for line in out.splitlines():
        if state.name in line or "Package:" in line:
            continue
        _assert_no_ids(line)
