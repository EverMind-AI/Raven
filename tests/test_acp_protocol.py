"""The agent direction's wire helpers, and what the handshake declares.

Every frame this file builds is also run through the vendored official schema,
so the assertions cover both "raven does what we intended" and "what we intended
is what the spec says". The second half is the one that catches a hand-written
mapper drifting, and it is why these tests import ``tests/acp_schema.py``
instead of comparing against dicts typed out here.
"""

from __future__ import annotations

import pytest

from raven.acp import protocol
from raven.acp.capabilities import ClientCapabilities, agent_capabilities, initialize_result
from tests.acp_schema import agent_method_names, is_valid_def, validate_def, validate_outbound


class TestIds:
    def test_a_string_id_survives_the_round_trip(self):
        """The client mints request ids and JSON-RPC allows a string. Answering a
        string id with a number is a correlation failure even though a reply
        arrived."""
        frame = protocol.request("req-1", "session/prompt", {"sessionId": "s"})

        assert frame["id"] == "req-1"
        validate_def("ClientRequest", frame)

    def test_a_request_carries_params_only_when_there_are_some(self):
        """Absent rather than null, for the same reason ``data`` is: a null reads
        as "there are parameters and they are empty"."""
        assert "params" not in protocol.request(1, "session/cancel")
        assert protocol.request(1, "session/cancel", {})["params"] == {}

    def test_an_error_carries_data_only_when_there_is_some(self):
        without = protocol.error_response(1, protocol.INVALID_PARAMS, "bad")
        with_data = protocol.error_response(1, protocol.INVALID_PARAMS, "bad", {"field": "cwd"})

        assert "data" not in without["error"], "a null data reads as empty detail rather than as no detail"
        assert with_data["error"]["data"] == {"field": "cwd"}
        validate_outbound(without)
        validate_outbound(with_data)

    def test_the_acp_specific_codes_are_the_ones_the_spec_assigns(self):
        # Typed out rather than derived, because these are the load-bearing
        # numbers: -32002 is what tells a client its session is gone rather than
        # empty, and -32800 is what a cancelled request must answer.
        assert (protocol.AUTH_REQUIRED, protocol.RESOURCE_NOT_FOUND, protocol.REQUEST_CANCELLED) == (
            -32000,
            -32002,
            -32800,
        )


class TestVersionNegotiation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (1, 1),
            (2, 2),
            (0, 0),
            (1.0, 1),
            ("1", 1),
            ("  7 ", 7),
            # A non-ASCII decimal numeral, which ``int()`` parses. Twelve rather
            # than one so the assertion can tell reading it apart from falling
            # back: the fallback is 1, so a single digit here would pass either
            # way and prove nothing.
            ("\u0661\u0662", 12),
        ],
    )
    def test_a_readable_version_is_read(self, raw, expected):
        assert protocol.normalize_protocol_version(raw) == expected

    @pytest.mark.parametrize("raw", [None, "one", "1.5", 1.5, [], {}, True, False, "\u00b2", "\u00bd"])
    def test_an_unreadable_version_falls_back_rather_than_failing(self, raw):
        """Failing the handshake over a malformed version denies the client the
        one thing it needs to decide what to do: which version we serve.

        ``True`` is listed because ``bool`` is an ``int`` subclass -- normalising
        it to 1 would be a coincidence, not a reading of intent. Superscript two
        is listed because it is the case the ``isdecimal`` check exists for: it
        satisfies ``isdigit`` and makes ``int()`` raise, so the looser check would
        have guarded nothing.
        """
        assert protocol.normalize_protocol_version(raw) == protocol.PROTOCOL_VERSION

    def test_an_unsupported_version_is_answered_with_ours(self):
        assert protocol.negotiated_version(99) == protocol.PROTOCOL_VERSION
        assert protocol.negotiated_version(1) == 1

    def test_stop_reasons_are_exactly_the_schema_enum(self):
        for reason in protocol.STOP_REASONS:
            validate_def("StopReason", reason)
        assert not is_valid_def("StopReason", "done")
        assert len(protocol.STOP_REASONS) == 5


class TestDeclaredCapabilities:
    def test_the_initialize_result_matches_the_schema(self):
        validate_def("InitializeResponse", initialize_result({"protocolVersion": 1}))

    def test_it_survives_a_client_that_sends_nothing_at_all(self):
        """A handshake is the one exchange with no surface for reporting its own
        failure, so it must not have one."""
        validate_def("InitializeResponse", initialize_result(None))
        validate_def("InitializeResponse", initialize_result({}))

    def test_no_authentication_is_declared_as_a_fact_not_an_omission(self):
        assert initialize_result({})["authMethods"] == []

    def test_nothing_unbuilt_is_declared(self):
        """The worst failure shape in the protocol is a declared capability with
        nothing behind it: the client routes work to a method that errors, and
        the turn stalls on a promise nobody keeps."""
        caps = agent_capabilities()

        assert caps["promptCapabilities"]["audio"] is False, "there is no audio path on the prompt side"
        assert caps["mcpCapabilities"] == {"http": False, "sse": False}, "MCP is per process, not per session"
        assert caps["auth"] == {}, "declaring auth.logout would put a method on the wire with nothing to end"
        # ``list`` is declared and the other four are not. Each of resume / close
        # / delete / additionalDirectories is stable in the schema and unbuilt
        # here, and each one declared is a method that must then work.
        assert set(caps["sessionCapabilities"]) == {"list"}

    def test_what_is_declared_is_declared_because_it_works(self):
        caps = agent_capabilities()

        assert caps["promptCapabilities"]["image"] is True
        assert caps["promptCapabilities"]["embeddedContext"] is True

    def test_load_is_declared_only_because_the_replay_exists(self):
        """The flag is a promise. A client that reopens a session on a false one
        shows a person an empty history for a conversation that had one."""
        from raven.acp.methods import UNIMPLEMENTED_METHODS
        from raven.acp.replay import replay

        assert agent_capabilities()["loadSession"] is True
        assert "session/load" not in UNIMPLEMENTED_METHODS
        assert replay([{"role": "user", "text": "hi"}], session_id="acp:s"), "the replay must produce something"

    def test_list_is_declared_only_because_the_method_answers(self):
        from raven.acp.methods import UNIMPLEMENTED_METHODS

        assert "list" in agent_capabilities()["sessionCapabilities"]
        assert "session/list" not in UNIMPLEMENTED_METHODS

    def test_the_agent_names_itself_with_a_real_version(self):
        info = initialize_result({})["agentInfo"]

        assert info["name"] == "raven"
        assert info["version"] and info["version"] != "unknown"
        validate_def("Implementation", info)


class TestRecordedClientCapabilities:
    def test_a_client_that_declares_nothing_can_do_nothing(self):
        for params in (None, {}, {"clientCapabilities": None}, {"clientCapabilities": "yes"}):
            caps = ClientCapabilities.from_params(params)

            assert caps.elicitation is False
            assert caps.reads_files is False
            assert caps.writes_files is False
            assert caps.has_terminal is False

    def test_elicitation_is_read_by_presence_because_it_is_an_object(self):
        """The capability is an object in the schema, not a boolean. Reading it
        as truthy would make ``{"elicitation": {}}`` -- a client declaring
        support with no options -- read as no support."""
        assert ClientCapabilities.from_params({"clientCapabilities": {"elicitation": {}}}).elicitation is True

    def test_the_fs_flags_are_read_from_inside_their_section(self):
        caps = ClientCapabilities.from_params(
            {"clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": False}}}
        )

        assert caps.reads_files is True
        assert caps.writes_files is False

    def test_a_malformed_section_does_not_raise(self):
        caps = ClientCapabilities.from_params({"clientCapabilities": {"fs": "both"}})

        assert caps.reads_files is False


class TestManifestAgreement:
    def test_every_method_raven_answers_is_in_the_stable_manifest(self):
        """Guards against serving an unstable method by name. The manifest omits
        all 18 of them, so membership is the check."""
        from raven.acp.methods import UNIMPLEMENTED_METHODS

        served = {"initialize", "authenticate", "session/new", "session/prompt", "session/cancel"}
        for method in served | UNIMPLEMENTED_METHODS:
            assert method in agent_method_names(), f"{method} is not in the stable manifest"
