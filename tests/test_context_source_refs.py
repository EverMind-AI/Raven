"""A ref's promise, from the log that keeps it to the call that cashes it.

``[[ref session/...]]`` says one thing: the text this stands for is stored, and
you can have it back exactly. ``context_retrieve`` is what reads it back, and
what it returns has to be byte-identical -- a helpful summary of the original
would make the promise worthless in exactly the cases that motivated it.

A ref is scoped to the session's revision, so only the conversation it was
minted in, and only the log it was minted against, can answer it. History
written under the old 16000-character cap cannot be recovered, so retrieval
says ``truncated=true`` rather than passing off a prefix as the whole.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.context_retrieve import ContextRetrieveTool
from raven.session.manager import Session, SessionManager
from raven.session.refs import TOOL_RESULT_TRUNCATION_MARKER, TOOL_RESULT_VIEW_MAX_CHARS, session_ref, tool_result_view
from raven.utils.refs import SourceRef
from raven.utils.tokens import estimate_message_tokens

HUGE = ("Traceback (most recent call last):\n  File auth.ts, line 214, in refresh\n" * 600)[:40_000]
FILLER = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod " * 20


def _tool_turn(result: object) -> list[dict]:
    return [
        {"role": "user", "content": "run the tests"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": '{"command": "pytest"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": result},
        {"role": "assistant", "content": "one test fails"},
    ]


def _saved(tmp_path: Path, result: object, *, extra: list[dict] | None = None) -> tuple[SessionManager, Session]:
    """A session log written to disk, with the tool result as message 2."""
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("cli:direct")
    for message in [*_tool_turn(result), *(extra or [])]:
        session.record(dict(message))
    manager.save(session)
    return manager, session


def _retriever(manager: SessionManager, session: Session) -> ContextRetrieveTool:
    tool = ContextRetrieveTool(manager)
    tool.set_context("cli", "direct", session.key)
    return tool


def _ref(session: Session, lo: int, hi: int | None = None) -> SourceRef:
    return session_ref(session.revision, lo, hi)


class TestRetrieval:
    async def test_it_returns_the_stored_bytes_exactly(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, HUGE)

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()], max_tokens=32_000)

        assert out.split("\n", 1)[1] == HUGE

    async def test_it_reads_the_log_back_from_disk_when_the_session_is_not_in_memory(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, HUGE)
        fresh = SessionManager(tmp_path)

        out = await _retriever(fresh, session).execute(refs=[_ref(session, 2).render()], max_tokens=32_000)

        assert out.split("\n", 1)[1] == HUGE

    async def test_the_header_says_what_was_found(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, HUGE)

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()], max_tokens=32_000)

        header = out.split("\n", 1)[0]
        assert "role=tool" in header and "name=exec" in header and "chars=40,000" in header

    async def test_a_span_comes_back_in_order(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small result")

        out = await _retriever(manager, session).execute(refs=[_ref(session, 0, 3).render()])

        assert out.index("run the tests") < out.index("small result") < out.index("one test fails")

    async def test_a_ref_from_another_conversation_is_refused(self, tmp_path: Path) -> None:
        """A ref is a string the model can compose, so resolving whatever scope
        arrives would read another conversation's log on request."""
        manager, session = _saved(tmp_path, HUGE)
        other = manager.get_or_create("tg:someone-else")
        other.record({"role": "user", "content": "theirs"})
        manager.save(other)

        out = await _retriever(manager, session).execute(refs=[_ref(other, 0).render()])

        assert "not part of this conversation" in out
        assert "theirs" not in out

    async def test_a_ref_from_before_the_log_was_cleared_resolves_to_nothing(self, tmp_path: Path) -> None:
        """``/new`` restarts the numbering, so the old ordinal now names some
        other message. The log id in the scope turns that into a miss."""
        manager, session = _saved(tmp_path, HUGE)
        stale = _ref(session, 2).render()
        session.clear()
        for message in _tool_turn("a different result entirely"):
            session.record(message)
        manager.save(session)

        out = await _retriever(manager, session).execute(refs=[stale])

        assert "not part of this conversation" in out
        assert "a different result entirely" not in out

    async def test_a_ref_from_before_an_undo_resolves_to_nothing(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small")
        session.record({"role": "user", "content": "the message the ref will name"})
        doomed = len(session.messages) - 1
        stale = _ref(session, doomed).render()

        session.undo_last_turn()
        session.record({"role": "user", "content": "a different message at the same index"})
        manager.save(session)

        out = await _retriever(manager, session).execute(refs=[stale])

        assert "was rewritten" in out
        assert "a different message at the same index" not in out

    async def test_a_ref_from_a_later_generation_says_so(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small")
        ahead = SourceRef("session", f"{session.log_id}@{session.log_generation + 3}", 0, 0)

        out = await _retriever(manager, session).execute(refs=[ahead.render()])

        assert "names generation 3" in out and "at generation 0 here" in out
        assert "rewritten" not in out

    async def test_every_ref_in_one_string_is_read(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small result")

        out = await _retriever(manager, session).execute(
            refs=[f"see {_ref(session, 0).render()} and {_ref(session, 2).render()}"]
        )

        assert "run the tests" in out and "small result" in out

    async def test_a_legacy_log_not_in_memory_names_the_same_scope_on_every_read(self, tmp_path: Path) -> None:
        path = tmp_path / "sessions" / "cli" / "direct.jsonl"
        path.parent.mkdir(parents=True)
        records = [
            {"_type": "metadata", "key": "cli:direct", "created_at": "2026-01-01T10:00:00", "metadata": {}},
            *_tool_turn("legacy result"),
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
        minted = SessionManager(tmp_path).peek("cli:direct")
        tool = ContextRetrieveTool(SessionManager(tmp_path))
        tool.set_context("cli", "direct", "cli:direct")

        out = await tool.execute(refs=[_ref(minted, 2).render()])

        assert out.split("\n", 1)[1] == "legacy result"
        assert path.read_text(encoding="utf-8") == "".join(json.dumps(record) + "\n" for record in records)

    @pytest.mark.parametrize("max_tokens", [256, 1_000, 4_000])
    async def test_a_cut_entry_stays_inside_the_budget(self, tmp_path: Path, max_tokens: int) -> None:
        manager, session = _saved(tmp_path, HUGE)

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()], max_tokens=max_tokens)

        assert "shown=" in out.split("\n", 1)[0]
        assert estimate_message_tokens({"role": "tool", "content": out}) <= max_tokens

    async def test_an_index_past_the_end_says_so(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small")

        out = await _retriever(manager, session).execute(refs=[_ref(session, 99).render()])

        assert "out of range" in out and "4 message(s)" in out

    async def test_a_span_running_past_the_end_says_so(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small")

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2, 4).render()])

        assert "out of range" in out

    async def test_another_store_is_not_this_tools_business(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small")

        out = await _retriever(manager, session).execute(refs=["[[ref shared_memory/raven-code#7-9]]"])

        assert "not retrievable here" in out

    async def test_something_that_is_not_a_ref_is_reported_not_raised(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small")

        out = await _retriever(manager, session).execute(refs=["message 2 please"])

        assert "is not a ref" in out

    async def test_history_written_under_the_old_cap_is_reported_as_truncated(self, tmp_path: Path) -> None:
        """The bytes were never stored. Returning the prefix as though it were
        the whole answer is the one thing this must not do."""
        manager, session = _saved(tmp_path, HUGE[:16_000] + TOOL_RESULT_TRUNCATION_MARKER)

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()], max_tokens=32_000)

        assert "truncated=true" in out.split("\n", 1)[0]
        assert out.split("\n", 1)[1] != HUGE

    async def test_a_result_stored_whole_is_not_reported_as_truncated(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, HUGE)

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()], max_tokens=32_000)

        assert "truncated" not in out.split("\n", 1)[0]

    async def test_the_budget_binds_and_says_it_did(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, HUGE)

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()], max_tokens=256)

        assert len(out) < len(HUGE)
        assert "shown=" in out.split("\n", 1)[0]

    async def test_the_default_budget_binds_on_a_large_result(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, HUGE)

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()])

        assert "shown=" in out.split("\n", 1)[0]
        assert HUGE not in out

    async def test_a_snippet_is_the_two_ends(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, HUGE)

        out = await _retriever(manager, session).execute(
            refs=[_ref(session, 2).render()], mode="snippet", max_tokens=32_000
        )

        assert HUGE[:400] in out and HUGE[-400:] in out
        assert len(out) < len(HUGE) // 10
        assert "ask again with mode=exact" in out

    async def test_an_unbound_tool_retrieves_nothing(self, tmp_path: Path) -> None:
        """Every registered tool is bound per turn; one that answered without a
        session would be answering about whichever session ran last."""
        out = await ContextRetrieveTool(SessionManager(tmp_path)).execute(refs=["[[ref session/x@0#1]]"])

        assert out.startswith("Error: context_retrieve has no session bound")

    async def test_an_empty_request_is_an_error(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small")

        assert (await _retriever(manager, session).execute(refs=[])).startswith("Error:")

    async def test_a_ref_arriving_as_a_bare_string_is_still_read(self, tmp_path: Path) -> None:
        """Models pass one of something where a list was asked for often
        enough that refusing would be a round trip spent on syntax."""
        manager, session = _saved(tmp_path, "small result")

        out = await _retriever(manager, session).execute(refs=_ref(session, 2).render())

        assert "small result" in out

    async def test_an_address_without_its_brackets_is_still_read(self, tmp_path: Path) -> None:
        """Measured on a live run: the model copied the address out of the tag
        and left the brackets behind, then took the refusal as "gone"."""
        manager, session = _saved(tmp_path, "small result")
        bare = _ref(session, 2).render().removeprefix("[[ref ").removesuffix("]]")

        out = await _retriever(manager, session).execute(refs=[bare])

        assert "small result" in out

    async def test_a_list_serialised_into_one_string_is_still_read(self, tmp_path: Path) -> None:
        """The same live call also sent the list as a JSON string."""
        import json as _json

        manager, session = _saved(tmp_path, "small result")
        bare = _ref(session, 2).render().removeprefix("[[ref ").removesuffix("]]")

        out = await _retriever(manager, session).execute(refs=_json.dumps([bare]), max_tokens="8000")

        assert "small result" in out

    async def test_an_entry_larger_than_one_call_is_read_in_turns(self, tmp_path: Path) -> None:
        """A filed result can be far larger than one call's budget; the cut answer
        names where to start next, and the turns join back into the original."""
        import re as _re

        manager, session = _saved(tmp_path, HUGE)
        tool, ref, offset, pieces = _retriever(manager, session), _ref(session, 2).render(), 0, []
        for _ in range(40):
            out = await tool.execute(refs=[ref], max_tokens=1_000, offset=offset)
            header, body = out.split("\n", 1)
            pieces.append(body)
            nxt = _re.search(r"next_offset=(\d+)", header)
            if nxt is None:
                break
            offset = int(nxt.group(1))
        assert len(pieces) > 1, "one call should not have held the whole entry"
        assert "".join(pieces) == HUGE

    async def test_an_offset_past_the_end_reads_nothing_more(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small result")
        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()], offset=10_000)
        assert out.split("\n", 1)[1] == ""
        assert "next_offset" not in out

    def test_a_read_back_needs_no_approval(self) -> None:
        from raven.permissions.rules import Tier, default_tier

        assert default_tier("context_retrieve", {"refs": ["[[ref session/x@0#1]]"]}) is Tier.ALLOW

    async def test_an_unusable_budget_falls_back_to_the_default(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small result")

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()], max_tokens="lots")

        assert "small result" in out

    async def test_an_unknown_mode_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small result")

        assert (await _retriever(manager, session).execute(refs=["x"], mode="summary")).startswith("Error:")

    async def test_a_session_with_no_log_is_reported(self, tmp_path: Path) -> None:
        tool = ContextRetrieveTool(SessionManager(tmp_path))
        tool.set_context("cli", "direct", "cli:direct")

        assert (await tool.execute(refs=["[[ref session/x@0#1]]"])).startswith("Error:")

    async def test_a_multimodal_result_comes_back_as_its_stored_shape(self, tmp_path: Path) -> None:
        """Images are stripped before the write, so what a ref names here is
        the block list the log actually holds."""
        manager, session = _saved(tmp_path, [{"type": "text", "text": "a"}])

        out = await _retriever(manager, session).execute(refs=[_ref(session, 2).render()])

        assert '"text": "a"' in out

    async def test_a_span_that_outruns_the_budget_says_where_it_stopped(self, tmp_path: Path) -> None:
        filler = [{"role": "user", "content": f"and then {i} {FILLER}"} for i in range(5)]
        manager, session = _saved(tmp_path, HUGE, extra=filler)

        out = await _retriever(manager, session).execute(refs=[_ref(session, 0, 8).render()], max_tokens=256)

        assert "the token budget for this call is spent" in out

    async def test_refs_the_budget_did_not_reach_are_named(self, tmp_path: Path) -> None:
        """Silently answering two of three asked-for refs would read as "the
        other two hold nothing"."""
        manager, session = _saved(tmp_path, HUGE)
        ref = _ref(session, 2).render()

        out = await _retriever(manager, session).execute(refs=[ref, ref, ref], max_tokens=256)

        assert "more ref(s) not read" in out

    async def test_a_long_list_is_answered_up_to_its_limit(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, "small")

        out = await _retriever(manager, session).execute(refs=[_ref(session, 0).render()] * 30)

        assert "beyond the 20 this call answers were ignored" in out

    async def test_a_retrieve_leaves_the_session_and_its_file_alone(self, tmp_path: Path) -> None:
        manager, session = _saved(tmp_path, HUGE)
        path = manager.session_path(session.key)
        before_bytes = path.read_bytes()
        before_revision = session.revision
        fresh = SessionManager(tmp_path)

        for tool in (_retriever(manager, session), _retriever(fresh, session)):
            await tool.execute(refs=[_ref(session, 0, 3).render(), "[[ref session/x@0#1]]"], max_tokens=32_000)

        assert path.read_bytes() == before_bytes
        assert session.revision == before_revision
        assert fresh.exists(session.key) and "cli:direct" not in fresh._cache


class TestScopeNaming:
    def test_the_scope_survives_appends_and_changes_on_a_clear(self, tmp_path: Path) -> None:
        """What makes an ordinal a stable name: appending cannot renumber what
        is already written, and clearing renumbers everything."""
        _manager, session = _saved(tmp_path, "small")
        before = session.revision.scope
        session.record({"role": "user", "content": "one more"})
        assert session.revision.scope == before

        session.clear()
        assert session.revision.scope != before

    def test_an_undo_changes_the_scope_even_though_message_zero_does_not(self, tmp_path: Path) -> None:
        """A ref minted before the cut has to stop resolving, or it names one
        message and returns another."""
        _manager, session = _saved(tmp_path, "small")
        session.record({"role": "user", "content": "a question I will take back"})
        session.record({"role": "assistant", "content": "an answer to it"})
        before = session.revision.scope
        anchor_before = session.messages[0]

        assert session.undo_last_turn()
        assert session.messages[0] == anchor_before
        assert session.revision.scope != before

    def test_a_minted_ref_parses_back_to_itself(self, tmp_path: Path) -> None:
        _manager, session = _saved(tmp_path, "small")
        ref = _ref(session, 1, 3)

        assert SourceRef.parse(f"see {ref.render()} for the output") == ref

    def test_an_empty_log_still_names_a_scope(self) -> None:
        assert Session(key="cli:direct").revision.scope.endswith("@0")


def test_a_tool_result_view_cuts_long_text_and_says_how_much() -> None:
    assert tool_result_view("short") == "short"
    exact = "x" * TOOL_RESULT_VIEW_MAX_CHARS
    assert tool_result_view(exact) == exact

    cut = tool_result_view("y" * 40_000)

    assert cut.startswith("y" * TOOL_RESULT_VIEW_MAX_CHARS + "\n")
    assert "24,000 more characters of the stored result not shown here" in cut
    assert len(cut) < TOOL_RESULT_VIEW_MAX_CHARS + 200


def test_a_result_cut_at_filing_within_the_view_goes_out_as_stored() -> None:
    filed = "z" * TOOL_RESULT_VIEW_MAX_CHARS + TOOL_RESULT_TRUNCATION_MARKER

    assert tool_result_view(filed) == filed

    longer = "z" * (TOOL_RESULT_VIEW_MAX_CHARS + 500) + TOOL_RESULT_TRUNCATION_MARKER
    assert "500 more characters of the stored result" in tool_result_view(longer)
