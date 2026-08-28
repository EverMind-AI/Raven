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
    - ``_CANCEL``           — ``ask()`` returns None (Ctrl+C / EOF);
    - a zero-arg callable   — invoked at answer time (side effects), its return used;
    - anything else         — returned verbatim.
    Every prompt is recorded as (kind, message, [choice titles]).
    """

    class Choice:
        def __init__(self, title, value=None):
            self.title = title
            self.value = value if value is not None else title

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[tuple[str, str, list[str]]] = []
        self.calls: list[tuple[str, str, dict]] = []

    def _resolve(self, kind, message, choices, kwargs=None):
        titles = [c.title for c in choices] if choices else []
        self.prompts.append((kind, message, titles))
        self.calls.append((kind, message, kwargs or {}))
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer()
        if answer is _CANCEL:
            value = None
        elif isinstance(answer, tuple) and answer and answer[0] == "pick":
            value = next(c.value for c in choices if answer[1] in c.title)
        elif isinstance(answer, tuple) and answer and answer[0] == "pickall":
            value = [next(c.value for c in choices if s in c.title) for s in answer[1]]
        else:
            value = answer
        return type("_Ask", (), {"ask": staticmethod(lambda: value)})()

    def select(self, message, choices=None, **kw):
        return self._resolve("select", message, choices, kw)

    def checkbox(self, message, choices=None, **kw):
        return self._resolve("checkbox", message, choices, kw)

    def confirm(self, message, **kw):
        return self._resolve("confirm", message, None, kw)

    def text(self, message, **kw):
        return self._resolve("text", message, None, kw)


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
    attribute still yields a human label (normalized to one line)."""
    span = _span("trace-1", session_key="weirdkey", attrs={"channel": "discord\nextra"})
    _write_log(state / "logs" / "audit-spans.log", [span])

    (srow,) = tbrowse.scan_sessions(workspace)

    assert srow.title == "discord extra session · 2026-08-20 10:00"
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
        [("pick", "session"), ("pick", "#1"), ("pick", "Save"), ("pick", "Back"), ("pick", "Exit")],
        workspace,
    )

    assert (state / "bundles" / aid / "manifest.json").exists()
    out = capsys.readouterr().out
    assert str(state / "bundles") in out.replace("\n", "")


def test_minimize_flow_and_repeat(state, workspace, monkeypatch):
    _two_turn_log(state)
    script = [("pick", "session"), ("pick", "#1"), ("pick", "Minimize"), ("pick", "Back"), ("pick", "Exit")]
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
            ("pick", "Back"),
            ("pick", "Exit"),
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
            ("pick", "Merge attempts"),
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
            ("pick", "Back"),
            ("pick", "Exit"),
        ],
        workspace,
    )

    selects = [(m, titles) for kind, m, titles in fake.prompts if kind == "select"]
    attempt_screens = [titles for m, titles in selects if m == "Attempt:"]
    # after merge: one merged row, no more merge entry
    assert sum("merged" in t for t in attempt_screens[1]) == 1
    assert not any("Merge attempts" in t for t in attempt_screens[1])
    # after verdict: the row shows it
    assert any("fail" in t for t in attempt_screens[2])
    # after save: the action menu offers Unpin (auto-pin happened)
    action_screens = [titles for m, titles in selects if m == "Action:"]
    assert any("Unpin" in t for t in action_screens[2])
    # after split: two rows again, none merged
    assert sum(t.startswith("#") for t in attempt_screens[4]) == 2
    assert not any("merged" in t for t in attempt_screens[4])

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
            ("pick", "Report"),
            False,
            ("pick", "#1"),
            ("pick", "Back"),
            ("pick", "Back"),
            ("pick", "Exit"),
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
            ("pick", "Back"),
            ("pick", "Back"),
            ("pick", "Exit"),
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
            ("pick", "Merge attempts"),
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
            ("pick", "Back"),
            ("pick", "Exit"),
        ],
        workspace,
    )

    _assert_no_ids(capsys.readouterr().out)


@pytest.mark.parametrize(
    "script, patch_target",
    [
        ([("pick", "session"), ("pick", "#1"), ("pick", "Save"), ("pick", "Back"), ("pick", "Exit")], "collect_bundle"),
        (
            [("pick", "session"), ("pick", "#1"), ("pick", "Report"), ("pick", "Back"), ("pick", "Exit")],
            "collect_bundle",
        ),
        (
            [("pick", "session"), ("pick", "#1"), ("pick", "Minimize"), ("pick", "Back"), ("pick", "Exit")],
            "collect_bundle",
        ),
        (
            [
                ("pick", "session"),
                ("pick", "Merge attempts"),
                ("pickall", ["#1", "#2"]),
                ("pick", "Back"),
                ("pick", "Exit"),
            ],
            "merge_attempts",
        ),
        (
            [("pick", "session"), ("pick", "#1"), ("pick", "Pin"), "keep", ("pick", "Back"), ("pick", "Exit")],
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
        [("pick", "session"), ("pick", "#1"), ("pick", "Split"), True, ("pick", "Back"), ("pick", "Exit")],
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
            ("pick", "Report"),
            True,
            ("pick", "Back"),
            ("pick", "Exit"),
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
            ("pick", "Back"),
            ("pick", "Exit"),
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
            ("pick", "Back"),
            ("pick", "Exit"),
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
        [("pick", "session"), ("pick", "#1"), ("pick", "Report"), _CANCEL],
        [("pick", "session"), ("pick", "Merge attempts"), _CANCEL],
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


def test_selects_share_prompt_chrome(state, workspace, monkeypatch):
    """Every select/checkbox passes the shared QMARK and POINTER so the
    browser's prompt chrome matches the rest of the CLI."""
    from raven.cli._theme import POINTER, QMARK

    _two_turn_log(state)

    fake = _browse(
        monkeypatch,
        [
            ("pick", "session"),
            ("pick", "Merge attempts"),
            ("pickall", ["#1", "#2"]),
            ("pick", "#1"),
            ("pick", "Verdict"),
            "pass",
            "",
            "",
            ("pick", "Back"),
            ("pick", "Exit"),
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
            ("pick", "Merge attempts"),
            ("pickall", ["#1", "#2"]),
            ("pick", "Back"),
            ("pick", "Exit"),
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
        [("pick", "session"), ("pick", "#1"), ("pick", "Pin"), "keep", ("pick", "Back"), ("pick", "Exit")],
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
            ("pick", "attempt(s)"),
            ("pick", "#1"),
            ("pick", "Save"),
            ("pick", "#1"),
            ("pick", "Minimize"),
            ("pick", "Back"),
            ("pick", "Exit"),
        ],
        other,
    )

    manifest = json.loads((state / "bundles" / aid / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_included"] is True
    assert (state / "bundles" / aid / "session.jsonl").exists()
    assert (state / "cassettes" / aid / "manifest.json").exists()
