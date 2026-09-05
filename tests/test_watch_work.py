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
        {"role": "user", "content": "[Runtime Context]\nnow=12:00\n\n跑一个算例,25 分钟"},
    ]
    assert watch_work.asked_for(messages) == "跑一个算例,25 分钟"


def test_asked_for_handles_block_content_and_empty_history():
    messages = [{"role": "user", "content": [{"type": "text", "text": "watch this"}]}]
    assert watch_work.asked_for(messages) == "watch this"
    assert watch_work.asked_for([]) == ""


# --- oncall_agent: who on this roster runs work on machines ---


@pytest.mark.asyncio
async def test_the_agent_whose_manifest_claims_machines_is_the_one_named(monkeypatch):
    monkeypatch.setattr(
        "raven.agent.subagent.dag_machines.runs_on_machines",
        lambda agent: agent == "Raven-Oncall",
    )

    assert await watch_work.oncall_agent(["Raven-Code", "Raven-Oncall"]) == "Raven-Oncall"


@pytest.mark.asyncio
async def test_an_empty_registry_still_names_the_agent(monkeypatch):
    """The flag is about the agent, not the registry's contents: an empty
    registry must still name the specialist -- the spawn-side check is what
    refuses it, and that refusal is how the owner gets asked for a machine."""
    monkeypatch.setattr("raven.agent.subagent.dag_machines.runs_on_machines", lambda agent: True)

    assert await watch_work.oncall_agent(["Raven-Oncall"]) == "Raven-Oncall"


@pytest.mark.asyncio
async def test_a_roster_nobody_flags_names_nobody(monkeypatch):
    monkeypatch.setattr("raven.agent.subagent.dag_machines.runs_on_machines", lambda agent: False)

    assert await watch_work.oncall_agent(["Raven-Code"]) is None


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
    def __init__(self, names):
        self._names = names

    def _agents(self):
        from types import SimpleNamespace

        return [SimpleNamespace(name=n) for n in self._names]


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


@pytest.fixture
def oncall_on_roster(monkeypatch):
    async def fake_oncall(names):
        return "Raven-Oncall" if "Raven-Oncall" in names else None

    monkeypatch.setattr(watch_work, "oncall_agent", fake_oncall)


@pytest.mark.asyncio
async def test_a_look_at_the_named_path_gains_the_line(oncall_on_roster):
    loop, state = _loop()
    out = await loop._note_watch_work(
        state, "read_file", {"path": "/tmp/arena/run.sh"}, "file contents", "跑 /tmp/arena,预算 25 分钟"
    )
    assert "spawn `Raven-Oncall`" in out
    assert out.startswith("file contents"), "the result itself must survive intact"


@pytest.mark.asyncio
async def test_exec_commands_are_searched_for_the_path(oncall_on_roster):
    loop, state = _loop()
    out = await loop._note_watch_work(
        state, "exec", {"command": "cd /tmp/arena/runs && bash run.sh"}, "ok", "跑 /tmp/arena"
    )
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
async def test_the_judgement_is_paid_once_per_turn(oncall_on_roster):
    loop, state = _loop()
    for _ in range(3):
        await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert loop._llm_calls == 1


@pytest.mark.asyncio
async def test_a_not_watched_verdict_leaves_every_result_alone(oncall_on_roster):
    loop, state = _loop(verdict='{"watched": false, "paths": []}')
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "读一下这个文件")
    assert out == "r"


@pytest.mark.asyncio
async def test_a_look_elsewhere_stays_clean_even_on_a_watched_turn(oncall_on_roster):
    loop, state = _loop()
    out = await loop._note_watch_work(state, "read_file", {"path": "/etc/hosts"}, "r", "跑 /tmp/arena")
    assert out == "r"


@pytest.mark.asyncio
async def test_after_a_real_oncall_dispatch_the_turn_goes_quiet(oncall_on_roster):
    loop, state = _loop()
    await loop._note_watch_work(state, "spawn", {"subagent": "Raven-Oncall"}, ACCEPTED, "跑 /tmp/arena")
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert out == "r"
    assert loop._llm_calls == 0, "a handed-over turn must not pay for the judgement"


@pytest.mark.asyncio
async def test_an_unrelated_spawn_silences_nothing(oncall_on_roster):
    """Reproduced in review: a spawn of another agent read as a hand-off and
    suppressed the next matching path nudge."""
    loop, state = _loop(names=("Raven-Code", "Raven-Oncall"))
    await loop._note_watch_work(
        state, "spawn", {"subagent": "Raven-Code"}, ACCEPTED.replace("x", "code job"), "跑 /tmp/arena"
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        "Spawn refused: delegation is paused",
        "Error: prompt_template names a file this sub-agent cannot be given",
    ],
)
async def test_a_refused_dispatch_of_any_shape_silences_nothing(oncall_on_roster, refusal):
    loop, state = _loop()
    await loop._note_watch_work(state, "spawn", {"subagent": "Raven-Oncall"}, refusal, "跑 /tmp/arena")
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
async def test_two_interleaved_turns_never_share_watch_state(oncall_on_roster):
    """run_turn lets turns from other sessions run concurrently on the one
    loop object: B's dispatch must not silence A, and A's verdict must not
    answer for B."""
    loop, state_a = _loop()
    state_b = watch_work.TurnWatch()

    await loop._note_watch_work(state_b, "spawn", {"subagent": "Raven-Oncall"}, ACCEPTED, "跑 /tmp/arena")
    out_a = await loop._note_watch_work(state_a, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")

    assert "spawn `Raven-Oncall`" in out_a, "B's hand-off silenced A"
    assert state_b.dispatched and not state_a.dispatched
    assert state_a.verdict is not None and state_b.verdict is None, "the verdict stayed with A's turn"


@pytest.mark.asyncio
async def test_a_roster_without_the_specialist_asks_no_judgement(oncall_on_roster):
    loop, state = _loop(names=("Raven-Code",))
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert out == "r"
    assert loop._llm_calls == 0


@pytest.mark.asyncio
async def test_a_judgement_that_blows_up_never_reaches_the_result(oncall_on_roster, monkeypatch):
    loop, state = _loop()

    async def broken(names):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(watch_work, "oncall_agent", broken)
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert out == "r"


# --- the spawn-side half: nowhere to run is refused before anything runs ---


@pytest.mark.asyncio
async def test_spawning_a_machineless_oncall_agent_is_refused_with_the_ask(monkeypatch):
    """The DAG path had this check; the failure that bought it here was a single
    spawn -- the agent booted, found the registry empty, and the owner learned
    it one sub-agent run too late."""
    from raven.agent.subagent import dag_machines as _machines
    from raven.agent.subagent.spawn_tool import SpawnTool

    async def fake_verdict(agents, **kwargs):
        return _machines.Verdict(agent=agents[0], usable=0, listed=0)

    monkeypatch.setattr(_machines, "verdict_for_async", fake_verdict)

    class _Manager:
        async def spawn(self, **kwargs):
            raise AssertionError("nothing may be dispatched")

    tool = SpawnTool(manager=_Manager())
    out = await tool.execute("watch a case", "run the case", subagent="Raven-Oncall")

    assert "Nothing was dispatched" in out
    for asked in ("port", "private key", "installed", "budget"):
        assert asked in out, f"the owner must be asked for the {asked}"


@pytest.mark.asyncio
async def test_a_spawn_nobody_answers_for_dispatches_as_before(monkeypatch, tmp_path):
    from raven.agent.subagent import dag_machines as _machines
    from raven.agent.subagent.spawn_tool import SpawnTool

    async def fake_verdict(agents, **kwargs):
        return None

    monkeypatch.setattr(_machines, "verdict_for_async", fake_verdict)

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
    out = await tool.execute("do a thing", "the task", subagent="Raven-Code")

    assert mgr.spawned and out == "spawned ok"


# --- a refused dispatch answers nothing: the pressure must survive it ---


@pytest.mark.asyncio
async def test_a_machineless_refusal_keeps_the_turn_nudged_and_sharpens_it(oncall_on_roster):
    """The first live run: spawn refused for having no machine, and the plain
    flag silenced every look after it -- at exactly the moment the model chose
    to run trials by hand."""
    loop, state = _loop()
    refusal = "'Raven-Oncall' runs work on the owner's machines...\nNothing was dispatched. Ask the owner..."
    out = await loop._note_watch_work(state, "spawn", {"subagent": "Raven-Oncall"}, refusal, "跑 /tmp/arena")
    assert out == refusal, "the refusal itself is not annotated"

    after = await loop._note_watch_work(state, "exec", {"command": "bash /tmp/arena/run.sh"}, "ok", "跑 /tmp/arena")
    assert "STOP running this by hand" in after
    assert "end your turn" in after


@pytest.mark.asyncio
async def test_a_successful_dispatch_still_goes_quiet(oncall_on_roster):
    loop, state = _loop()
    await loop._note_watch_work(state, "spawn", {"subagent": "Raven-Oncall"}, ACCEPTED, "跑 /tmp/arena")
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert out == "r"


def test_the_machineless_nudge_names_the_ask_and_the_stop():
    line = watch_work.machineless_nudge("Raven-Oncall")
    assert "STOP" in line
    for asked in ("ssh", "address/port/user/key", "budget", "jobs at once"):
        assert asked in line
    assert "connection add" in line


@pytest.mark.asyncio
async def test_the_refusal_carries_the_runnable_add_command(monkeypatch):
    """Registration has to be one runnable line. Measured 2026-08-27, before
    `ops connection` was lifted into this install: the model collected every
    answer from the owner, found no command to put them in, and hand-ran the
    work over ssh instead."""
    from raven.agent.subagent import dag_machines as _machines
    from raven.agent.subagent.spawn_tool import SpawnTool

    async def fake_verdict(agents, **kwargs):
        return _machines.Verdict(agent=agents[0], usable=0, listed=0)

    monkeypatch.setattr(_machines, "verdict_for_async", fake_verdict)

    tool = SpawnTool(manager=None)
    out = await tool.execute("watch a case", "run it", subagent="Raven-Oncall")

    assert "raven ops connection add --non-interactive" in out
    assert "--transport local" in out, "the this-very-computer shape must be named too"
    assert "tell them in your reply what you wrote down" in out, (
        "self-registration of this very computer is allowed, but the owner is told"
    )


@pytest.mark.asyncio
async def test_a_declined_graph_silences_nothing(oncall_on_roster):
    """Reproduced in review: the declined-graph message carries no error prefix,
    and the old DAG leg searched the serialized call, so naming the on-call
    agent in the graph was enough to read a non-dispatch as a hand-off."""
    loop, state = _loop()
    await loop._note_watch_work(
        state,
        "run_subagent_dag",
        {"nodes": [{"id": "watch", "subagent": "Raven-Oncall"}]},
        "The user did not approve this graph, so nothing was run. Do not re-submit it; ask what to change.",
        "跑 /tmp/arena",
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
async def test_a_graph_that_merely_mentions_the_agent_silences_nothing(oncall_on_roster):
    loop, state = _loop(names=("Raven-Code", "Raven-Oncall"))
    await loop._note_watch_work(
        state,
        "run_subagent_dag",
        {"nodes": [{"id": "build", "subagent": "Raven-Code", "prompt_template": "hand off to Raven-Oncall later"}]},
        "DAG run r1 started in the background (1 nodes).",
        "跑 /tmp/arena",
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert "spawn `Raven-Oncall`" in out


@pytest.mark.asyncio
async def test_an_accepted_graph_with_an_oncall_node_goes_quiet(oncall_on_roster):
    loop, state = _loop(names=("Raven-Code", "Raven-Oncall"))
    await loop._note_watch_work(
        state,
        "run_subagent_dag",
        {"nodes": [{"id": "build", "subagent": "Raven-Code"}, {"id": "watch", "subagent": "Raven-Oncall"}]},
        "DAG run r2 started in the background (2 nodes).",
        "跑 /tmp/arena",
    )
    out = await loop._note_watch_work(state, "read_file", {"path": "/tmp/arena/x"}, "r", "跑 /tmp/arena")
    assert out == "r"
