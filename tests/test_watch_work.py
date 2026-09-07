"""The main agent recognises run-and-watch work and steers it to the on-call agent.

The failure these pin, measured twice: a request naming a solver on this very
computer, a budget, and a shared machine was read correctly and iteration 1
still went straight to a local shell -- with the roster entry saying, in as many
words, that local work counts (2026-08-27). The one channel measured to change
the next move is a line arriving in a tool result, so that is the only place
these tests look for it.

The judgement's own failure modes have to stay silent: it sits in front of every
path-touching tool call, and a parse error there would break looking at files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.subagent import watch_work

# --- read_verdict: anything unreadable is "not watched", never an exception ---


def test_a_clean_yes_carries_its_paths():
    v = watch_work.read_verdict('{"watched": true, "paths": ["/tmp/arena"]}')
    assert v.watched and v.subjects == ["/tmp/arena"]


def test_prose_around_the_json_is_tolerated():
    v = watch_work.read_verdict('Sure!\n```{"watched": true, "paths": []}```')
    assert v.watched


@pytest.mark.parametrize("reply", ["", None, "not json", '["list"]', '{"watched": "unsure"}'])
def test_everything_unreadable_or_unsure_is_a_quiet_no(reply):
    assert watch_work.read_verdict(reply).watched is False


def test_claims_covers_the_path_and_what_sits_under_it():
    v = watch_work.Verdict(watched=True, subjects=["/tmp/arena"])
    assert v.claims("/tmp/arena")
    assert v.claims("/tmp/arena/runs/t1/config.json")
    assert not v.claims("/tmp/elsewhere")
    assert not v.claims("")


def test_a_no_claims_nothing_even_with_paths():
    assert watch_work.Verdict(watched=False, subjects=["/tmp/arena"]).claims("/tmp/arena") is False


def test_a_legacy_paths_reply_still_reads():
    v = watch_work.read_verdict('{"watched": true, "paths": ["/tmp/arena"]}')
    assert v.watched and v.subjects == ["/tmp/arena"]


def test_a_url_subject_claims_the_cli_call_that_reaches_it():
    """The measured shape (2026-08-28): the owner names a pipeline by web URL,
    and every look arrives as a glab call carrying the project slug
    percent-encoded. Neither side contains the other verbatim."""
    v = watch_work.Verdict(watched=True, subjects=["https://gitlab.com/npc-work/aic/ai/raven/-/pipelines/2798916676"])
    assert v.claims("glab api projects/npc-work%2Faic%2Fai%2Fraven/pipelines/2798916676 2>&1")
    assert v.claims("https://gitlab.com/npc-work/aic/ai/raven/-/pipelines/2798916676")
    assert not v.claims("glab api projects/other%2Fproject/pipelines/999")
    assert not v.claims("ls /tmp")


def test_a_relative_path_keeps_the_containment_it_always_had():
    """The review's counterexample: a leading-slash dispatch lost this claim,
    while the prior unconditional Path containment granted it."""
    v = watch_work.Verdict(watched=True, subjects=["run"])
    assert v.claims("run/job.sh")
    assert v.claims("run")
    assert not v.claims("elsewhere/job.sh")
    assert not v.claims("run-old/job.sh"), "a sibling with a longer name is not under it"


def test_a_short_handle_is_still_claimable():
    v = watch_work.Verdict(watched=True, subjects=["@bob"])
    assert v.claims("glab api /users/@bob/events")
    assert not v.claims("glab api /users/@alice/events")


def test_a_url_subject_never_claims_a_mere_path_and_vice_versa():
    url = watch_work.Verdict(watched=True, subjects=["https://gitlab.com/g/p/-/pipelines/12345678"])
    assert not url.claims("/tmp/arena/runs/t1")
    path = watch_work.Verdict(watched=True, subjects=["/tmp/arena"])
    assert not path.claims("glab api projects/g%2Fp/pipelines/12345678")


# --- asked_for: the judgement reads the owner's words, not the runtime glue ---


def test_asked_for_takes_the_last_user_message_and_strips_the_metadata_block():
    messages = [
        {"role": "system", "content": "be raven"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "[Runtime Context]\nnow=12:00\n\nrun a case, 25 min budget"},
    ]
    assert watch_work.asked_for(messages) == "run a case, 25 min budget"


def test_asked_for_handles_block_content_and_empty_history():
    messages = [{"role": "user", "content": [{"type": "text", "text": "watch this"}]}]
    assert watch_work.asked_for(messages) == "watch this"
    assert watch_work.asked_for([]) == ""


# --- the declaration: settled at admission, carried on the table ---


def _admitted(tmp_path, entry: dict):
    """One folder manifest through the real admission path, to a table row.

    ``discover_product_rows`` for the half that reads the folder, then
    ``_row_for`` for the half the registry dispenses -- neither reimplemented
    here, because a stand-in for either would keep passing on the day the real
    one stops carrying the declaration, which is the failure being pinned.
    """
    from raven.agent.subagent.registry import _row_for
    from raven.agent.subagent.vendored_agents import discover_product_rows

    folder = tmp_path / "raven-oncall"
    folder.mkdir()
    (folder / "subagent.json").write_text(json.dumps(entry), encoding="utf-8")
    (folder / "install.py").write_text("", encoding="utf-8")
    (cfg,) = discover_product_rows(root=tmp_path)
    return _row_for(cfg)


@pytest.mark.parametrize("kind", ["cli", "acp"])
@pytest.mark.parametrize("spelling", ["ownsWatchedWork", "owns_watched_work", "runsOnMachines", "runs_on_machines"])
def test_every_spelling_the_schema_ever_accepted_reaches_the_table(kind, spelling, tmp_path):
    """Both legacy spellings are what a row or a folder written before the
    rename carries, and ``Base`` sets ``populate_by_name``, so both were valid.
    A declaration admitted here has to arrive on the dispensed row: the reader
    downstream has no other source, so a spelling dropped at admission is a
    steering that goes quiet on upgrade with nothing said."""
    row = _admitted(tmp_path, {"name": "Raven-Oncall", "kind": kind, "command": "run", spelling: True})

    assert row.owns_watched_work is True
    assert row.meta().owns_watched_work is True


@pytest.mark.parametrize("kind", ["cli", "acp"])
def test_a_manifest_without_the_declaration_reaches_the_table_without_it(kind, tmp_path):
    row = _admitted(tmp_path, {"name": "Raven-Code", "kind": kind, "command": "run"})

    assert row.owns_watched_work is False
    assert row.meta().owns_watched_work is False


# --- oncall_agent: which row on this roster the request belongs with ---


def _meta(name: str, *, owns_watched_work: bool):
    from raven.agent.subagent.backends import AgentMeta

    return AgentMeta(name, "", False, False, owns_watched_work=owns_watched_work)


def test_the_row_that_declares_watched_work_is_the_one_named():
    roster = [_meta("Raven-Code", owns_watched_work=False), _meta("Raven-Oncall", owns_watched_work=True)]

    assert watch_work.oncall_agent(roster) == "Raven-Oncall"


def test_a_roster_nobody_flags_names_nobody():
    assert watch_work.oncall_agent([_meta("Raven-Code", owns_watched_work=False)]) is None


def test_a_row_that_cannot_answer_the_question_names_nobody():
    """Duck-typed rows reach this from the DAG runner and from test doubles; one
    that never heard of the declaration is not the specialist."""
    from types import SimpleNamespace

    assert watch_work.oncall_agent([SimpleNamespace(name="Raven-Code")]) is None


# --- the nudge itself ---


def test_the_nudge_names_the_spawn_and_both_shapes():
    line = watch_work.nudge("Raven-Oncall")
    assert "spawn `Raven-Oncall`" in line
    assert "RUN" in line and "WATCH" in line, (
        "measured 2026-08-21: steered with run-only words, a watch task built its "
        "own monitor out of write_file and cron"
    )
    assert "before running anything by hand" in line


# --- the loop hook, driven through a minimal AgentLoop stand-in ---


class _Response:
    def __init__(self, content):
        self.content = content


class _SpawnStub:
    """Real ``AgentMeta`` rows, because the hook reads the declaration off them."""

    def __init__(self, names):
        self._names = names

    def _agents(self):
        return [_meta(n, owns_watched_work=n == "Raven-Oncall") for n in self._names]


class _Tools:
    def __init__(self, names):
        self._spawn = _SpawnStub(names) if names is not None else None

    def get(self, name):
        return self._spawn if name == "spawn" else None


def _loop(names=("Raven-Oncall",), verdict='{"watched": true, "paths": ["/tmp/arena"]}'):
    """An AgentLoop with only what _note_watch_work reads, plus a turn's state.

    Returns the loop and one turn's TurnWatch: the state lives with the turn,
    not the loop, which is the whole point pinned by the interleaving test.
    """
    from types import SimpleNamespace

    from raven.agent.loop.main import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.tools = _Tools(list(names) if names is not None else None)
    # `model` is a property over the active binding; give the fallback leg one.
    loop._default_binding = SimpleNamespace(model="test-model")
    loop._llm_calls = 0

    async def fake_llm(messages, tools, model, **kwargs):
        loop._llm_calls += 1
        return _Response(verdict)

    loop._llm_call_stream = fake_llm
    return loop, watch_work.TurnWatch()


ACCEPTED = "Subagent [x] started (id: abc123). I'll notify you when it completes."


@pytest.mark.asyncio
async def test_a_look_at_the_named_path_gains_the_line():
    loop, state = _loop()
    out = await loop._note_watch_work(
        state, "read_file", {"path": "/tmp/arena/run.sh"}, "file contents", "run /tmp/arena, 25 min budget"
    )
    assert "spawn `Raven-Oncall`" in out
    assert "file contents" not in out, (
        "the note travels alone: the result is fenced as untrusted data by "
        "add_tool_result, and a note appended to it would ride inside the fence"
    )


@pytest.mark.asyncio
async def test_exec_commands_are_searched_for_the_path():
    loop, state = _loop()
    out = await loop._note_watch_work(
        state, "exec", {"command": "cd /tmp/arena/runs && bash run.sh"}, "ok", "run /tmp/arena"
    )
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
async def test_the_judgement_is_paid_once_per_turn():
    loop, state = _loop()
    for _ in range(3):
        await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert loop._llm_calls == 1


@pytest.mark.asyncio
async def test_a_not_watched_verdict_leaves_every_result_alone():
    loop, state = _loop(verdict='{"watched": false, "paths": []}')
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "read this file for me")
    assert out == ""


@pytest.mark.asyncio
async def test_a_look_elsewhere_stays_clean_even_on_a_watched_turn():
    loop, state = _loop()
    out = await loop._note_watch_work(state, "read_file", {"path": "/etc/hosts"}, "r", "run /tmp/arena")
    assert out == ""


@pytest.mark.asyncio
async def test_after_a_real_oncall_dispatch_the_turn_goes_quiet():
    """The judgement is paid at most once per turn. A straight-to-spawn turn
    pays it AT the hand-off, to judge the dispatch's shape (2026-09-01: a bare
    watcher spawn on a code-driven request dropped the coding half and every
    nudge then went silent) -- one flash call against hours of misdirected
    budget. Looks after the hand-off still pay nothing."""
    loop, state = _loop()
    await loop._note_watch_work(state, "spawn", {"subagent": "Raven-Oncall"}, ACCEPTED, "run /tmp/arena")
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert out == ""
    assert loop._llm_calls == 1, "the judgement is paid once, at the hand-off, and never again"


@pytest.mark.asyncio
async def test_an_unrelated_spawn_silences_nothing():
    """Reproduced in review: a spawn of another agent read as a hand-off and
    suppressed the next matching path nudge."""
    loop, state = _loop(names=("Raven-Code", "Raven-Oncall"))
    await loop._note_watch_work(
        state, "spawn", {"subagent": "Raven-Code"}, ACCEPTED.replace("x", "code job"), "run /tmp/arena"
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        "Spawn refused: delegation is paused",
        "Error: prompt_template names a file this sub-agent cannot be given",
    ],
)
async def test_a_refused_dispatch_of_any_shape_silences_nothing(refusal):
    loop, state = _loop()
    await loop._note_watch_work(state, "spawn", {"subagent": "Raven-Oncall"}, refusal, "run /tmp/arena")
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
async def test_two_interleaved_turns_never_share_watch_state():
    """run_turn lets turns from other sessions run concurrently on the one
    loop object: B's dispatch must not silence A, and A's verdict must not
    answer for B."""
    loop, state_a = _loop()
    state_b = watch_work.TurnWatch()

    await loop._note_watch_work(state_b, "spawn", {"subagent": "Raven-Oncall"}, ACCEPTED, "run /tmp/arena")
    out_a = await loop._note_watch_work(state_a, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")

    assert "spawn `Raven-Oncall`" in out_a, "B's hand-off silenced A"
    assert state_b.dispatched and not state_a.dispatched
    assert state_a.verdict is not None and state_b.verdict is not None
    assert state_a.verdict is not state_b.verdict, "each turn holds its own verdict"


@pytest.mark.asyncio
async def test_a_roster_without_the_specialist_asks_no_judgement():
    loop, state = _loop(names=("Raven-Code",))
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert out == ""
    assert loop._llm_calls == 0


@pytest.mark.asyncio
async def test_a_judgement_that_blows_up_never_reaches_the_result(monkeypatch):
    loop, state = _loop()

    def broken(agents):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(watch_work, "oncall_agent", broken)
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert out == ""


# --- the spawn itself: nothing stands between the call and the manager ---


@pytest.mark.asyncio
async def test_a_spawn_reaches_the_manager(tmp_path, monkeypatch):
    """Including the specialist, with no machine set up anywhere. The host used
    to read the owner's registry here and refuse a spawn it found empty; where
    the work runs is now the sub-agent's own question, asked after it starts."""
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(tmp_path / "never-written.json"))
    from raven.agent.subagent.spawn_tool import SpawnTool

    class _Manager:
        def __init__(self, tmp):
            self.spawned = False
            self.workspace = str(tmp)
            self._tmp = tmp

        async def spawn(self, **kwargs):
            self.spawned = True
            return "spawned ok"

        def reference_roots(self, session_key):
            return []

        def session_dir_for(self, session_key):
            return self._tmp

    mgr = _Manager(tmp_path)
    tool = SpawnTool(manager=mgr)
    out = await tool.execute("watch a case", "run the case", subagent="Raven-Oncall")

    assert mgr.spawned and out == "spawned ok"


# --- a refused dispatch answers nothing: the pressure must survive it ---


@pytest.mark.asyncio
async def test_a_successful_dispatch_still_goes_quiet():
    loop, state = _loop()
    await loop._note_watch_work(state, "spawn", {"subagent": "Raven-Oncall"}, ACCEPTED, "run /tmp/arena")
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert out == ""


@pytest.mark.asyncio
async def test_a_declined_graph_silences_nothing():
    """Reproduced in review: the declined-graph message carries no error prefix,
    and the old DAG leg searched the serialized call, so naming the on-call
    agent in the graph was enough to read a non-dispatch as a hand-off."""
    loop, state = _loop()
    await loop._note_watch_work(
        state,
        "run_subagent_dag",
        {"nodes": [{"id": "watch", "subagent": "Raven-Oncall"}]},
        "The user did not approve this graph, so nothing was run. Do not re-submit it; ask what to change.",
        "run /tmp/arena",
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
async def test_a_graph_that_merely_mentions_the_agent_silences_nothing():
    loop, state = _loop(names=("Raven-Code", "Raven-Oncall"))
    await loop._note_watch_work(
        state,
        "run_subagent_dag",
        {"nodes": [{"id": "build", "subagent": "Raven-Code", "prompt_template": "hand off to Raven-Oncall later"}]},
        "DAG run r1 started in the background (1 nodes).",
        "run /tmp/arena",
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
async def test_an_accepted_graph_with_an_oncall_node_goes_quiet():
    loop, state = _loop(names=("Raven-Code", "Raven-Oncall"))
    await loop._note_watch_work(
        state,
        "run_subagent_dag",
        {"nodes": [{"id": "build", "subagent": "Raven-Code"}, {"id": "watch", "subagent": "Raven-Oncall"}]},
        "DAG run r2 started in the background (2 nodes).",
        "run /tmp/arena",
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "run /tmp/arena")
    assert out == ""


# --- the hard form: the second claimed look is refused, not nudged again ---


def test_the_nudge_names_both_dispatch_doors():
    line = watch_work.nudge("Raven-Oncall")
    assert "spawn `Raven-Oncall`" in line and "run_subagent_dag" in line


def test_the_second_nudge_is_the_hard_form():
    first = watch_work.nudge("Raven-Oncall")
    second = watch_work.nudge("Raven-Oncall", repeat=True)
    assert not first.startswith("\n\nSTOP")
    assert second.startswith("\n\nSTOP")
    assert "do not ask the owner" in second


@pytest.mark.asyncio
async def test_the_loop_escalates_on_the_second_claimed_look():
    loop, state = _loop()
    first = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/a"}, "r", "run /tmp/arena")
    second = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/b"}, "r", "run /tmp/arena")
    assert "spawn `Raven-Oncall`" in first and not first.startswith("\n\nSTOP")
    assert second.startswith("\n\nSTOP")


# --- the owner-ask gate: a pre-dispatch question never reaches the owner ---


def _watched_state(subjects=("/srv/arena",)):
    state = watch_work.TurnWatch()
    state.verdict = watch_work.Verdict(watched=True, subjects=list(subjects))
    state.agent = "Raven-Oncall"
    return state


def test_a_pre_dispatch_ask_is_preempted_with_the_dispatch_door():
    state = _watched_state()
    out = watch_work.preempt_owner_ask(
        state, {"questions": [{"question": "Path /srv/arena does not exist on this machine, where is it?"}]}
    )
    assert "not sent to the owner" in out and "spawn `Raven-Oncall`" in out
    assert state.nudges == 1


def test_a_paraphrased_or_bundled_ask_is_still_preempted():
    """Measured 2026-08-28 run 6: the model wrote "this path" for the path and
    bundled two case-answerable questions alongside; a verbatim-subject filter
    let it through to the owner. The gate keys on the turn's judged state."""
    state = _watched_state()
    out = watch_work.preempt_owner_ask(
        state,
        {
            "questions": [
                {"question": "This path is on another machine. How do I reach that machine? SSH address?"},
                {"question": "What are the cantilever beam's geometry and material parameters?"},
            ]
        },
    )
    assert "not sent to the owner" in out and "spawn `Raven-Oncall`" in out


def test_no_verdict_or_handed_over_turn_never_intercepts():
    bare = watch_work.TurnWatch()
    assert watch_work.preempt_owner_ask(bare, {"questions": [{"question": "x /srv/arena"}]}) == ""
    done = _watched_state()
    done.dispatched = True
    assert watch_work.preempt_owner_ask(done, {"questions": [{"question": "x /srv/arena"}]}) == ""


# -- hoarded_code_note: bulk source in the main context earns one line --


def _source_blob(n: int = 200) -> str:
    return "\n".join(f"import os\ndef fn_{i}(x):\n    return x + {i}" for i in range(n))


def test_a_bulk_source_result_earns_the_hoarding_note():
    """The 2026-09-01 shape: whole files fetched into the main context."""
    note = watch_work.hoarded_code_note(_source_blob())
    assert "workspace of the node" in note
    assert "PATH" in note


def test_bulk_prose_is_not_code_and_earns_nothing():
    prose = (
        "The experiment concluded with a validation score that held steady "
        "across seeds, and the report describes each round in detail. "
    ) * 200
    assert watch_work.hoarded_code_note(prose) == ""


def test_a_small_snippet_earns_nothing():
    """Quoting a function while discussing it is normal conversation."""
    assert watch_work.hoarded_code_note("def f(x):\n    return x\n" * 20) == ""


# -- solo dispatch: a bare watcher spawn on a code-driven request --


CODE_VERDICT = '{"watched": true, "subjects": ["/tmp/arena"], "code_work": true}'
RUN_VERDICT = '{"watched": true, "subjects": ["/tmp/arena"], "code_work": false}'


def test_read_verdict_parses_code_work():
    assert watch_work.read_verdict(CODE_VERDICT).code_work is True
    assert watch_work.read_verdict(RUN_VERDICT).code_work is False
    assert watch_work.read_verdict('{"watched": true, "subjects": []}').code_work is False


@pytest.mark.asyncio
async def test_a_bare_watcher_spawn_on_code_work_earns_the_solo_note():
    """The 2026-09-01 shape: the whole search handed to the watcher as one
    spawn, the coding half owned by nobody, every nudge then silent."""
    loop, state = _loop(verdict=CODE_VERDICT)
    out = await loop._note_watch_work(
        state,
        "spawn",
        {"subagent": "Raven-Oncall", "task": "search"},
        ACCEPTED,
        "edit train.py for the search, run /tmp/arena",
    )
    assert "coding half" in out
    assert "run_subagent_dag" in out
    assert ACCEPTED not in out, (
        "the confirmation travels once, as the fenced tool result; repeating it in the trusted note "
        "was the duplication measured on every dispatch before this branch honoured the caller contract"
    )
    assert state.dispatched and state.solo


@pytest.mark.asyncio
async def test_a_watcher_spawn_on_run_only_work_is_a_complete_handoff():
    loop, state = _loop(verdict=RUN_VERDICT)
    out = await loop._note_watch_work(
        state,
        "spawn",
        {"subagent": "Raven-Oncall", "task": "run the case"},
        ACCEPTED,
        "run /tmp/arena and report the result",
    )
    assert "coding half" not in out
    assert state.dispatched and not state.solo


@pytest.mark.asyncio
async def test_a_later_claimed_look_repeats_the_solo_correction():
    loop, state = _loop(verdict=CODE_VERDICT)
    await loop._note_watch_work(
        state, "spawn", {"subagent": "Raven-Oncall", "task": "t"}, ACCEPTED, "edit train.py, run /tmp/arena"
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/train.py"}, "code", "msg")
    assert out.startswith("\n\nSTOP")
    assert "coding half" in out


@pytest.mark.asyncio
async def test_a_dag_dispatch_clears_the_solo_state():
    loop, state = _loop(verdict=CODE_VERDICT)
    await loop._note_watch_work(
        state, "spawn", {"subagent": "Raven-Oncall", "task": "t"}, ACCEPTED, "edit train.py, run /tmp/arena"
    )
    nodes = [{"subagent": "Raven-Code"}, {"subagent": "Raven-Oncall"}]
    await loop._note_watch_work(state, "run_subagent_dag", {"nodes": nodes}, "run r1 started in the background", "msg")
    assert not state.solo
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/train.py"}, "code", "msg")
    assert "coding half" not in out


# --- the note channel never carries tool output past the fence ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "result"),
    [
        ("spawn", {"subagent": "Raven-Oncall"}, ACCEPTED),
        ("run_subagent_dag", {"task_summary": "run and watch"}, "DAG run r1 accepted: node watch on Raven-Oncall"),
        ("spawn", {"subagent": "Raven-Oncall"}, "'Raven-Oncall' has no machine. Nothing was dispatched."),
    ],
)
async def test_a_dispatch_result_reaches_the_model_once_and_inside_the_fence(tmp_path, tool, args, result):
    """The whole road: ``_note_watch_work`` -> ``add_tool_result``.

    ``add_tool_result`` places the returned note AFTER the untrusted fence, as
    this system's own voice. Reviewed 2026-09-04: the spawn and DAG exits still
    returned ``result`` under the new contract, so a sub-agent's own text rode
    out of the fence as a trusted note -- a trust-boundary hole, not a display
    duplicate. Only system-composed text may travel that channel.
    """
    from raven.agent.context.builder import ContextBuilder

    loop, state = _loop()
    note = await loop._note_watch_work(state, tool, args, result, "run /tmp/arena")
    assert result not in note, "tool output must never be returned as the trusted note"

    messages = ContextBuilder(workspace=tmp_path).add_tool_result([], "c1", tool, result, trusted_note=note)
    content = messages[-1]["content"]
    assert content.count(result) == 1, "the result travels once, as the fenced tool result"
    fence_end = content.index("[END UNTRUSTED")
    assert result not in content[fence_end:], "and nothing of it appears after the fence closes"
    assert content.index(result) < fence_end


def test_the_resident_dag_skill_digest_routes_code_work_through_the_roster():
    """The orchestration skill is ``always: true`` with ``inject: description``:
    only its frontmatter description reaches an ordinary turn, and the body
    only after the model calls ``read_skill``. So the door the watch-work notes
    point at (a coding node feeding an on-call node) has to be stated in the
    description itself, or the resident digest keeps telling the model that
    code work is never a DAG while the notes tell it to dispatch one."""
    import raven

    skill = Path(raven.__file__).parent / "memory_engine" / "skills" / "subagent-dag-orchestration" / "SKILL.md"
    head = skill.read_text(encoding="utf-8").split("---")[1]
    description = next(line for line in head.splitlines() if line.startswith("description:"))
    assert "never a DAG" not in description
    assert "specialist" in description and "run_subagent_dag" in description
    assert "one graph per round" in description
