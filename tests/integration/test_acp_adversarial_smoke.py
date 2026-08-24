"""``raven acp`` under a client that is legal and unhelpful.

Every case here is something a real client does -- string request ids, a version
of the wrong type, two requests in flight, a frame split across writes -- and
each one is a place an agent can be accidentally right. The point of driving the
real binary is that none of it is mocked: real pipes, real framing, real
concurrency, real teardown.

The agent home is a throwaway directory, which also means no provider is
configured. That is deliberate rather than a limitation: it makes the run
reproducible, and it puts the "a turn could not start" path -- which must still
answer with a stop reason rather than an error -- on the happy path of the test
instead of behind a credential.

Isolating it takes three things, not one. ``RAVEN_HOME`` moves only tracing and
runtime data; ``get_config_path`` reads ``Path.home() / ".raven/config.json"``
whatever it says, and a provider key in the environment is read before either.
With only ``RAVEN_HOME`` set, this file loaded the operator's own configuration,
sent its ``hello`` prompt to a real provider, and hung until the case timed out
-- a test that advertises itself as credential-free spending somebody's key, and
answering differently on every machine.

Marked ``integration`` because it spawns the binary.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.acp_schema import validate_inbound, validate_outbound
from tests.acp_stub_client import raven_binary, stub_client

pytestmark = pytest.mark.integration


def _provider_env_keys() -> tuple[str, ...]:
    """Every environment variable a provider would read a credential from.

    Read off the registry rather than listed here, so a provider added later is
    isolated by the same fixture instead of quietly re-arming this file.
    """
    from raven.providers.registry import PROVIDERS

    return tuple(sorted({spec.env_key for spec in PROVIDERS if getattr(spec, "env_key", None)}))


@pytest.fixture
def home(tmp_path):
    if not raven_binary().exists():
        pytest.skip(f"raven console script not installed at {raven_binary()}")
    root = tmp_path / "home"
    user = tmp_path / "user"
    user.mkdir()
    env = {
        "RAVEN_HOME": str(root),
        # What actually moves the config file. USERPROFILE alongside it because
        # that is the one ``Path.home()`` reads on Windows.
        "HOME": str(user),
        "USERPROFILE": str(user),
    }
    # Emptied rather than left alone: a key in the parent's environment reaches
    # the child whatever the config file says, and this suite's contract is that
    # no turn here can reach a provider.
    env.update({key: "" for key in _provider_env_keys()})
    return env


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


class TestRequestIds:
    async def test_a_string_id_comes_back_as_the_same_string(self, home):
        """The stub mints string ids by default. An agent that keyed its pending
        map by ``int`` answers with a number, and the client never correlates the
        reply it did receive."""
        async with stub_client(env=home) as client:
            response = await client.request("initialize", {"protocolVersion": 1}, request_id="not-a-number")

            assert response["id"] == "not-a-number"

    @pytest.mark.parametrize("request_id", [0, -1, 2**53])
    async def test_awkward_but_legal_integer_ids_are_answered(self, home, request_id):
        """``0`` is the one that catches a truthiness check on the id, and the
        large value catches an agent that round-trips ids through a float."""
        async with stub_client(env=home) as client:
            response = await client.request("initialize", {"protocolVersion": 1}, request_id=request_id)

            assert response["id"] == request_id


class TestConcurrency:
    async def test_a_second_request_is_answered_while_the_first_is_suspended(self, home, project):
        """Two requests in flight is legal, and this is the shape cancellation
        depends on: an agent handling frames inline would leave the second frame
        unread until the first finished, which for a prompt is never."""
        async with stub_client(env=home) as client:
            await client.handshake()
            session = await client.new_session(project)
            slow = await client.send_request(
                "session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "hello"}]}
            )
            fast = await client.request("initialize", {"protocolVersion": 1})

            assert "result" in fast, "the handshake must be answered without waiting for the prompt"
            answer = await asyncio.wait_for(slow, timeout=60.0)
            assert "result" in answer


class TestToleranceAtTheHandshake:
    async def test_a_version_string_is_read_rather_than_rejected(self, home):
        """Failing the handshake over a type denies the client the one thing it
        needs to decide what to do: which version the agent serves."""
        async with stub_client(env=home) as client:
            result = await client.handshake(protocolVersion="2025-06-18")

            assert result["protocolVersion"] == 1

    async def test_unknown_params_are_ignored(self, home):
        async with stub_client(env=home) as client:
            result = await client.handshake(clientInfo={"name": "stub"}, futureField=[1, 2, 3])

            assert result["authMethods"] == []

    async def test_the_declared_capabilities_are_a_legal_agent_frame(self, home):
        async with stub_client(env=home) as client:
            await client.handshake()

            validate_outbound(client.frames[0])


class TestSessionRefusals:
    async def test_a_relative_cwd_is_refused_with_the_reason_attached(self, home):
        """``validate_override`` raises a bare ``ValueError``. A Python exception
        is not an acceptable handshake failure, and a client cannot show a person
        what to fix without the reason."""
        async with stub_client(env=home) as client:
            await client.handshake()
            response = await client.request("session/new", {"cwd": "relative/path", "mcpServers": []})

            assert response["error"]["code"] == -32602
            assert response["error"]["data"]["field"] == "cwd"
            assert "absolute" in response["error"]["message"]

    async def test_a_cwd_containing_the_agent_home_is_refused(self, home):
        """Not an arbitrary rule: a per-turn checkpoint that adds everything under
        the working directory cannot be saved by a ``.raven`` exclude when the
        agent home is *inside* the work tree -- so a session rooted here would
        commit provider keys into a shadow repository.

        The path is derived from the child's own ``HOME``, not from this
        process's configuration. ``workspace_path`` defaults to
        ``$HOME/.raven/workspace``, so the isolated home the fixture hands the
        child *is* an ancestor of the agent home the child will refuse for --
        while reading it from ``load_config()`` here would name the operator's
        directory, which the child no longer has and would not refuse. Nothing is
        created either way: the refusal happens before a session exists.
        """
        ancestor = home["HOME"]

        async with stub_client(env=home) as client:
            await client.handshake()
            response = await client.request("session/new", {"cwd": ancestor, "mcpServers": []})

            assert response["error"]["code"] == -32602, response

    async def test_per_session_mcp_servers_are_refused_not_ignored(self, home, project):
        async with stub_client(env=home) as client:
            await client.handshake()
            response = await client.request(
                "session/new",
                {"cwd": str(project), "mcpServers": [{"name": "s", "command": "true", "args": []}]},
            )

            assert response["error"]["code"] == -32602
            assert response["error"]["data"]["field"] == "mcpServers"

    async def test_the_connection_survives_every_refusal(self, home, project):
        async with stub_client(env=home) as client:
            await client.handshake()
            for params in ({"cwd": "rel", "mcpServers": []}, {"mcpServers": []}, {"cwd": str(project)}):
                await client.request("session/new", params)

            assert await client.new_session(project)


class TestMalformedInput:
    async def test_invalid_utf8_is_answered_and_the_next_frame_lands(self, home):
        """Strict decoding rather than ``errors="replace"``: replacement would
        accept a corrupted request and act on it."""
        async with stub_client(env=home) as client:
            await client.write_bytes(b'{"jsonrpc":"2.0","id":1,"method":"\xff\xfe"}\n')
            response = await client.request("initialize", {"protocolVersion": 1})

            assert "result" in response
            assert any(f.get("error", {}).get("code") == -32700 for f in client.frames)

    async def test_two_frames_in_one_write_are_both_answered(self, home):
        async with stub_client(env=home) as client:
            first = await client.send_request("initialize", {"protocolVersion": 1}, request_id="a")
            payload = json.dumps({"jsonrpc": "2.0", "id": "b", "method": "initialize", "params": {}}).encode()
            await client.write_bytes(payload + b"\n")
            second = await client.send_request("initialize", {"protocolVersion": 1}, request_id="c")

            assert "result" in await asyncio.wait_for(first, 60.0)
            assert "result" in await asyncio.wait_for(second, 60.0)
            assert any(f.get("id") == "b" for f in client.frames)

    async def test_a_frame_split_across_writes_is_reassembled(self, home):
        """What a client under load actually produces. An agent that treated each
        read as a frame would answer a parse error for a perfectly good request."""
        async with stub_client(env=home) as client:
            raw = json.dumps({"jsonrpc": "2.0", "id": "split", "method": "initialize", "params": {}}).encode()
            await client.write_bytes(raw[:10])
            await asyncio.sleep(0.05)
            await client.write_bytes(raw[10:] + b"\n")

            response = await client.request("initialize", {"protocolVersion": 1}, request_id="after")

            assert "result" in response
            assert any(f.get("id") == "split" and "result" in f for f in client.frames)

    async def test_an_oversized_frame_is_answered_and_the_connection_survives(self, home):
        """A pasted screenshot is one line of base64. Breaking the connection on a
        long line would make an ordinary paste look like a crashed agent."""
        from raven.acp.stdio import MAX_FRAME_BYTES

        async with stub_client(env=home) as client:
            await client.write_bytes(b"x" * (MAX_FRAME_BYTES + 1024) + b"\n")
            response = await client.request("initialize", {"protocolVersion": 1})

            assert "result" in response
            assert any(f.get("error", {}).get("code") == -32600 for f in client.frames)

    async def test_a_large_but_legal_frame_goes_through(self, home, project):
        """The other side of the cap: three megabytes is what one screenshot
        looks like, and it must not be refused."""
        import base64

        async with stub_client(env=home) as client:
            await client.handshake()
            session = await client.new_session(project)
            data = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * (3 * 1024 * 1024)).decode()
            response = await client.request(
                "session/prompt",
                {"sessionId": session, "prompt": [{"type": "image", "data": data, "mimeType": "image/png"}]},
                timeout=120.0,
            )

            assert "result" in response


class TestNotifications:
    async def test_a_cancel_for_a_session_that_never_existed_is_ignored(self, home):
        """A client cancelling a session it already dropped is tidy, not broken --
        and a notification has no reply, so the only wrong answer is dying."""
        async with stub_client(env=home) as client:
            await client.handshake()
            await client.notify("session/cancel", {"sessionId": "acp:invented"})
            await client.notify("session/cancel", {})

            assert "result" in await client.request("initialize", {"protocolVersion": 1})

    async def test_a_protocol_level_cancel_is_ignored_safely(self, home):
        """``$/cancel_request`` is explicitly optional: a receiver MAY act on it,
        and ignoring it is conformant."""
        async with stub_client(env=home) as client:
            await client.notify("$/cancel_request", {"id": "stub-1"})

            assert "result" in await client.request("initialize", {"protocolVersion": 1})

    async def test_an_unknown_method_is_answered_rather_than_dropped(self, home):
        """A liveness probe or a newer client's method. Silence would leave the
        client's promise pending for the life of the session."""
        async with stub_client(env=home) as client:
            await client.handshake()
            response = await client.request("ping", {})

            assert response["error"]["code"] == -32601


class TestPromptTurn:
    async def test_a_turn_that_cannot_start_is_explained_and_still_ends(self, home, project):
        """No provider is configured, so the engine has a build error. The rule
        under test is that a prompt is never answered with a JSON-RPC error: the
        client gets a stop reason, and the reason is said as message content.
        """
        async with stub_client(env=home) as client:
            await client.handshake()
            session = await client.new_session(project)
            response = await client.request(
                "session/prompt",
                {"sessionId": session, "prompt": [{"type": "text", "text": "hello"}]},
            )

            assert "error" not in response, "erroring a prompt makes clients tear down the whole turn"
            assert response["result"]["stopReason"] in {"end_turn", "refusal"}
            said = client.text_of([f["params"]["update"] for f in client.frames if f.get("method") == "session/update"])
            assert said, "a turn that produced nothing and explained nothing is indistinguishable from a hang"

    async def test_a_prompt_of_nothing_but_a_resource_link_still_runs(self, home, project):
        """The block Zed sends for every file mention, gated by no capability at
        all. An agent with no branch for it sends an empty turn."""
        target = project / "notes.md"
        target.write_text("hello\n")
        async with stub_client(env=home) as client:
            await client.handshake()
            session = await client.new_session(project)
            response = await client.request(
                "session/prompt",
                {
                    "sessionId": session,
                    "prompt": [{"type": "resource_link", "uri": target.as_uri(), "name": target.name}],
                },
            )

            assert "error" not in response
            assert response["result"]["stopReason"] in {"end_turn", "refusal"}

    async def test_every_frame_the_agent_sent_is_a_legal_agent_frame(self, home, project):
        """The whole session validated against the official schema, in the
        direction the schema calls ``Agent``. A hand-written mapper drifting shows
        up here rather than in a reviewer's reading."""
        async with stub_client(env=home) as client:
            await client.handshake()
            session = await client.new_session(project)
            await client.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]})

            assert client.frames
            for frame in client.frames:
                validate_outbound(frame)
            assert client.malformed == [], f"stdout carried non-protocol lines: {client.malformed[:3]}"


class TestTeardown:
    async def test_closing_stdin_exits_zero(self, home):
        """How an editor ends a session. A non-zero exit is reported to the user
        as a crash."""
        async with stub_client(env=home) as client:
            await client.handshake()

        assert client.returncode == 0, client.stderr.decode("utf-8", "replace")[-2000:]

    async def test_a_suspended_prompt_is_answered_when_the_client_leaves(self, home, project):
        """Not observable by the client, which is gone -- but the handler has to
        return through its own code so its turn slot is released and the engine is
        not torn down underneath it. A process left behind is the visible symptom."""
        async with stub_client(env=home) as client:
            await client.handshake()
            session = await client.new_session(project)
            await client.send_request(
                "session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]}
            )

        assert client.returncode == 0, client.stderr.decode("utf-8", "replace")[-2000:]


class TestTheStubItself:
    async def test_the_stub_only_sends_frames_a_real_client_could_send(self, home, project):
        """A stub that sent an illegal frame would prove the agent tolerant of
        something no client can produce. Checked in the schema's ``Client``
        direction, which is the mirror of what the agent's own frames are held to.
        """
        frames = [
            {"jsonrpc": "2.0", "id": "a", "method": "initialize", "params": {"protocolVersion": 1}},
            {"jsonrpc": "2.0", "id": "b", "method": "session/new", "params": {"cwd": str(project), "mcpServers": []}},
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "acp:x"}},
        ]
        for frame in frames:
            validate_inbound(frame)
