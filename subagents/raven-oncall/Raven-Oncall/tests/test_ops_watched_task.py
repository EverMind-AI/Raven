"""Judging whether a request is work to run and watch.

Measured three times on 2026-08-19 with one task -- a solver on this computer,
with a budget, a "tell me when you're done", and a machine the owner said they
were also using -- and three times iteration 1 went straight to a local shell.
The reasoning read the statement correctly every time; what never appeared was
the thought that this belonged on a machine and in a ledger. The same task with a
path on a different box goes through the whole loop, because looking there FAILS
and the failure arrives in a tool result.

So: something other than the acting model decides, and the decision is delivered
where a failure would be.
"""

from __future__ import annotations

import pytest

from raven.ops.watched import Verdict, build_prompt, provenance_line, read_verdict


def test_a_reply_that_cannot_be_read_is_not_watched():
    """A judgement sits in front of every path-touching call.

    An exception there would break looking at files, so anything unreadable has to
    leave the turn exactly as it would have been.
    """
    for junk in (None, "", "not json", "{", "[1, 2]", '{"watched": "sure"}'):
        assert read_verdict(junk).watched is False, junk


def test_a_reply_wrapped_in_chat_is_still_read():
    v = read_verdict('Sure thing! {"watched": true, "paths": ["/srv/case"]} — hope that helps')

    assert v.watched and v.paths == ["/srv/case"]


def test_paths_match_themselves_and_what_is_under_them():
    v = Verdict(watched=True, paths=["/srv/case"])

    assert v.claims("/srv/case")
    assert v.claims("/srv/case/deck/job.inp")
    assert not v.claims("/srv/case-old"), "a sibling with a longer name is not under it"
    assert not v.claims("/etc/hosts")
    assert not v.claims("")


def test_a_not_watched_verdict_claims_nothing():
    # The line is the whole effect, so "not watched" has to be inert -- otherwise a
    # wrong judgement would put machine advice on ordinary file reads.
    assert not Verdict(watched=False, paths=["/srv/case"]).claims("/srv/case")


def test_the_prompt_says_where_the_work_sits_is_irrelevant():
    prompt = build_prompt("帮我跑个算例，代码在 /srv/case，给你 25 分钟")[0]["content"]

    assert "/srv/case" in prompt and "25" in prompt
    assert "this very computer" in prompt, (
        "the failure being fixed is a local path reading as ordinary scripting"
    )
    assert "Not that" in prompt, "the negative half is what keeps ordinary work out"


def test_the_line_names_what_going_through_ops_buys():
    line = provenance_line()

    for earned in ("budget", "working directory", "wake"):
        assert earned in line, (
            "a nudge with no stated gain is one more rule; the loop chose a local "
            "shell three times because nothing said what it was giving up"
        )
    for tool in ("ops_connections", "ops_declare", "ops_submit"):
        assert tool in line


def test_the_line_covers_work_that_is_watched_rather_than_run():
    """It used to describe only an experiment -- "ops_submit runs a round against
    it", "a working directory per round". A campaign that watches a price runs no
    rounds and needs no directory, so a loop given a watch task did not recognise
    itself in the sign: measured 2026-08-21, it built its own monitor out of
    write_file and cron while every on-call tool sat registered and unused."""
    line = provenance_line()

    assert "condition" in line, "the shape a watch declares"
    assert "readings" in line, "what to read and when"
    assert "looks" in line, "a budget that is not machine time"
    assert "ops_check_later" in line, "coming back through the campaign, not a cron of your own"


@pytest.mark.asyncio
async def test_only_path_taking_tools_are_judged_and_only_once_a_turn(monkeypatch, on_call_enabled):
    """A request that never reaches for a path never pays for the judgement."""
    from raven.agent.loop import main as loop_main

    calls = {"n": 0}

    class _Loop:
        model = "stub"
        _watched_verdict = None
        _WATCHED_TOOLS = loop_main.AgentLoop._WATCHED_TOOLS
        _note_watched_path = loop_main.AgentLoop._note_watched_path

        async def _llm_call_stream(self, messages, tools, model):  # noqa: ANN001
            calls["n"] += 1

            class _R:
                content = '{"watched": true, "paths": ["/srv/case"]}'

            return _R()

    loop = _Loop()
    assert await loop._note_watched_path("message", {"content": "hi"}, "sent", "req") == "sent"
    assert calls["n"] == 0, "a tool that takes no path must not trigger a judgement"

    out = await loop._note_watched_path("list_dir", {"path": "/srv/case"}, "run.sh", "req")
    assert "ops_declare" in out and calls["n"] == 1

    again = await loop._note_watched_path("read_file", {"path": "/srv/case/run.sh"}, "x", "req")
    assert "ops_declare" in again and calls["n"] == 1, "the answer is cached for the turn"

    elsewhere = await loop._note_watched_path("read_file", {"path": "/etc/hosts"}, "x", "req")
    assert elsewhere == "x", "a path the owner never named gets nothing"


def _stub_loop(session: str = ""):
    from raven.agent.loop import main as loop_main

    class _Loop:
        model = "stub"
        _watched_verdict = None
        _watched_session = session
        _WATCHED_TOOLS = loop_main.AgentLoop._WATCHED_TOOLS
        _note_watched_path = loop_main.AgentLoop._note_watched_path

        async def _llm_call_stream(self, messages, tools, model):  # noqa: ANN001
            class _R:
                content = '{"watched": true, "paths": ["/srv/case"]}'

            return _R()

    return _Loop()


@pytest.mark.asyncio
async def test_naming_a_machine_does_not_silence_the_line(monkeypatch, on_call_enabled):
    """Naming a machine says the look is remote. It says nothing about whether
    anything is being recorded, and it used to skip the judgement outright.

    Measured 2026-08-21 on a watch task: ops_connections named the machine, every
    look afterwards carried machine=, so the line never appeared once -- and the
    loop went on to build its own monitor out of write_file and cron, never seeing
    that an on-call path existed. The condition was a proxy that failed exactly
    where the line was needed."""
    out = await _stub_loop()._note_watched_path(
        "exec", {"command": "ls /srv/case", "machine": "conn_box"}, "run.sh", "req")

    assert "ops_declare" in out


@pytest.mark.asyncio
async def test_a_window_that_already_has_a_campaign_is_left_alone(
    monkeypatch, tmp_path, on_call_enabled
):
    """The real test of "already on the ops path": something is being recorded.
    Telling a loop that is driving a campaign to go and declare one is noise."""
    import raven.agent.tools.ops as ops_mod
    from raven.ops.window import bind_window

    home = tmp_path / "ops"
    home.mkdir()
    monkeypatch.setattr(ops_mod, "_ops_home", lambda: home)
    bind_window(home, "tui:w1", "watch-volt", pid=None)

    out = await _stub_loop("tui:w1")._note_watched_path(
        "exec", {"command": "ls /srv/case", "machine": "conn_box"}, "run.sh", "req")

    assert out == "run.sh"


@pytest.mark.asyncio
async def test_a_command_gets_the_line_too(on_call_enabled):
    """This is the tool that does most of the looking, and it never got the line.

    ``re.findall`` on the command raised NameError -- the loop module had no
    ``import re`` -- and the except around it turned that into one debug line, so
    the judgement ran, was paid for, and its answer was thrown away. Found
    2026-08-21 while fixing the machine= skip that had been hiding it.
    """
    out = await _stub_loop()._note_watched_path(
        "exec", {"command": "tail -5 /srv/case/log.foam"}, "Time = 0.4", "req")

    assert "ops_declare" in out
