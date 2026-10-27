"""Agent Plugins 1.0.0 manifest reading, cross-checked against the official schema.

The vendored schema at ``schemas/agent-plugins/1.0.0/plugin.schema.json`` is
an unmodified snapshot of the official one, so it is the yardstick: this
file asks the official validator and ``read_manifest`` the same question
about the same documents and compares the answers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from jsonschema import Draft202012Validator

from raven.agent_plugins.manifest import (
    AGENT_PLUGINS_SCHEMA_URL,
    MANIFEST_FILENAME,
    SUPPORTED_SCHEMA_URLS,
    AgentPluginManifestError,
    read_manifest,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "agent-plugins" / "1.0.0" / "plugin.schema.json"

UNSUPPORTED_SCHEMA_URL = "https://agent-plugins.org/schemas/2.0.0/plugin.schema.json"

_FULL_DOCUMENT: dict[str, Any] = {
    "$schema": AGENT_PLUGINS_SCHEMA_URL,
    "name": "acme.tools",
    "version": "1.2.3",
    "description": "A plugin that does things.",
    "author": {
        "name": "Acme Inc",
        "email": "dev@acme.example",
        "url": "https://acme.example",
    },
    "homepage": "https://acme.example",
    "repository": "https://acme.example/repo",
    "license": "Apache-2.0",
    "keywords": ["acme", "tools"],
    "extensions": {"com.acme.custom": {"free": "form"}},
}


class _Case(NamedTuple):
    label: str
    document: Any
    official_accepts: bool
    ours_accepts: bool


_CASES = (
    _Case(
        "valid minimal",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "acme"},
        True,
        True,
    ),
    _Case("valid full", _FULL_DOCUMENT, True, True),
    _Case(
        "unknown top-level field",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "acme", "futureField": 1},
        False,
        True,
    ),
    _Case(
        "author with an extra key",
        {
            "$schema": AGENT_PLUGINS_SCHEMA_URL,
            "name": "acme",
            "author": {"name": "Acme", "nickname": "A"},
        },
        False,
        False,
    ),
    _Case("missing $schema", {"name": "acme"}, False, False),
    _Case(
        "unsupported $schema",
        {"$schema": UNSUPPORTED_SCHEMA_URL, "name": "acme"},
        False,
        False,
    ),
    _Case(
        "name in upper case",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "Acme"},
        False,
        False,
    ),
    _Case(
        "name with a leading hyphen",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "-acme"},
        False,
        False,
    ),
    _Case(
        "name with a double hyphen",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "has--double"},
        False,
        False,
    ),
    _Case(
        "name with a double period",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "too.many..dots"},
        False,
        False,
    ),
    _Case(
        "empty name",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": ""},
        False,
        False,
    ),
    _Case(
        "name longer than 64 characters",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "a" * 65},
        False,
        False,
    ),
    _Case(
        "name of exactly 64 characters",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "a" * 64},
        True,
        True,
    ),
    _Case(
        "name with a period",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "acme.tools"},
        True,
        True,
    ),
    _Case("manifest is not an object", ["acme"], False, False),
    _Case(
        "extensions is not an object",
        {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "acme", "extensions": ["a"]},
        False,
        True,
    ),
    _Case(
        "extensions with an unknown namespace",
        {
            "$schema": AGENT_PLUGINS_SCHEMA_URL,
            "name": "acme",
            "extensions": {"com.acme.unheard-of": {"anything": [1, 2]}},
        },
        True,
        True,
    ),
)

_DIVERGENT = frozenset({"unknown top-level field", "extensions is not an object"})
"""The two cases the specification lets through where the schema refuses.

An unknown top-level field and a non-object ``extensions`` field are
reported and ignored, so the rest of the manifest still loads. Every other
case answers the schema the same way.
"""


@pytest.fixture(scope="module")
def official_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _manifest_file(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / MANIFEST_FILENAME
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _ours_accepts(document: Any, tmp_path: Path) -> bool:
    try:
        read_manifest(_manifest_file(tmp_path, document))
    except AgentPluginManifestError:
        return False
    return True


# ---------------------------------------------------------------------------
# The cross-check: one corpus, two validators, one answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=[case.label for case in _CASES])
def test_read_manifest_agrees_with_the_official_schema(
    case: _Case,
    tmp_path: Path,
    official_validator: Draft202012Validator,
) -> None:
    """The vendored schema and the loader must answer alike.

    The table states what each side is expected to do, the official
    validator is asked to confirm its own column, and the two columns are
    then compared.
    """
    assert official_validator.is_valid(case.document) is case.official_accepts

    path = _manifest_file(tmp_path, case.document)
    if case.ours_accepts:
        read_manifest(path)
    else:
        with pytest.raises(AgentPluginManifestError):
            read_manifest(path)

    if case.label in _DIVERGENT:
        assert not case.official_accepts and case.ours_accepts
    else:
        assert case.official_accepts is case.ours_accepts


def test_the_only_departures_from_the_official_schema_are_the_documented_ones(
    tmp_path: Path,
    official_validator: Draft202012Validator,
) -> None:
    """A corpus-wide statement: the two sides differ on exactly the two
    cases the specification lets through, and on nothing else."""
    differences = {
        case.label
        for case in _CASES
        if official_validator.is_valid(case.document) != _ours_accepts(case.document, tmp_path)
    }
    assert differences == set(_DIVERGENT)


# ---------------------------------------------------------------------------
# The manifest that loads
# ---------------------------------------------------------------------------


def test_a_trailing_newline_in_a_name_is_refused(tmp_path: Path) -> None:
    """The spec's name pattern is anchored at the end; Python's ``$`` is not.

    The parity table above cannot carry this case: ``jsonschema`` evaluates
    patterns with Python's ``re``, so it shares the quirk the spec does not
    have.
    """
    path = _manifest_file(tmp_path, {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "acme\n"})

    with pytest.raises(AgentPluginManifestError):
        read_manifest(path)


def test_a_minimal_manifest_loads_without_notes(tmp_path: Path) -> None:
    manifest, notes = read_manifest(_manifest_file(tmp_path, {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "acme"}))
    assert manifest.name == "acme"
    assert manifest.version is None
    assert manifest.unknown_fields == ()
    assert notes == ()


def test_a_full_manifest_keeps_every_permitted_field(tmp_path: Path) -> None:
    manifest, notes = read_manifest(_manifest_file(tmp_path, _FULL_DOCUMENT))
    assert manifest.name == "acme.tools"
    assert manifest.version == "1.2.3"
    assert manifest.description == "A plugin that does things."
    assert manifest.author is not None
    assert manifest.author.name == "Acme Inc"
    assert manifest.author.email == "dev@acme.example"
    assert manifest.author.url == "https://acme.example"
    assert manifest.homepage == "https://acme.example"
    assert manifest.repository == "https://acme.example/repo"
    assert manifest.license == "Apache-2.0"
    assert manifest.keywords == ["acme", "tools"]
    assert manifest.extensions == {"com.acme.custom": {"free": "form"}}
    assert notes == ()


# ---------------------------------------------------------------------------
# Non-fatal findings
# ---------------------------------------------------------------------------


def test_an_unknown_top_level_field_loads_and_is_reported(tmp_path: Path) -> None:
    manifest, notes = read_manifest(
        _manifest_file(
            tmp_path,
            {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "acme", "futureField": 1},
        )
    )
    assert manifest.name == "acme"
    assert manifest.unknown_fields == ("futureField",)
    assert len(notes) == 1
    assert "futureField" in notes[0]


def test_a_non_object_extensions_field_loads_and_is_reported(tmp_path: Path) -> None:
    manifest, notes = read_manifest(
        _manifest_file(
            tmp_path,
            {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": "acme", "extensions": ["a"]},
        )
    )
    assert manifest.name == "acme"
    assert manifest.extensions == {}
    assert len(notes) == 1
    assert "extensions" in notes[0]


def test_an_unknown_namespace_is_kept_unchanged(tmp_path: Path) -> None:
    """The specification assigns no semantics to a namespace's contents."""
    document = {
        "$schema": AGENT_PLUGINS_SCHEMA_URL,
        "name": "acme",
        "extensions": {"com.acme.unheard-of": {"anything": [1, 2], "nested": {"a": 1}}},
    }
    manifest, notes = read_manifest(_manifest_file(tmp_path, document))
    assert manifest.extensions == document["extensions"]
    assert notes == ()


# ---------------------------------------------------------------------------
# Fatal violations
# ---------------------------------------------------------------------------


def test_an_unreadable_manifest_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / MANIFEST_FILENAME
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AgentPluginManifestError) as excinfo:
        read_manifest(path)
    assert "JSON" in str(excinfo.value)


def test_a_manifest_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / MANIFEST_FILENAME
    path.write_text('["acme"]', encoding="utf-8")
    with pytest.raises(AgentPluginManifestError) as excinfo:
        read_manifest(path)
    assert "object" in str(excinfo.value)


def test_a_missing_schema_declaration_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AgentPluginManifestError) as excinfo:
        read_manifest(_manifest_file(tmp_path, {"name": "acme"}))
    assert AGENT_PLUGINS_SCHEMA_URL in str(excinfo.value)


def test_an_unsupported_schema_version_is_rejected_and_named(tmp_path: Path) -> None:
    with pytest.raises(AgentPluginManifestError) as excinfo:
        read_manifest(_manifest_file(tmp_path, {"$schema": UNSUPPORTED_SCHEMA_URL, "name": "acme"}))
    message = str(excinfo.value)
    assert UNSUPPORTED_SCHEMA_URL in message
    assert "2.0.0" in message
    assert str(sorted(SUPPORTED_SCHEMA_URLS)) in message


@pytest.mark.parametrize(
    "name",
    ["Acme", "-acme", "acme-", "has--double", "too.many..dots", "", "a" * 65, "acme tools"],
)
def test_an_invalid_name_is_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(AgentPluginManifestError):
        read_manifest(_manifest_file(tmp_path, {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": name}))


@pytest.mark.parametrize("name", ["acme", "acme.tools", "acme-tools", "a", "a" * 64, "a1.b2-c3"])
def test_a_valid_name_is_accepted(tmp_path: Path, name: str) -> None:
    manifest, notes = read_manifest(_manifest_file(tmp_path, {"$schema": AGENT_PLUGINS_SCHEMA_URL, "name": name}))
    assert manifest.name == name
    assert notes == ()


def test_an_author_with_an_extra_key_is_rejected(tmp_path: Path) -> None:
    """The official schema closes the author object, so the loader does too."""
    with pytest.raises(AgentPluginManifestError):
        read_manifest(
            _manifest_file(
                tmp_path,
                {
                    "$schema": AGENT_PLUGINS_SCHEMA_URL,
                    "name": "acme",
                    "author": {"name": "Acme", "nickname": "A"},
                },
            )
        )
