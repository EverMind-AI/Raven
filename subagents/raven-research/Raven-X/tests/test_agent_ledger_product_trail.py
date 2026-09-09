"""dr@2.9: the product-side ledger, i.e. the reason the appendix could ever render.

``final_shape.process_appendix`` has defaulted on since dr@2.8 because the default is
the product, but the trail it renders is computed from the client-side ledger and the
only writer of ``RAVEN_WEB_LEDGER`` in the tree is the batch launcher. So the single
configuration that asked for an appendix was the single configuration guaranteed not to
get one, and it failed silently: ``build_appendix`` returned ``ledger_not_configured``,
the answer came back intact, and nothing logged. Every test here pins one of the
properties that made that defect possible or that the fix must not break.

Half of these assert the *opposite* direction on purpose. A fallback of this shape has
two ways to be worse than the bug: it can take over a measured arm's ledger, and it can
be added to the reader alone so the appendix reads a file nothing ever wrote.
"""

from __future__ import annotations

import asyncio
import json
import os

import raven.agent.ledger as ledger_mod
from raven.agent.ledger import (
    LEDGER_ENV,
    close_product_ledger,
    ledger_append,
    ledger_path,
    open_product_ledger,
)


def _isolate(monkeypatch, tmp_path):
    """No env ledger, and the instance data dir points into tmp."""
    monkeypatch.delenv(LEDGER_ENV, raising=False)
    monkeypatch.setattr(
        "raven.config.paths.get_product_ledger_dir", lambda: tmp_path, raising=True
    )


def test_product_default_gives_the_appendix_a_ledger_to_read(monkeypatch, tmp_path):
    """The defect itself: with nothing configured there was nowhere to write."""
    _isolate(monkeypatch, tmp_path)
    try:
        path = open_product_ledger("turn-1")

        assert path is not None
        assert ledger_path() == path
        ledger_append({"op": "search", "query": "q", "replay": False, "zero_hit": False})
        assert json.loads(open(path, encoding="utf-8").read().strip())["query"] == "q"
    finally:
        close_product_ledger()


def test_the_product_path_now_renders_a_real_appendix(monkeypatch, tmp_path):
    """End to end, and stated as before/after because that is the whole claim.

    Before: ``os.environ`` was the only resolution, so the product configuration got
    ``("", {"emitted": False, "reason": "ledger_not_configured"})`` on every turn.
    """
    from raven.agent.process_appendix import build_appendix

    _isolate(monkeypatch, tmp_path)
    try:
        path = open_product_ledger("turn-e2e")
        ledger_append({"op": "search", "query": "arwa charter", "replay": False, "zero_hit": False})
        ledger_append({"op": "fetch", "url": "https://a.example/one", "chars": 900, "ok": True})

        appendix, trail = build_appendix(ledger_path(), "see https://a.example/one")

        assert trail["emitted"] is True
        assert appendix, "the trail rendered empty on the one path that asks for it"
        assert "https://a.example/one" in appendix

        # The old resolution, on the same turn: this is what the defect returned.
        was, was_trail = build_appendix(os.environ.get(LEDGER_ENV), "see https://a.example/one")
        assert (was, was_trail["reason"]) == ("", "ledger_not_configured")
        assert path is not None
    finally:
        close_product_ledger()


def test_the_writer_and_the_reader_resolve_through_one_function(monkeypatch, tmp_path):
    """Regression guard on the shape of the bug, not just the bug.

    ``ledger_append`` used to read ``os.environ`` directly, so "where do I write?" and
    "where do I read?" were two pieces of code that merely happened to agree. Adding the
    product fallback to ``ledger_path`` alone would have made them disagree and produced
    the same empty appendix one layer deeper - a file nothing ever wrote.
    """
    _isolate(monkeypatch, tmp_path)
    try:
        path = open_product_ledger("turn-2")
        ledger_append({"op": "fetch", "url": "https://a.example/x", "chars": 9, "ok": True})

        assert os.path.exists(path), "ledger_append resolved somewhere ledger_path does not"
    finally:
        close_product_ledger()


def test_an_env_ledger_always_wins_and_is_never_deleted(monkeypatch, tmp_path):
    """Opposite direction: a batch arm must be byte-identical to before this change."""
    arm = tmp_path / "arm_dr_q17.jsonl"
    arm.write_text('{"op": "search", "query": "measured"}\n', encoding="utf-8")
    monkeypatch.setenv(LEDGER_ENV, str(arm))
    monkeypatch.setattr(
        "raven.config.paths.get_product_ledger_dir", lambda: tmp_path / "never", raising=True
    )

    assert open_product_ledger("turn-3") is None, "took over a launcher-owned ledger"
    assert ledger_path() == str(arm)

    ledger_append({"op": "fetch", "url": "https://b.example/y", "chars": 1, "ok": True})
    close_product_ledger()

    assert arm.exists(), "deleted measurement data"
    assert len(arm.read_text(encoding="utf-8").strip().split("\n")) == 2
    assert not (tmp_path / "never").exists(), "created a product dir on a measured arm"


def test_nothing_is_configured_until_someone_opens_it(monkeypatch, tmp_path):
    """Opposite direction: importing the module must not turn the ledger on.

    The ledger is off by default in every process that does not ask for it - that is
    what keeps ordinary use and every already-measured arm unchanged.
    """
    _isolate(monkeypatch, tmp_path)

    assert ledger_path() is None
    ledger_append({"op": "search", "query": "dropped"})

    assert list(tmp_path.iterdir()) == []


def test_the_file_is_released_after_the_appendix_has_read_it(monkeypatch, tmp_path):
    """One file per turn with one reader; keeping them would accumulate forever."""
    _isolate(monkeypatch, tmp_path)
    path = open_product_ledger("turn-4")
    ledger_append({"op": "search", "query": "q"})
    assert os.path.exists(path)

    close_product_ledger()

    assert not os.path.exists(path)
    assert ledger_path() is None, "a released ledger must not stay resolvable"


def test_close_is_idempotent_and_survives_a_missing_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    path = open_product_ledger("turn-5")
    os.unlink(path) if os.path.exists(path) else None

    close_product_ledger()
    close_product_ledger()


def test_an_unwritable_directory_disables_the_trail_without_raising(monkeypatch, tmp_path):
    """An appendix is a nicety; the answer is not. Degrade, never raise."""
    monkeypatch.delenv(LEDGER_ENV, raising=False)

    def boom():
        raise OSError("read-only file system")

    monkeypatch.setattr("raven.config.paths.get_product_ledger_dir", boom, raising=True)

    assert open_product_ledger("turn-6") is None
    assert ledger_path() is None
    ledger_append({"op": "search", "query": "q"})


def test_the_ledger_is_opened_before_the_turn_does_any_work():
    """Source-level, because the failure it guards against has no output symptom.

    The trail can only contain rows written while the ledger was open. Move this call
    below the iteration loop - next to the appendix render, where it reads more
    naturally - and every search and fetch of the turn happens before there is anywhere
    to write, so ``build_appendix`` reports ``ledger_empty`` and the answer comes back
    without a trail. That is the original defect with a different reason string.
    """
    import inspect

    from raven.agent.loop import main

    src = inspect.getsource(main.AgentLoop._run_agent_loop)

    assert src.index("open_product_ledger") < src.index("while "), (
        "the turn-scoped ledger must be opened before the iteration loop"
    )
    assert src.index("open_product_ledger") < src.index("appendix, trail = build_appendix")


def test_two_concurrent_turns_do_not_share_one_file(monkeypatch, tmp_path):
    """The reason this is a ContextVar and not a module global.

    Bench runs one question per process, where a global is correct. A channel process
    serves several sessions at once, each turn its own task, and a global would let the
    turn that started second redirect the first one's writes - producing an appendix
    that credits this answer with another conversation's pages.
    """
    _isolate(monkeypatch, tmp_path)

    async def turn(name: str, hold: float) -> str:
        path = open_product_ledger(name)
        # Interleave: A opens, yields; B opens, writes, closes; only then does A write.
        # With a module global, A's write would land in B's file (or nowhere, after B's
        # close). The sleeps are what make the ordering deterministic.
        await asyncio.sleep(hold)
        ledger_append({"op": "search", "query": name})
        contents = open(path, encoding="utf-8").read()
        close_product_ledger()
        return f"{path}\n{contents}"

    async def both():
        return await asyncio.gather(turn("a", 0.02), turn("b", 0.0))

    (path_a, row_a), (path_b, row_b) = (r.split("\n", 1) for r in asyncio.run(both()))

    assert path_a != path_b, "two concurrent turns shared one ledger file"
    assert json.loads(row_a)["query"] == "a", "turn a's write landed in the other file"
    assert json.loads(row_b)["query"] == "b"
    assert ledger_mod.ledger_path() is None, "a turn leaked its ledger to the caller"


# --------------------------------------------------------------------------- #
# session_seq: the ledger file is keyed by workspace, so a re-run appends       #
# --------------------------------------------------------------------------- #


def test_a_fresh_ledger_starts_at_session_one(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(path))
    ledger_mod._session_seq.clear()

    ledger_append({"op": "search", "query": "q"})

    assert json.loads(path.read_text(encoding="utf-8").strip())["session_seq"] == 1


def test_a_rerun_of_the_same_question_gets_the_next_session(monkeypatch, tmp_path):
    """The defect this field exists for.

    ``extbench_20260811/run_dr29/browsecomp-781``: one ledger file, two complete
    sessions 398s apart, 217 rows then 67 - and ``traj_raw`` keeps only one row per
    question, so the second session's 67 rows are invisible there. Reading the file
    whole over-counts that question by 31%.
    """
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(path))

    ledger_mod._session_seq.clear()          # first process
    ledger_append({"op": "search", "query": "first-run"})
    ledger_mod._session_seq.clear()          # a second process opens the same file
    ledger_append({"op": "search", "query": "second-run"})

    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [r["session_seq"] for r in rows] == [1, 2]
    # And the partition is usable: exactly the rows of the run that was scored.
    assert [r["query"] for r in rows if r["session_seq"] == 2] == ["second-run"]


def test_rows_written_before_the_field_existed_stay_distinguishable(monkeypatch, tmp_path):
    """Absent must not collapse into 1, or a landed file's first re-run becomes
    indistinguishable from its original session."""
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "led.jsonl"
    path.write_text(json.dumps({"op": "search", "query": "old"}) + "\n", encoding="utf-8")
    monkeypatch.setenv(LEDGER_ENV, str(path))
    ledger_mod._session_seq.clear()

    ledger_append({"op": "search", "query": "new"})

    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert "session_seq" not in rows[0]
    assert rows[1]["session_seq"] == 1


def test_the_sequence_is_resolved_once_not_per_row(monkeypatch, tmp_path):
    """It reads the file tail; doing that per append would be O(n^2) on a ledger
    that reaches tens of thousands of rows."""
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(path))
    ledger_mod._session_seq.clear()

    calls = []
    real = ledger_mod._resolve_session_seq
    monkeypatch.setattr(ledger_mod, "_resolve_session_seq",
                        lambda p: (calls.append(p), real(p))[1])
    for i in range(5):
        ledger_append({"op": "search", "query": f"q{i}"})

    assert len(calls) == 1
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert {r["session_seq"] for r in rows} == {1}


def test_a_caller_supplied_session_seq_is_not_overwritten(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    path = tmp_path / "led.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(path))
    ledger_mod._session_seq.clear()

    ledger_append({"op": "search", "session_seq": 99})

    assert json.loads(path.read_text(encoding="utf-8").strip())["session_seq"] == 99
