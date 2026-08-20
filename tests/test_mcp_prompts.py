"""The two prompt meta-tools, and what they do with an assistant-role message.

The load-bearing decision is in ``_render_prompt``: an assistant-role message is
kept and labelled rather than dropped. ``prompts/get`` returns them for few-shot
framing, so a template that demonstrates the answer shape loses its point without
them -- and keeping them is safe here precisely because this arrives as a tool
result the model reads, not as messages replayed into its own history.
"""

from __future__ import annotations

import json

from mcp import types

from raven.mcp.prompts import GetMcpPromptTool, ListMcpPromptsTool, prompt_tools


class _Session:
    def __init__(self, *, prompts=(), result=None, fail: Exception | None = None) -> None:
        self._prompts = list(prompts)
        self._result = result
        self._fail = fail
        self.calls: list[tuple[str, dict | None]] = []

    async def list_prompts(self):
        if self._fail:
            raise self._fail
        return types.ListPromptsResult(prompts=self._prompts)

    async def get_prompt(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self._fail:
            raise self._fail
        return self._result


class _Manager:
    def __init__(self, sessions: dict[str, _Session], *, offering: list[str] | None = None) -> None:
        self._sessions = sessions
        self._offering = sorted(sessions) if offering is None else offering

    def servers_offering(self, primitive: str) -> list[str]:
        return list(self._offering) if primitive == "prompts" else []

    def session_of(self, server: str):
        return self._sessions.get(server)


def _text(s: str) -> types.TextContent:
    return types.TextContent(type="text", text=s)


def _msg(role: str, text: str) -> types.PromptMessage:
    return types.PromptMessage(role=role, content=_text(text))


class TestListing:
    async def test_rows_carry_the_server_and_the_arguments(self):
        prompt = types.Prompt(
            name="triage",
            description="triage a bug",
            arguments=[types.PromptArgument(name="url", description="issue url", required=True)],
        )
        mgr = _Manager({"gh": _Session(prompts=[prompt])})
        rows = json.loads(await ListMcpPromptsTool(mgr).execute())
        assert rows[0]["server"] == "gh"
        assert rows[0]["arguments"] == [{"name": "url", "description": "issue url", "required": True}]

    async def test_omitting_the_server_covers_everyone_offering(self):
        p = types.Prompt(name="p")
        mgr = _Manager({"a": _Session(prompts=[p]), "b": _Session(prompts=[p])})
        rows = json.loads(await ListMcpPromptsTool(mgr).execute())
        assert sorted(r["server"] for r in rows) == ["a", "b"]

    async def test_one_broken_server_does_not_hide_the_others(self):
        mgr = _Manager({"good": _Session(prompts=[types.Prompt(name="p")]), "bad": _Session(fail=RuntimeError("x"))})
        out = await ListMcpPromptsTool(mgr).execute()
        body, _, errors = out.partition("\n\n")
        assert json.loads(body)[0]["server"] == "good"
        assert "bad" in errors

    async def test_nothing_offering_says_so(self):
        mgr = _Manager({}, offering=[])
        assert "no MCP server is serving prompts" in await ListMcpPromptsTool(mgr).execute()


class TestExpanding:
    def _result(self, *messages) -> types.GetPromptResult:
        return types.GetPromptResult(description="how to triage", messages=list(messages))

    async def test_an_assistant_message_is_kept_and_labelled(self):
        """The decision this module turns on.

        Dropping it would cost a few-shot template its demonstration. Keeping it
        unlabelled would let an outside server's text read as the model's own
        prior output.
        """
        res = self._result(_msg("user", "here is a bug"), _msg("assistant", "here is how I would answer"))
        mgr = _Manager({"gh": _Session(result=res)})
        out = await GetMcpPromptTool(mgr).execute(server="gh", name="triage")
        assert "[assistant]" in out
        assert "here is how I would answer" in out
        assert "[user]" in out

    async def test_the_description_leads(self):
        mgr = _Manager({"gh": _Session(result=self._result(_msg("user", "body")))})
        out = await GetMcpPromptTool(mgr).execute(server="gh", name="triage")
        assert out.startswith("# how to triage")

    async def test_arguments_reach_the_server(self):
        session = _Session(result=self._result(_msg("user", "x")))
        mgr = _Manager({"gh": session})
        await GetMcpPromptTool(mgr).execute(server="gh", name="triage", arguments={"url": "http://x"})
        assert session.calls == [("triage", {"url": "http://x"})]

    async def test_a_non_string_argument_is_cast_rather_than_refused(self):
        # The SDK wants strings; a model passing a number means the obvious thing
        # and refusing it costs a round-trip over a cast.
        session = _Session(result=self._result(_msg("user", "x")))
        mgr = _Manager({"gh": session})
        await GetMcpPromptTool(mgr).execute(server="gh", name="p", arguments={"n": 3, "flag": True})
        assert session.calls[0][1] == {"n": "3", "flag": "true"}

    async def test_arguments_sent_as_a_json_string_still_go_through(self):
        session = _Session(result=self._result(_msg("user", "x")))
        mgr = _Manager({"gh": session})
        await GetMcpPromptTool(mgr).execute(server="gh", name="p", arguments='{"url": "http://x"}')
        assert session.calls[0][1] == {"url": "http://x"}

    async def test_unparseable_arguments_are_refused_before_the_call(self):
        session = _Session(result=self._result(_msg("user", "x")))
        mgr = _Manager({"gh": session})
        out = await GetMcpPromptTool(mgr).execute(server="gh", name="p", arguments="{not json")
        assert "must be a JSON object" in out
        assert session.calls == []

    async def test_no_arguments_sends_none_not_an_empty_object(self):
        session = _Session(result=self._result(_msg("user", "x")))
        mgr = _Manager({"gh": session})
        await GetMcpPromptTool(mgr).execute(server="gh", name="p")
        assert session.calls == [("p", None)]

    async def test_non_text_content_is_described_never_stringified(self):
        # A pydantic repr would dump a whole base64 payload into the prompt as
        # prose -- the same failure the tool-result renderer exists to avoid.
        img = types.ImageContent(type="image", data="QUJD", mimeType="image/png")
        res = self._result(types.PromptMessage(role="user", content=img))
        mgr = _Manager({"gh": _Session(result=res)})
        out = await GetMcpPromptTool(mgr).execute(server="gh", name="p")
        assert "non-text prompt content" in out
        assert "QUJD" not in out

    async def test_an_empty_expansion_says_so(self):
        mgr = _Manager({"gh": _Session(result=types.GetPromptResult(messages=[]))})
        out = await GetMcpPromptTool(mgr).execute(server="gh", name="p")
        assert "expanded to nothing" in out

    async def test_a_server_side_failure_is_an_answer_not_a_crash(self):
        mgr = _Manager({"gh": _Session(fail=ValueError("missing arg"))})
        out = await GetMcpPromptTool(mgr).execute(server="gh", name="p")
        assert "failed" in out and "ValueError" in out


class TestTheExecutionTimeGate:
    async def test_a_server_that_stopped_offering_is_refused(self):
        mgr = _Manager({"gh": _Session()}, offering=[])
        out = await GetMcpPromptTool(mgr).execute(server="gh", name="p")
        assert "does not serve prompts" in out

    async def test_offered_but_unreachable_is_its_own_answer(self):
        mgr = _Manager({}, offering=["gh"])
        out = await GetMcpPromptTool(mgr).execute(server="gh", name="p")
        assert "not connected" in out

    async def test_a_missing_name_is_refused_before_the_call(self):
        session = _Session()
        mgr = _Manager({"gh": session})
        assert "'name' is required" in await GetMcpPromptTool(mgr).execute(server="gh", name="")
        assert session.calls == []


class TestTheSchema:
    def test_the_server_argument_lists_who_offers(self):
        mgr = _Manager({"gh": _Session(), "linear": _Session()})
        described = ListMcpPromptsTool(mgr).parameters["properties"]["server"]["description"]
        assert "gh, linear" in described

    def test_get_says_the_result_is_text_to_read_not_an_applied_instruction(self):
        # The spec positions prompts as user-selected; reaching them through a
        # tool makes them model-selected, so the description has to say what the
        # product is.
        mgr = _Manager({"gh": _Session()})
        by_name = {t.name: t for t in prompt_tools(mgr)}
        assert "not an instruction that has already been applied" in by_name["get_mcp_prompt"].description

    def test_required_arguments_are_declared(self):
        mgr = _Manager({"gh": _Session()})
        by_name = {t.name: t for t in prompt_tools(mgr)}
        assert by_name["list_mcp_prompts"].parameters["required"] == []
        assert by_name["get_mcp_prompt"].parameters["required"] == ["server", "name"]
