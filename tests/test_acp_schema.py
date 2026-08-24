"""The vendored ACP schema is pinned, local, and actually discriminating.

Three separate claims, because a schema fixture can fail in three unrelated
ways and each failure means something different to whoever reads the red test:

1. **It is the file that was vendored.** ``VERSION.json`` records a release and
   a sha256 per file. Upgrading the spec then has to touch both, which turns
   "the schema moved" from an invisible drift into a reviewable diff.
2. **It resolves offline.** Every ``$ref`` is a local ``#/$defs/`` pointer, so
   ``tests/acp_schema.py`` can build validators by wrapping a pointer instead of
   standing up a registry -- and a future schema with an external ref fails here
   rather than resolving to nothing and passing everything.
3. **It says no to the frames it should.** A validator that accepts everything
   is worse than no validator: it makes the mapping look checked. The
   discrimination cases below are the ones that decide real design questions in
   the mapping table, each paired with its positive twin so a validator that
   rejected everything would fail too.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.acp_schema import (
    META_PATH,
    SCHEMA_PATH,
    AcpSchemaError,
    agent_method_names,
    client_method_names,
    is_valid_def,
    meta,
    schema,
    sha256_of,
    validate_def,
    validate_inbound,
    validate_outbound,
    version_stamp,
)


class TestPinnedFixture:
    def test_version_stamp_matches_every_digest(self):
        stamp = version_stamp()
        assert stamp["acp_schema_version"] == "1.20.0"
        assert stamp["sha256"], "VERSION.json records no digests"
        for name, expected in stamp["sha256"].items():
            assert sha256_of(name) == expected, (
                f"{name} does not match the digest in VERSION.json. If the schema was "
                "upgraded on purpose, update acp_schema_version and both digests in the "
                "same change."
            )

    def test_the_schema_is_itself_valid(self):
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema())

    def test_the_manifest_covers_both_directions(self):
        # Read from the manifest rather than compared against a list typed out
        # here: a hand-kept copy would have to be edited in lock-step with the
        # fixture, and the edit that got forgotten is the one that matters.
        assert "initialize" in agent_method_names()
        assert "session/prompt" in agent_method_names()
        assert "session/update" in client_method_names()
        assert "session/request_permission" in client_method_names()

    def test_the_manifest_omits_the_unstable_methods(self):
        # The manifest is the stable surface. Nothing in it may look unstable,
        # and the check is spelled as a property of the whole set rather than as
        # a blocklist so a newly-unstable method cannot slip in unnamed.
        for name in agent_method_names() | client_method_names():
            assert "unstable" not in name
            assert not name.startswith("_")

    def test_no_extra_files_crept_into_the_fixture_directory(self):
        stamp = version_stamp()
        on_disk = {p.name for p in SCHEMA_PATH.parent.iterdir() if p.is_file()}
        assert on_disk == set(stamp["sha256"]) | {"VERSION.json"}


class TestRefsResolveOffline:
    def test_every_ref_is_local(self):
        raw = SCHEMA_PATH.read_text(encoding="utf-8")
        refs = set(re.findall(r'"\$ref":\s*"([^"]+)"', raw))
        assert refs, "found no $refs at all, which means this test stopped testing anything"
        external = sorted(r for r in refs if not r.startswith("#/$defs/"))
        assert external == [], (
            "acp_schema._validator resolves refs by wrapping a pointer in a document that "
            f"carries $defs; these refs cannot resolve that way: {external}"
        )

    def test_the_manifest_is_parseable_json(self):
        # Cheap, but it is the file the method-name assertions read, and a
        # trailing comma there would otherwise surface as an unrelated failure.
        assert isinstance(json.loads(META_PATH.read_text(encoding="utf-8")), dict)
        assert meta()["version"] == 1


class TestDiscrimination:
    """Each pair decides a question the mapping table answers.

    Positive and negative together: alone, either half can be satisfied by a
    validator that is broken in one direction.
    """

    def test_session_update_enum_is_closed(self):
        good = {
            "sessionId": "s1",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}},
        }
        validate_def("SessionNotification", good)
        bad = {**good, "update": {**good["update"], "sessionUpdate": "agent_message"}}
        assert not is_valid_def("SessionNotification", bad)

    def test_request_permission_requires_a_tool_call(self):
        # Why ask_user cannot be a bare question: a pure question has no tool
        # call, and the field is required. The mapping either synthesises one or
        # routes through elicitation.
        options = [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}]
        assert not is_valid_def("RequestPermissionRequest", {"sessionId": "s1", "options": options})
        validate_def(
            "RequestPermissionRequest",
            {
                "sessionId": "s1",
                "options": options,
                "toolCall": {"toolCallId": "t1", "title": "rm -rf build"},
            },
        )

    def test_a_diff_needs_no_old_text(self):
        # Why structured diffs do not have to wait for a before-image: oldText
        # is optional, so the write tool's own content is enough.
        validate_def("Diff", {"path": "/tmp/a.py", "newText": "x = 1\n"})
        assert not is_valid_def("Diff", {"newText": "x = 1\n"})

    def test_a_refusal_is_a_selection_not_a_denial(self):
        # Why there is no "denied" outcome to send: refusing is `selected` with
        # a reject option id.
        assert not is_valid_def("RequestPermissionOutcome", {"outcome": "denied"})
        validate_def("RequestPermissionOutcome", {"outcome": "cancelled"})
        validate_def("RequestPermissionOutcome", {"outcome": "selected", "optionId": "reject"})

    def test_stop_reason_is_one_of_five(self):
        for reason in ("end_turn", "cancelled", "refusal", "max_tokens", "max_turn_requests"):
            validate_def("StopReason", reason)
        assert not is_valid_def("StopReason", "finished")

    def test_tool_kind_is_closed(self):
        validate_def("ToolKind", "execute")
        assert not is_valid_def("ToolKind", "shell")

    def test_an_unknown_definition_name_is_an_error_not_a_pass(self):
        # The failure mode this guards: a typo'd definition name would otherwise
        # make every assertion in a test file vacuously true.
        with pytest.raises(AcpSchemaError, match="no definition named"):
            validate_def("SessionNotifcation", {})


class TestFrameDirection:
    def test_an_agent_reply_is_outbound_and_a_client_call_is_inbound(self):
        validate_outbound({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}})
        validate_inbound(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": 1, "clientCapabilities": {}},
            }
        )

    def test_a_frame_without_jsonrpc_is_refused_in_both_directions(self):
        with pytest.raises(AcpSchemaError):
            validate_outbound({"id": 1, "result": {}})
        with pytest.raises(AcpSchemaError):
            validate_inbound({"id": 1, "method": "initialize"})

    def test_the_direction_branches_are_selected_by_title(self):
        # Guards the reordering hazard: if the two top-level branches were
        # picked by index, a schema upgrade that swapped them would point every
        # outbound assertion at the client half and still pass.
        assert [b.get("title") for b in schema()["anyOf"]][:2] == ["Agent", "Client"]

    def test_the_frame_level_check_is_blind_to_an_invented_method(self):
        # Recorded as a test, not as a comment, because it is the reason payload
        # assertions use validate_def: `method` is `type: string` with no enum
        # and `params` is nullable, so this passes. If a schema upgrade ever
        # closes that, this test fails and the mapping can start relying on it.
        validate_outbound({"jsonrpc": "2.0", "method": "session/opdate"})
        assert "session/opdate" not in client_method_names()
