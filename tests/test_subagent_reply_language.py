"""What language a dispatched sub-agent answers in, on both routes that dispatch.

A raven-loop sub-agent renders the host's own system prompt and so already
carries the reply-language line. An acp product -- Raven-PPT, Raven-Design --
brings its own prompt and never sees ours, and one conversation held entirely
in Chinese came back with its sub-agent's narration entirely in English.

The dispatched task is the one thing every backend receives, so that is where
the language is stated. There are two routes to a backend and they share no
call: `spawn` goes through `SubagentManager.run_subagent`, and each node of a
graph goes through `dag_runner`. These drive both, at the boundary, so that
removing either call site is a red test rather than a silent hole.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven import i18n
from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.dag_graph import parse_dag_spec
from raven.agent.subagent.dag_runner import run_dag
from raven.agent.subagent.manager import SubagentManager
from raven.context_engine.segments import render
from raven.i18n import zh_lexicon

# From the lexicon rather than spelled here: AGENTS.md 1.3 keeps CJK out of
# source outside the named i18n zones, and a literal copy would also be a
# second place the name has to be kept right.
CHINESE = zh_lexicon.LANGUAGE_NAME


@pytest.fixture(autouse=True)
def _restore_language():
    """The language is process state; a case that sets it must put it back."""
    before = i18n.current_language()
    yield
    i18n.set_language(before)


class _Recorder:
    """Answers with a fixed string and keeps every prompt it was handed."""

    streams = False

    def __init__(self, seen: list[str]) -> None:
        self.seen = seen

    async def run(self, task: str, **kwargs: Any) -> str:
        self.seen.append(task)
        return "done"


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"


class _DummyExecutor:
    async def __aenter__(self) -> "_DummyExecutor":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


# --- the reading itself -----------------------------------------------------


class TestTheReading:
    """`reply_language` reads the process's dispensed language.

    CONTEXT.md seats the language in `raven.i18n`, set by the entrance -- the
    root CLI callback does it from `config.language` before any subcommand
    runs. Reading the file here instead would be a second reader of an owned
    key, and would answer wrongly in the case the owner exists for.
    """

    def test_chinese_is_named_in_full(self) -> None:
        i18n.set_language("zh")

        assert render.reply_language() == CHINESE

    def test_english_is_the_absence_of_a_directive(self) -> None:
        """Not the string "English": empty is what leaves the prompt untouched,
        and every caller branches on it."""
        i18n.set_language("en")

        assert render.reply_language() == ""

    def test_a_language_changed_while_serving_is_picked_up(self) -> None:
        """The console switches it through `i18n.set_language` on a live
        process, and a config read would not have seen that at all."""
        i18n.set_language("en")
        assert render.dispatch_language_line("t") == "t"

        i18n.set_language("zh")

        assert CHINESE in render.dispatch_language_line("t")

    def test_the_system_prompt_line_reads_the_same_source(self) -> None:
        """Two statements of one rule; a second reading is how they drift."""
        i18n.set_language("zh")

        assert CHINESE in render._language_directive()
        assert CHINESE in render.dispatch_language_line("t")


# --- how the line is written ------------------------------------------------


class TestTheLine:
    def test_the_task_stays_the_first_thing_it_says(self) -> None:
        """Appended, not prepended: the task's opening line is what a reader
        sees on the card, and it should be the work."""
        i18n.set_language("zh")

        out = render.dispatch_language_line("draw the poster\nfor a football club")

        assert out.splitlines()[0] == "draw the poster"

    def test_it_does_not_point_at_its_own_language(self) -> None:
        """The trap this sentence exists to avoid: an English instruction
        appended to an English task is read by a model following "answer in the
        language of the request" as a request for English -- the same defect by
        another road."""
        i18n.set_language("zh")

        out = render.dispatch_language_line("draw the poster")

        assert "whatever language this instruction or the task above happens to be in" in out


# --- the two dispatch routes ------------------------------------------------


async def _spawned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str) -> str:
    """One spawn through the real manager, returning what the backend got."""
    from raven.agent.subagent.spawn_tool import SpawnTool
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.session.manager import SessionManager

    i18n.set_language(language)
    seen: list[str] = []
    home = tmp_path / "home"
    mgr = SubagentManager(
        provider=_StubProvider(),
        workspace=home,
        session_dir=lambda key: SessionManager(home).session_dir(key),
        agents=[
            ThirdPartyCliSubagentConfig(
                name="Designer", command="cat {agent_id}", resume_command="cat --resume {agent_id}"
            )
        ],
    )
    mgr.set_submit(lambda _s: None)
    mgr.registry._backends["Designer"] = _Recorder(seen)
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())

    tool = SpawnTool(manager=mgr)
    tool.set_context("cli", "direct", "cli:direct")
    await tool.execute(node_id="n1", task_summary="draw it", prompt_template="draw the poster", subagent="Designer")
    for _ in range(400):
        if seen:
            break
        await _tick()
    assert seen, "the backend was never dispatched to"
    return seen[0]


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.01)


async def test_a_spawn_carries_the_language_to_the_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The route the report came from, driven end to end rather than through
    the helper: deleting the call in `run_subagent` reddens this."""
    handed = await _spawned(tmp_path, monkeypatch, "zh")

    assert handed.startswith("draw the poster")
    assert CHINESE in handed


async def test_a_spawn_on_an_english_install_is_dispatched_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default, and why this is safe to land: byte-identical text."""
    handed = await _spawned(tmp_path, monkeypatch, "en")

    assert handed == "draw the poster"


async def _graph_node(language: str) -> str:
    """One graph node through the real runner, returning what the backend got.

    `resolve` hands back the AGENT backend; `backend` is the graph's own file
    store. The in-memory store is the module next door's, reused rather than
    copied so this cannot drift from how the runner is exercised elsewhere.
    """
    from tests.test_subagent_dag_runner import _by_name, _InMemBackend

    i18n.set_language(language)
    seen: list[str] = []
    spec = parse_dag_spec(
        {
            "task_summary": "draw the deck",
            "nodes": [
                {"id": "a", "subagent": "Designer", "node_summary": "draw it", "prompt_template": "draw the poster"}
            ],
        }
    )
    result = await run_dag(
        spec,
        resolve=_by_name({"Designer": _Recorder(seen)}),
        backend=_InMemBackend(),
        workdir="/w",
        run_root="/hist/mas_dag",
        nodes_root="/hist/nodes",
        history_root="/hist",
    )
    assert result.summary["completed"] == 1
    assert seen, "the node backend was never dispatched to"
    return seen[0]


async def test_a_graph_node_carries_the_language_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_subagent_dag` never touches the manager: it goes straight from
    `_run_node` to `agent_backend.run`. A fix applied only to `spawn` leaves an
    acp node narrating in English, which is the reported defect intact."""
    handed = await _graph_node("zh")

    assert handed.startswith("draw the poster")
    assert CHINESE in handed


async def test_a_graph_node_on_an_english_install_is_dispatched_unchanged() -> None:
    handed = await _graph_node("en")

    assert handed == "draw the poster"


# --- what the announcement quotes back ------------------------------------


async def _announced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, authored: bool) -> str:
    """One spawn through the real manager, returning the completion announcement.

    `authored` is the difference between the two shapes of caller: the spawn
    tool passes `authored_task`, and the two sentinel routes
    (`sentinel/executor/spawn.py`, `sentinel/executor/action_executor.py`) do
    not. Both go through this same method.
    """
    from raven.config.schema import ThirdPartyCliSubagentConfig
    from raven.session.manager import SessionManager

    i18n.set_language("zh")
    home = tmp_path / "home"
    submitted: list[object] = []
    mgr = SubagentManager(
        provider=_StubProvider(),
        workspace=home,
        session_dir=lambda key: SessionManager(home).session_dir(key),
        agents=[
            ThirdPartyCliSubagentConfig(
                name="Designer", command="cat {agent_id}", resume_command="cat --resume {agent_id}"
            )
        ],
    )
    mgr.set_submit(submitted.append)
    mgr.registry._backends["Designer"] = _Recorder([])
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())

    kwargs = dict(
        task="water the plants",
        task_summary="water",
        origin_channel="cli",
        origin_chat_id="direct",
        agent="Designer",
    )
    if authored:
        # Deliberately NOT the same string as `task`. In production they
        # differ: the spawn tool passes the template unrendered while `task`
        # carries the rendering, precisely so a file inlined into the task is
        # not read back into the host's context. A fixture that passes the
        # same text either way cannot see the precedence at all.
        kwargs["authored_task"] = "{{ inputs.chore }}"
    await mgr.spawn(**kwargs)
    for _ in range(500):
        if submitted:
            break
        await _tick()
    assert submitted, "nothing was announced"
    return "\n".join(str(getattr(s, "text", s)) for s in submitted)


async def test_the_announcement_does_not_quote_our_own_line_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `Task:` line shows what was asked, not what we appended to it.

    `_announce_result` falls back to `task` when `origin["authored_task"]` is
    absent, and `task` stops being the model's wording the moment the language
    line is on it. Two of the three callers pass no `authored_task`, so on
    those routes the host was handed its own instruction back inside the
    quoted task -- an instruction addressed to the sub-agent, arriving as if
    the user had written it.

    Driven without `authored_task`, which is the sentinel shape and the one
    that reproduced.
    """
    text = await _announced(tmp_path, monkeypatch, authored=False)

    assert "water the plants" in text
    assert "[raven] Write your answer in" not in text


async def test_the_caller_that_states_its_own_wording_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default must not displace an `authored_task` a caller did pass --
    the spawn tool passes the template unrendered on purpose, so that a file
    inlined into the task is not read back into the host's context."""
    text = await _announced(tmp_path, monkeypatch, authored=True)

    assert "{{ inputs.chore }}" in text
    assert "water the plants" not in text
    assert "[raven] Write your answer in" not in text
